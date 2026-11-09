"""0.5.6：第二轮独立审查 INDEPENDENT_REVIEW_0.5.5 各条问题的回归测试。

每条测试都直接复现原问题（作者复核脚本 author_check_055.py 的场景），修复前应失败、修复后通过。只用合成数据，不联网。
"""

import asyncio
import gzip
import logging
import sqlite3
import threading
import time
import tracemalloc
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hub import cards, net
from hub.executor import Executor, RunResult, make_plan
from hub.health import Health
from hub.image_exec import ImageExecutor, ImagePlan
from hub.image_sources import ImageHit
from hub.jobs import Job, JobManager
from hub.ledger import Ledger
from hub.merge import rrf_merge
from hub.net import Outcome, SourceError, TooLarge, read_limited, redact, set_secrets
from hub.providers import Hit, ProviderInfo

from test_core import Fake, hits


@pytest.fixture(autouse=True)
def _clear_secrets():
    def reset():
        getattr(net, "_retired", {}).clear()
        set_secrets([])
    reset()
    yield
    reset()


async def _read(body: bytes, enc: str, cap: int, *, truncate: bool = False, chunk: int = 0):
    def handler(_r):
        stream = httpx.ByteStream(body)
        if chunk:
            class Chunks(httpx.AsyncByteStream):
                async def __aiter__(self):
                    for i in range(0, len(body), chunk):
                        yield body[i:i + chunk]
            stream = Chunks()
        return httpx.Response(200, headers={"content-encoding": enc} if enc else {}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        async with c.stream("GET", "https://a.test/") as resp:
            return await read_limited(resp, cap, truncate=truncate)


# ---------------- B1：解压每一步都受限 ----------------
@pytest.mark.parametrize("enc,body", [
    ("gzip", gzip.compress(b"a" * 8 * 2**20, 9)),
    ("deflate", zlib.compress(b"a" * 8 * 2**20, 9)),
])
async def test_b1_single_layer_bomb_peak_is_bounded(enc, body):
    """8 MiB 的压缩炸弹（线缆约 8 KB），上限 16 KiB：0.5.5 峰值约 21 MiB，现在应在 1 MiB 以内。"""
    tracemalloc.start()
    with pytest.raises(TooLarge):
        await _read(body, enc, 16384)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 1 * 2**20, f"峰值 {peak/2**20:.1f} MiB"


async def test_b1_truncate_mode_bomb_is_bounded():
    bomb = gzip.compress(b"a" * 8 * 2**20, 9)     # 先在 tracemalloc 之外造好
    tracemalloc.start()
    out = await _read(bomb, "gzip", 16384, truncate=True)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert out == b"a" * 16384 and peak < 1 * 2**20


async def test_b1_stacked_encodings_rejected():
    """两层 gzip（0.5.5 峰值约 53 MiB）：请求里只声明一层 gzip/deflate，多层或其他编码一律拒绝。"""
    body = gzip.compress(gzip.compress(b"a" * 24 * 2**20, 9), 9)
    for enc in ("gzip, gzip", "br", "zstd", "deflate, gzip"):
        with pytest.raises(SourceError):
            await _read(body, enc, 16384)


async def test_b1_wire_bytes_capped_even_when_decoded_small():
    """压缩数据几乎不产出内容（例如超长 gzip 头部字段）也不能无限读：线缆字节有上限。"""
    header = b"\x1f\x8b\x08\x08\x00\x00\x00\x00\x00\xff" + b"x" * 200_000   # FNAME 字段一直不结束
    with pytest.raises(TooLarge):
        await _read(header, "gzip", 1000, chunk=4096)


async def test_b1_roundtrip_and_errors():
    txt = ("hello 世界 " * 5000).encode()
    assert await _read(gzip.compress(txt), "gzip", 10**6, chunk=777) == txt
    assert await _read(zlib.compress(txt), "deflate", 10**6) == txt
    assert await _read(zlib.compress(txt)[2:-4], "deflate", 10**6) == txt      # 裸 deflate
    assert await _read(txt, "identity", 10**6) == txt
    assert await _read(gzip.compress(b""), "gzip", 10) == b""
    with pytest.raises(SourceError):
        await _read(gzip.compress(txt)[:-200], "gzip", 10**6)                   # 被截断
    with pytest.raises(SourceError):
        await _read(b"\x1f\x8b\x08\x00garbage!", "gzip", 10**6)                # 不是有效压缩数据


def test_b1_requests_only_declare_supported_encodings():
    assert net.ACCEPT_ENCODING == "gzip, deflate"
    assert net.Http()._client.headers["accept-encoding"] == "gzip, deflate"


# ---------------- B2：脱敏 ----------------
KEY_OLD = "OLDKEY-0123456789abcdef"
KEY_NEW = "NEWKEY-fedcba9876543210"


def test_b2_old_key_still_redacted_after_change():
    """改配置换 key 之后，在途旧请求回显的旧 key 仍要遮掉（宽限期内）。"""
    set_secrets([KEY_OLD], now=100.0)
    set_secrets([KEY_NEW], now=110.0)
    assert KEY_OLD not in redact(f"401 invalid key {KEY_OLD}")
    assert KEY_NEW not in redact(f"key {KEY_NEW}")
    set_secrets([KEY_NEW], now=110.0 + net.SECRET_GRACE + 1)       # 宽限期过后旧 key 才退出
    assert KEY_OLD in redact(KEY_OLD)


def test_b2_redact_before_clip():
    """SourceError 先脱敏再截断：截断后的 key 前半段不能露出来。"""
    key = "0123456789abcdefghij0123456789abcdefghij"
    set_secrets([key])
    from hub.providers import _clip
    e = SourceError(Outcome.REJECTED, "error: invalid key " + key + " please check")
    assert "0123456789" not in e.message and "***" in e.message
    # 来源代码不再预先截断：错误文字整段交给 SourceError
    e2 = SourceError(Outcome.REJECTED, _clip("x" * 30 + key, 200))
    assert "0123456789" not in e2.message
    assert len(SourceError(Outcome.REJECTED, "y" * 500).message) <= net.MESSAGE_MAX
    assert key not in str(e.args)


def test_b2_sources_do_not_clip_before_error():
    """来源代码里不再出现 SourceError(..., _clip(...)) 这种先截断的写法。"""
    import re
    root = Path(__file__).resolve().parents[1] / "hub"
    for f in ("sources.py", "image_sources.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert not re.search(r"SourceError\(Outcome\.[A-Z_]+,\s*_clip\(", src), f


def test_d83_search_content_kept_verbatim():
    """0.6.1（D83，用户决定）：搜到的公开内容不改写——网址里的 key=value 示例、正文里像 key 的字符串都原样保留。
    报错文字和日志仍然脱敏（见 test_b2_redact_before_clip、test_b2_log_filter_redacts_exception_text）。"""
    set_secrets(["SECRETKEY-abcdef"])
    led = Ledger(None)
    m = rrf_merge([("a", "a", 1, [Hit("title key=value 示例", "https://a.test/p?key=value&x=1", "snippet")])])
    j = Job("J1", "s", "q", "quick", "auto", "any", "planner")
    j.result = RunResult(m, [], [], ["keenable"])
    j.entries = led.record("s", m)
    out = cards.sync_result(j)
    assert "key=value" in out and "?key=value&x=1" in out
    assert cards._link("https://a.test/p?utm_source=x&key=value") == "https://a.test/p?key=value"   # 跟踪参数仍去掉


def test_b2_log_filter_redacts_exception_text():
    set_secrets(["SECRETKEY-abcdef"])
    rec = logging.LogRecord("httpx", logging.ERROR, __file__, 1, "boom", None, None)
    try:
        raise RuntimeError("url https://x.test/?api_key=SECRETKEY-abcdef")
    except RuntimeError:
        import sys
        rec.exc_info = sys.exc_info()
    net.RedactingFilter().filter(rec)
    text = logging.Formatter().format(rec)
    assert "SECRETKEY-abcdef" not in text


# ---------------- B3：发布脚本目标目录 ----------------
def _release():
    import sys
    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import release
    return release


def test_b3_stage_refuses_parent_child_self_symlink_and_non_git(tmp_path):
    release = _release()
    root = release.ROOT.resolve()
    for bad in (root.parent, root, root / "hub"):
        with pytest.raises(SystemExit):
            release.check_dest(bad)
    link = tmp_path / "link"
    link.symlink_to(root.parent)
    with pytest.raises(SystemExit):
        release.check_dest(link)
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "important.txt").write_text("x")
    with pytest.raises(SystemExit):
        release.check_dest(junk)
    assert (junk / "important.txt").exists()
    ok = tmp_path / "repo"
    (ok / ".git").mkdir(parents=True)
    assert release.check_dest(ok) == ok.resolve()
    assert release.check_dest(tmp_path / "new") == (tmp_path / "new").resolve()


def test_b3_parent_dir_stage_does_not_delete_dev_files(tmp_path):
    """原复现：把开发仓库复制到 box/dev，stage(box) 会删掉 dev 里的源文件。现在应在删除前拒绝。"""
    import shutil
    release = _release()
    dev = tmp_path / "dev"
    shutil.copytree(release.ROOT, dev, ignore=shutil.ignore_patterns(".git", ".venv*", "__pycache__", "dist",
                                                                     ".pytest_cache", ".local"))
    old = release.ROOT
    release.ROOT = dev
    try:
        with pytest.raises(SystemExit):
            release.stage(tmp_path)
    finally:
        release.ROOT = old
    assert (dev / "plugin.py").exists()


def test_b3_untracked_and_symlinked_files_rejected(tmp_path):
    import build_zip  # noqa: F401  (由 _release 加入路径)
    release = _release()
    bz = release.build_zip
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "real.py").write_text("y")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    with pytest.raises(ValueError, match="git"):
        bz.check_file(tmp_path, "a.py", tracked={"real.py"})
    with pytest.raises(ValueError, match="符号链接"):
        bz.check_file(tmp_path, "link.py", tracked={"link.py"})
    bz.check_file(tmp_path, "real.py", tracked={"real.py"})
    (tmp_path / "d").mkdir()
    (tmp_path / "sub").symlink_to(tmp_path / "d", target_is_directory=True)
    (tmp_path / "d" / "m.py").write_text("z")
    with pytest.raises(ValueError, match="符号链接"):
        bz.check_file(tmp_path, "sub/m.py", tracked={"sub/m.py"})


# ---------------- S1：卸载时取消全部尾部请求 ----------------
async def test_s1_shutdown_cancels_tails_created_during_cancel():
    """normal 档主源在跑时任务被卸载取消：Executor 的 finally 才把主源请求交给 tails。
    0.5.5 先拍 tails 快照，这个请求漏网；现在卸载结束时不应有未完成的请求。"""
    led = Ledger(None)
    m = JobManager(led, merge_window=0.05)
    h = Health()
    started = asyncio.Event()

    class Slow(Fake):
        async def search(self, query, *, max_results):
            started.set()
            await asyncio.sleep(30)
            return []

    slow = Slow("keenable")
    ex = Executor(h)
    j = Job(led.next_job_id(), "s1", "python 最新 版本", "normal", "auto", "any", "planner")
    await m.start(j, make_plan([slow], h, depth="normal"), ex, window=0.05)
    await asyncio.wait_for(started.wait(), 2)
    t0 = time.monotonic()
    await asyncio.wait_for(m.shutdown(), 5)
    assert time.monotonic() - t0 < 2
    assert j.status == "interrupted"
    assert all(t.done() for t in ex.tails) and all(t.done() for t in m._tails)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
                and "Slow.search" in repr(t.get_coro())]
    assert not leftover


# ---------------- S2：并行子代理与跨会话计数 ----------------
def _msg(t):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": t}]}


def _tools(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _call(args):
    return {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t1"},
            "tool_call": {"call_id": "c1", "func_name": "search", "args": args}}


@pytest.fixture
async def plug(tmp_path):
    import plugin as P

    class Prov:
        info = ProviderInfo("keenable", "keenable", "fast", False, True, False)

        async def search(self, q, *, max_results):
            return [Hit(f"{q} 结果{i}", f"https://s{i}.example.com/{abs(hash(q))}", f"{q} 的内容 {i}") for i in range(4)]

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    p._providers = [Prov()]
    yield p
    await p.on_unload()


SUB = dict(selected_history_count=3, built_message_count=4, selection_reason="实际发送 3 条上下文|聊天消息 1|tool 2")


async def _ctx(plug, session, task):
    await plug.hub_context(items=[_msg(f"[派发给你的任务] {task}")], tool_definitions=_tools("search"),
                           session_id=session, **SUB)


async def _grant_and_search(plug, session, q):
    out = await plug.hub_grant(output_items=[_call({"query": q})], session_id=session, **SUB)
    return await plug.search(**out["modified_kwargs"]["output_items"][0]["tool_call"]["args"])


async def test_s2_interleaved_subagents_cannot_bypass_cap(plug):
    """A 已用满 3 次；同会话 A、B 的 before_request 先后到，然后才是 A 的响应。
    0.5.5：A 的搜索记到 B 上，成功派发第 4 次。现在：候选里有 A，A 已到上限 → 拒绝。"""
    for i in range(3):
        await _ctx(plug, "s", "任务A")
        assert (await _grant_and_search(plug, "s", f"a{i}"))["success"]
    await _ctx(plug, "s", "任务A")
    await _ctx(plug, "s", "任务B")          # 同阶段的另一个子代理，配对键相同
    res = await _grant_and_search(plug, "s", "a3")
    assert not res["success"] and "上限" in res["error"]


async def test_s2_same_task_text_in_other_session_counts_separately(plug):
    for i in range(3):
        await _ctx(plug, "s1", "查天气")
        assert (await _grant_and_search(plug, "s1", f"x{i}"))["success"]
    await _ctx(plug, "s2", "查天气")
    assert (await _grant_and_search(plug, "s2", "y0"))["success"]


async def test_s2_unpaired_falls_back_to_all_recent_tasks(plug):
    """配不上请求时，退回本会话近期的全部子代理任务，任一到上限就拒绝。"""
    for i in range(3):
        await _ctx(plug, "s", "任务A")
        assert (await _grant_and_search(plug, "s", f"a{i}"))["success"]
    await _ctx(plug, "s", "任务B")
    plug._task_by_req.clear()                # 模拟配对失败
    plug._caller_by_req.clear()
    res = await _grant_and_search(plug, "s", "b0")
    assert not res["success"]


async def test_s2_stale_pairing_entry_is_replaced(plug):
    """响应里没有调用本插件工具时，配对键上的候选不会被取走；过了有效期后不能再被合并进新请求。"""
    import plugin as P
    await _ctx(plug, "s", "任务A")
    rk = next(iter(plug._task_by_req))
    cands, at, pending = plug._task_by_req[rk]
    plug._task_by_req[rk] = (cands, at - P.REQ_PAIR_TTL - 1, pending)
    await _ctx(plug, "s", "任务B")
    assert len(plug._task_by_req[rk][0]) == 1


async def test_s2_idle_task_count_resets(plug, monkeypatch):
    for i in range(3):
        await _ctx(plug, "s", "任务A")
        assert (await _grant_and_search(plug, "s", f"a{i}"))["success"]
    for k, (n, at) in list(plug._task_searches.items()):
        plug._task_searches[k] = (n, at - plug.TASK_IDLE - 1)
    await _ctx(plug, "s", "任务A")
    assert (await _grant_and_search(plug, "s", "a9"))["success"]


# ---------------- S3：旧库迁移 ----------------
def test_s3_legacy_db_prune_then_record_keeps_numbering(tmp_path):
    db = tmp_path / "h.sqlite3"
    led = Ledger(db)
    led.record("s", rrf_merge([("a", "a", 1, [Hit("t1", "https://a.test/1", "x"), Hit("t2", "https://a.test/2", "x")])]))
    led._db.execute("DELETE FROM counters WHERE name LIKE 'sn:%'")     # 模拟 0.5.1 旧库：没有计数器
    led._db.execute("UPDATE entries SET last_seen=0, first_seen=0")
    led.close()
    led = Ledger(db)                                                    # 升级后第一次打开
    led.prune(30, 7)
    e = led.record("s", rrf_merge([("a", "a", 1, [Hit("new", "https://new.test/", "x")])]))[0]
    assert e.sn == 3
    led.close()


def test_s3_legacy_db_clear_then_record_keeps_numbering(tmp_path):
    db = tmp_path / "h.sqlite3"
    led = Ledger(db)
    led.record("s", rrf_merge([("a", "a", 1, [Hit("t1", "https://a.test/1", "x")])]))
    led._db.execute("DELETE FROM counters WHERE name LIKE 'sn:%'")
    led.close()
    led = Ledger(db)
    led.clear_session("s") if hasattr(led, "clear_session") else led._db.execute("DELETE FROM entries")
    e = led.record("s", rrf_merge([("a", "a", 1, [Hit("new", "https://new.test/", "x")])]))[0]
    assert e.sn == 2
    led.close()


def test_s3_old_schema_gets_reads_column(tmp_path):
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE entries (session_id TEXT, sn INTEGER, url_key TEXT, url TEXT, title TEXT, snippet TEXT,"
                " published_at TEXT, sources TEXT, first_seen REAL, last_seen REAL, hits INTEGER DEFAULT 1,"
                " used INTEGER DEFAULT 0, PRIMARY KEY (session_id, sn))")
    con.execute("INSERT INTO entries VALUES ('s', 7, 'k', 'https://a.test/', 't', 'x', NULL, '[]', 0, 0, 1, 0)")
    con.commit()
    con.close()
    led = Ledger(db)
    led.set_page("s", 7, "正文摘录")
    e = led.record("s", rrf_merge([("a", "a", 1, [Hit("new", "https://new.test/", "x")])]))[0]
    assert e.sn == 8
    led.close()


def test_used_counts_once_and_reads_separately(tmp_path):
    led = Ledger(None)
    led.record("s", rrf_merge([("a", "a", 1, [Hit("t1", "https://a.test/1", "x")])]))
    for _ in range(3):
        led.mark_shown("s", [1])
    led.set_page("s", 1, "正文")
    used, reads = led._db.execute("SELECT used, reads FROM entries WHERE sn=1").fetchone()
    assert (used, reads) == (1, 1)


# ---------------- S4：图片验证取消与线程并发 ----------------
class _ImgProv:
    def __init__(self):
        self.info = ProviderInfo("yandex", "yandex", "slow", paid=False, zero_marginal_cost=True, has_dates=False)
        self.info.extra["web"] = True

    async def search_image(self, jpeg, *, max_results):
        return [ImageHit(f"候选{i}", f"https://c{i}.test/", "", "visual", None, "weak",
                         extra={"thumbnail": f"https://t{i}.test/a.jpg"}) for i in range(3)]


class _SlowReader:
    async def fetch_bytes(self, url, *, max_bytes, accept):
        await asyncio.sleep(30)
        return b""


async def test_s4_cancel_during_verify_keeps_hits():
    img = SimpleNamespace(jpeg=b"x", dhash=0)
    ex = ImageExecutor(Health(), reader=_SlowReader())
    plan = ImagePlan("normal", "auto", [_ImgProv()], img)
    t = asyncio.create_task(ex.run(plan, ""))
    await asyncio.sleep(0.1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    p = ex.partial()
    assert p is not None and p.used_sources == ["yandex"] and len(p.results) == 3


async def test_s4_decode_threads_never_exceed_two(monkeypatch):
    """协程被取消后，后台解码线程仍在跑；之后的解码不能让同时运行的线程超过 2 个。"""
    from hub import image_exec
    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocking(_data):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        release.wait(5)
        with lock:
            active -= 1
        return None

    monkeypatch.setattr(image_exec, "dhash_bytes", blocking)
    first = [asyncio.create_task(image_exec.run_image_cpu(image_exec.dhash_bytes, b"")) for _ in range(2)]
    await asyncio.sleep(0.1)
    for t in first:
        t.cancel()
    second = [asyncio.create_task(image_exec.run_image_cpu(image_exec.dhash_bytes, b"")) for _ in range(2)]
    await asyncio.sleep(0.2)
    assert peak <= 2
    release.set()
    await asyncio.gather(*first, *second, return_exceptions=True)
    await asyncio.sleep(0.1)


# ---------------- S5：换 key 后旧请求晚到的失败 ----------------
def test_s5_stale_generation_failure_does_not_cool_new_key():
    h = Health()
    h.failure("keenable", SourceError(Outcome.REJECTED, "x", status=401, auth_failed=True), 0)
    assert not h.available("keenable")
    h.reset("keenable", 1)                                         # 换 key
    h.failure("keenable", SourceError(Outcome.REJECTED, "x", status=401, auth_failed=True), 0)   # 旧请求晚到
    assert h.available("keenable")
    h.failure("keenable", SourceError(Outcome.REJECTED, "x", status=401, auth_failed=True), 1)   # 新 key 真的错
    assert not h.available("keenable")


async def test_s5_plugin_tags_providers_with_key_generation(plug):
    import plugin as P
    cfg = plug.config
    cfg.sources.keenable.api_key = "keen_first_key_123456"
    plug._apply_config()
    g0 = next(p.info.extra["key_gen"] for p in plug._providers if p.info.name == "keenable")
    cfg.sources.keenable.api_key = "keen_second_key_654321"
    plug._apply_config()
    g1 = next(p.info.extra["key_gen"] for p in plug._providers if p.info.name == "keenable")
    assert g1 == g0 + 1
    assert "container" not in P._key_fingerprints(cfg) and "serpapi_lens" in P._key_fingerprints(cfg)


# ---------------- S6：预算跳过不算失败（重写 0.5.3 那条没跑到被测分支的测试） ----------------
async def test_s6_budget_skip_really_skips_third_paid_source():
    from hub.budget import Budget
    counters: dict[str, int] = {}
    budget = Budget(lambda k: counters.get(k, 0), lambda k, n: counters.__setitem__(k, counters.get(k, 0) + n))
    h = Health()
    # 两个付费主源都只返回 1 条，结果不足 → 会去对冲；第 3 个付费来源应被“本次 2 次”预算挡下
    a = Fake("baidu", hits=[Hit("python 最新 版本 a", "https://a.test/", "python 最新 版本")], zmc=False)
    b = Fake("exa", hits=[Hit("python 最新 版本 b", "https://b.test/", "python 最新 版本")], zmc=False)
    c = Fake("tavily", hits=hits("c", 3), zmc=False)
    for f in (a, b, c):
        f.info.paid = True
    plan = make_plan([a, b, c], h, depth="normal")
    plan.primary, plan.hedge_pool = [a], [b, c]
    r = await Executor(h, budget=budget).run(plan, "python 最新 版本")
    assert (a.calls, b.calls, c.calls) == (1, 1, 0)
    skipped = [x for x in r.attempts if x.source == "tavily"]
    assert skipped and skipped[0].outcome == "skipped"
    assert r.complete
    assert h.snapshot().get("tavily", {}).get("fail", 0) == 0


def test_s6_exa_default_fits_monthly_free_credit():
    from hub.budget import DEFAULT_DAILY
    from hub.config import HubConfig
    # Auto 搜索 $7/千次，每月 $10 ≈ 1428 次；31 天都按上限用也不能超
    assert DEFAULT_DAILY["exa"] * 31 <= 10 / 7 * 1000
    assert HubConfig().sources.exa.daily_limit == DEFAULT_DAILY["exa"]



async def test_r61_21_late_response_after_pop_still_counts_for_a(plug):
    """R61-21：A、B 同一配对键在途；B 先响应，C 随后登记同键；A 的响应晚到。0.6.0：A 的第 4 次搜索只记给 C。"""
    for i in range(3):
        await _ctx(plug, "s", "任务A")
        assert (await _grant_and_search(plug, "s", f"a{i}"))["success"]
    await _ctx(plug, "s", "任务A")
    await _ctx(plug, "s", "任务B")
    out = await plug.hub_grant(output_items=[], session_id="s", **SUB)       # B 的响应：没调用本插件工具
    await _ctx(plug, "s", "任务C")
    res = await _grant_and_search(plug, "s", "a3")                           # A 晚到
    assert not res["success"] and "上限" in res["error"]
