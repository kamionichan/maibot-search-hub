"""0.5.2：INDEPENDENT_REVIEW_0.5.1 各条问题的回归测试（复现脚本见作者复核 REVIEW_AUTHOR_CHECK.md）。"""

import asyncio
import gzip
import io
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from hub import cards, coexist
from hub.coexist import CoexistPolicy, hide_tools, original_name, strip_deferred_reminder
from hub.executor import Executor, RunResult, make_plan
from hub.health import Health
from hub.image import dhash_bytes
from hub.jobs import Job, JobManager
from hub.ledger import Ledger
from hub.merge import judge, normalize_url, rrf_merge
from hub.net import Http, Outcome, SourceError, TooLarge, read_limited, redact, set_secrets
from hub.providers import Hit, ProviderInfo
from hub.reader import BuiltinReader, validate_public_url

from test_core import Fake, hits


def _resolver(table):
    async def r(host, port):
        return table[host]
    return r


# ---------------- F01：解压后大小上限 ----------------
def _gzip_bomb(n: int) -> bytes:
    return gzip.compress(b"a" * n, compresslevel=9)


async def test_f01_json_gzip_bomb_is_capped():
    bomb = _gzip_bomb(20_000_000)            # 压缩后约 20 KB，解压 20 MB
    assert len(bomb) < 100_000

    def handler(req):
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                              stream=httpx.ByteStream(bomb))

    http = Http(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError) as ei:
        await http.get_json("https://api.test/x", {})
    assert "上限" in ei.value.message
    await http.aclose()


async def test_f01_read_limited_truncates_text_and_streams():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"content-encoding": "gzip"},
                                     stream=httpx.ByteStream(_gzip_bomb(5_000_000))))) as c:
        async with c.stream("GET", "https://a.test/") as resp:
            data = await read_limited(resp, 100_000, truncate=True)
        assert len(data) == 100_000
        async with c.stream("GET", "https://a.test/") as resp:
            with pytest.raises(TooLarge):
                await read_limited(resp, 100_000)


async def test_f01_reader_html_bomb_truncated_and_image_bomb_rejected():
    def handler(req):
        if req.url.path.endswith(".png"):
            return httpx.Response(200, headers={"content-type": "image/png", "content-encoding": "gzip"},
                                  stream=httpx.ByteStream(_gzip_bomb(30_000_000)))
        body = b"<html><body><p>" + b"x" * 8_000_000 + b"</p></body></html>"
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"},
                              stream=httpx.ByteStream(gzip.compress(body)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = BuiltinReader(client, resolver=_resolver({"a.test": ["93.184.216.34"]}))
    page = await r.fetch("https://a.test/p")
    from hub.reader import MAX_BYTES
    assert page.text and len(page.text) <= MAX_BYTES            # 解压后 8 MB → 截断到 3 MiB
    with pytest.raises(SourceError) as ei:
        await r.fetch_bytes("https://a.test/i.png", max_bytes=2_000_000, accept=("image/",))
    assert "过大" in ei.value.message
    await client.aclose()


# ---------------- F02：代理留空就直连；读取器不复用连接 ----------------
def test_f02_proxy_empty_ignores_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    h = Http(proxy="")
    assert h._client._trust_env is False                       # noqa: SLF001
    assert all(not getattr(t, "_proxy_url", None) for t in h._client._mounts.values() if t)  # noqa: SLF001
    r = BuiltinReader.create(proxy="")
    assert r._client._trust_env is False and r._pin_ip        # noqa: SLF001
    pool = r._client._transport._pool                          # noqa: SLF001
    assert pool._max_keepalive_connections == 0               # noqa: SLF001
    r2 = BuiltinReader.create(proxy="http://127.0.0.1:7890")
    assert not r2._pin_ip                                      # noqa: SLF001 - 走代理时按域名发出，证书按域名校验


async def test_f02_reader_via_proxy_sends_hostname_not_ip():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = BuiltinReader(client, resolver=_resolver({"a.test": ["93.184.216.34"]}), pin_ip=False)
    await r.fetch("https://a.test/p#frag")
    assert seen == [("https://a.test/p", None)]
    with pytest.raises(SourceError):          # 仍然先做公网校验
        await BuiltinReader(client, resolver=_resolver({"b.test": ["10.0.0.1"]}), pin_ip=False).fetch("https://b.test/")
    await client.aclose()


# ---------------- F03：key 不进错误文字和日志 ----------------
def test_f03_redact_and_log_filter(caplog):
    set_secrets(["sk-SECRET123456"])
    assert "sk-SECRET123456" not in redact("GET https://saucenao.com/search.php?api_key=sk-SECRET123456&db=999")
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert "***" in redact("raw sk-SECRET123456 in text")
    from hub.net import install_log_filter
    install_log_filter()
    lg = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        lg.info('HTTP Request: %s %s "%s"', "GET", "https://saucenao.com/search.php?api_key=sk-SECRET123456", "HTTP/1.1 200 OK")
    assert caplog.records and "sk-SECRET123456" not in caplog.records[-1].getMessage()
    set_secrets([])


# ---------------- F04：卡片里有可引用的完整链接 ----------------
def test_f04_card_has_url():
    led = Ledger(None)
    job = Job("J1", "s", "q", "quick", "auto", "any", "planner")
    job.result = RunResult(rrf_merge([("a", "a", 1, [Hit("Go 1.26", "https://go.dev/doc/go1.26?utm_source=x&v=1#a", "s")])]),
                           [], [], ["keenable"])
    job.entries = led.record("s", job.result.results)
    text = cards.sync_result(job)
    assert "https://go.dev/doc/go1.26?v=1" in text and "utm_source" not in text and "#a" not in text
    long = cards._link("https://a.test/" + "p" * 400)
    assert long.startswith("https://a.test/") and "已截短" in long


# ---------------- F05：超时交付已拿到的部分结果；异常进入终态 ----------------
async def test_f05_deep_timeout_keeps_partial():
    led = Ledger(None)
    m = JobManager(led, merge_window=0.05)
    m.job_timeout = 0.5
    fast = Fake("keenable", hits=hits("a", 3))
    slow = Fake("tavily", hits=hits("b", 3), delay=5)
    h = Health()
    j = Job(led.next_job_id(), "s1", "python 最新 版本", "deep", "auto", "any", "planner")
    await m.start(j, make_plan([fast, slow], h, depth="deep"), Executor(h), window=3)
    assert j.status == "timed_out" and j.result and j.result.used_sources == ["keenable"]
    assert j.entries and not j.result.complete and "部分结果" in j.result.error
    await m.shutdown()


async def test_f05_unexpected_exception_reaches_terminal_state():
    led = Ledger(None)
    m = JobManager(led, merge_window=0.05)

    class Boom(Executor):
        async def run(self, *a, **k):
            raise RuntimeError("bug")

    h = Health()
    j = Job(led.next_job_id(), "s1", "q", "quick", "auto", "any", "planner")
    await m.start(j, make_plan([Fake("keenable")], h, depth="quick"), Boom(h), window=1)
    assert j.status == "failed" and "内部错误" in j.result.error
    assert m.active("s1") == []
    await m.shutdown()


async def test_f05_cancel_keeps_partial_and_tails_are_managed():
    led = Ledger(None)
    m = JobManager(led, merge_window=0.05)
    h = Health()
    fast = Fake("keenable", hits=hits("a", 3))
    slow = Fake("tavily", hits=hits("b", 3), delay=5)
    j = Job(led.next_job_id(), "s1", "python 最新 版本", "deep", "auto", "any", "planner")
    await m.start(j, make_plan([fast, slow], h, depth="deep"), Executor(h), window=0.2)
    await asyncio.sleep(0.1)
    j.task.cancel()
    await asyncio.gather(j.task, return_exceptions=True)
    assert j.status == "cancelled" and j.entries                  # “已拿到的结果仍保留在记录里”
    await m.shutdown()


def test_f05_bad_port_does_not_crash_merge():
    assert normalize_url("https://a.test:99999/x")
    merged = rrf_merge([("a", "a", 1, [Hit("t", "https://a.test:99999/x", "s"), Hit("t2", "https://b.test/", "s")])])
    assert len(merged) == 2


# ---------------- F06：deep 不绕过 read.enabled ----------------
async def test_f06_enrich_respects_read_enabled(tmp_path):
    from test_plugin import FakeChain
    import plugin as P

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    chain = FakeChain()
    p._reader = chain
    p.config.read.enabled = False
    job = Job("J1", "s", "q", "deep", "auto", "any", "planner")
    job.result = RunResult(rrf_merge([("a", "a", 1, hits("a", 3))]), [], [], ["keenable"])
    job.entries = p.ledger.record("s", job.result.results)
    await p._enrich(job)
    assert chain.urls == []
    await p.on_unload()


# ---------------- F07/F10：清空与编号 ----------------
def test_f10_sn_not_reused_after_eviction_or_clear(tmp_path):
    led = Ledger(tmp_path / "h.sqlite3", per_session=20)
    for i in range(25):
        led.record("s", rrf_merge([("a", "a", 1, [Hit(f"t{i}", f"https://x{i}.test/", "s")])]))
    assert led.get("s", 1) is None                              # 最旧的被淘汰
    e = led.record("s", rrf_merge([("a", "a", 1, [Hit("new", "https://new.test/", "s")])]))
    assert e[0].sn == 26                                       # 不复用被淘汰的编号
    led.clear_all()
    e2 = led.record("s", rrf_merge([("a", "a", 1, [Hit("after", "https://after.test/", "s")])]))
    assert e2[0].sn == 27                                      # 清空后也不从 1 重来，旧对话里的 S1 不会指到新网址
    led.close()


def test_f10_old_db_counter_starts_from_max(tmp_path):
    led = Ledger(tmp_path / "h.sqlite3")
    led.record("s", rrf_merge([("a", "a", 1, hits("a", 3))]))
    led._db.execute("DELETE FROM counters WHERE name LIKE 'sn:%'")   # noqa: SLF001 - 模拟 0.5.1 的旧库
    led._db.commit()                                                 # noqa: SLF001
    e = led.record("s", rrf_merge([("a", "a", 1, [Hit("n", "https://n.test/", "s")])]))
    assert e[0].sn == 4
    led.close()


# ---------------- F08：付费主源在跑时不启动第二个付费来源 ----------------
async def test_f08_no_paid_hedge_while_paid_primary_running():
    h = Health()
    paid = Fake("exa", hits=hits("a", 3), delay=3.0, zmc=False)
    free = Fake("keenable", hits=hits("b", 1), delay=0.05)            # 免费对冲先回来，但不足
    paid2 = Fake("serper", hits=hits("c", 3), zmc=False)
    plan = make_plan([paid, free, paid2], h, depth="normal")
    plan.primary, plan.hedge_pool = [paid], [free, paid2]
    await Executor(h).run(plan, "python 最新 版本")
    assert paid2.calls == 0


# ---------------- F09：子代理次数上限 ----------------
async def test_f09_req_key_ignores_built_message_count():
    import plugin as P
    before = dict(session_id="s", selected_history_count=0, built_message_count=1, selection_reason="sub")
    after = dict(before, built_message_count=3)                       # 本插件插了进度消息
    assert P._req_key(before) == P._req_key(after)


# ---------------- F11/K1：改名后的工具、提醒误删、子代理不隐藏 ----------------
REM = ("<system-reminder>\n以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：\n"
       "1. plugin_google_search_web_search: 谷歌\n2. mute_user: 禁言\n\n"
       "如需其中某个工具，请先调用 tool_search。tool_search 只负责发现工具，不直接执行。\n</system-reminder>")


def _u(t):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": t}]}


def _t(n):
    return {"type": "function", "function": {"name": n, "description": "d", "parameters": {"type": "object"}}}


def test_k1_renamed_tool_recognized():
    assert original_name("plugin_google_search_web_search") == "web_search"
    assert original_name("mcp_searx_search_web") == "search_web"
    assert original_name("web_search") == "web_search" and original_name("mute_user") == "mute_user"
    p = CoexistPolicy(host_mode="hide", other_mode="observe")
    assert p.mode_for("web_search") == "hide"                         # Host 自带的（原名）
    assert p.mode_for("plugin_google_search_web_search") == "observe"  # 插件同名工具：按“其他插件”处理
    p2 = CoexistPolicy(host_mode="off", other_mode="hide")
    defs, hidden = hide_tools([_t("reply"), _t("plugin_google_search_web_search")], p2)
    assert hidden == ["plugin_google_search_web_search"]
    items, removed = strip_deferred_reminder([_u(REM)], lambda n: p2.mode_for(n) == "hide")
    assert removed == ["plugin_google_search_web_search"] and "1. mute_user: 禁言" in items[0]["parts"][0]["text"]


def test_f11_user_quote_of_reminder_not_touched():
    quoted = "你看这个：<system-reminder>\n1. web_search: x\n</system-reminder> 是什么意思？tool_search"
    items, removed = strip_deferred_reminder([_u(quoted)], ["web_search"])
    assert removed == [] and items[0]["parts"][0]["text"] == quoted
    tampered = REM.replace("2. mute_user: 禁言", "随便一行")
    items, removed = strip_deferred_reminder([_u(tampered)], ["plugin_google_search_web_search"])
    assert removed == []                                                # 格式对不上：原样保留


async def test_f11_subagent_tools_not_hidden(tmp_path):
    import plugin as P

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    out = await p.hub_context(items=[], tool_definitions=[_t("search"), _t("web_fetch")], session_id="s")
    names = [d["function"]["name"] for d in (out.get("modified_kwargs") or {}).get("tool_definitions", [_t("search"), _t("web_fetch")])]
    assert "web_fetch" in names                                          # 子代理：主 Planner 授权的工具不隐藏
    await p.on_unload()


# ---------------- F12：候选图解码有像素上限 ----------------
def test_f12_candidate_pixel_cap():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (4000, 4001)).save(buf, "PNG")
    assert dhash_bytes(buf.getvalue()) is None
    small = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(small, "PNG")
    assert isinstance(dhash_bytes(small.getvalue()), int)
    assert dhash_bytes(b"BM" + b"\0" * 100) is None                     # 非白名单格式


# ---------------- F14：未来日期不算新内容 ----------------
def test_f14_future_date_not_fresh():
    far = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 10 * 86400))
    m = rrf_merge([("a", "a", 1, [Hit(f"python 最新 版本 {i}", f"https://x{i}.test/", "python 最新 版本", far)
                                  for i in range(5)])])
    assert "fresh" in judge("python 最新 版本", m, depth="normal", freshness="recent", intent="auto")


# ---------------- H01：组播、NAT64 不是公网目标 ----------------
@pytest.mark.parametrize("ip", ["224.0.0.1", "239.1.2.3", "ff02::1", "64:ff9b::a00:1"])
async def test_h01_non_unicast_rejected(ip):
    with pytest.raises(SourceError):
        await validate_public_url("http://a.test/", _resolver({"a.test": [ip]}))


async def test_f09_passive_replyer_planner_detected(tmp_path):
    """passive_replyer 开启时主 Planner 没有 reply，但仍有 wait/tool_search/subagent。"""
    import plugin as P

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    await p.hub_context(items=[], tool_definitions=[_t("stop"), _t("wait"), _t("tool_search"), _t("search")], session_id="s")
    assert p._caller_hint["s"] == "planner"
    await p.hub_context(items=[], tool_definitions=[_t("stop"), _t("search")], session_id="s2")
    assert p._caller_hint["s2"] == "subagent"
    await p.on_unload()


# ---------------- 0.5.3：生产运行后发现的问题 ----------------
async def test_053_budget_skip_is_not_failure():
    """normal 档第 3 个付费来源被本次预算挡下：结果卡不应写“部分来源失败”，健康统计也不计失败。"""
    from hub.budget import Budget
    counters: dict[str, int] = {}
    budget = Budget(lambda k: counters.get(k, 0), lambda k, n: counters.__setitem__(k, counters.get(k, 0) + n))
    h = Health()
    a = Fake("baidu", hits=[Hit(f"python 最新 版本 {i}", f"https://a{i}.test/", "python 最新 版本") for i in range(5)],
             zmc=False)
    b = Fake("exa", hits=[Hit("python 最新 版本 x", "https://b.test/", "python 最新 版本")], zmc=False)
    c = Fake("tavily", hits=hits("c", 3), zmc=False)
    for f in (a, b, c):
        f.info.paid = True
    plan = make_plan([a, b, c], h, depth="normal")
    plan.primary, plan.hedge_pool = [a, b], [c]
    r = await Executor(h, budget=budget).run(plan, "python 最新 版本")
    assert c.calls == 0 and r.complete
    assert h.snapshot().get("tavily", {}).get("fail", 0) == 0


def test_053_health_reset():
    h = Health()
    h.failure("keenable", SourceError(Outcome.REJECTED, "x", status=401, auth_failed=True))
    assert not h.available("keenable")
    h.reset("keenable")
    assert h.available("keenable")


async def test_053_read_source_param_aliases(tmp_path):
    import plugin as P

    p = P.SearchHubPlugin()
    seen = []

    async def fake_impl(ref="", focus="", **kw):
        seen.append((ref, focus))
        return {"success": True, "content": "ok"}

    p._read_impl = fake_impl  # type: ignore[method-assign]
    await p.read_source(source="https://a.test/x")
    await p.read_source(url="https://b.test/", query="日期")
    await p.read_source(sn=3)
    await p.read_source(ref="S2", source="https://ignored.test/")
    assert seen == [("https://a.test/x", ""), ("https://b.test/", "日期"), ("S3", ""), ("S2", "")]


def _auth_err(P):
    """用插件自己加载的模块构造错误（测试里 hub.net 与插件的 hubplugin.hub.net 是两个模块对象，枚举不相等）。"""
    import sys
    net = sys.modules[P.SourceError.__module__]
    return net.SourceError(net.Outcome.REJECTED, "x", status=401, auth_failed=True)


async def test_053_key_change_clears_cooldown(tmp_path):
    import plugin as P

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    cfg = P.HubConfig()
    cfg.sources.keenable.api_key = "wrong-value-from-autofill"
    p._plugin_config_instance = cfg  # type: ignore[attr-defined]
    await p.on_load()
    p.health.failure("keenable", _auth_err(P))
    assert not p.health.available("keenable")
    cfg.sources.keenable.api_key = "keen_correct"
    p._apply_config()
    assert p.health.available("keenable")
    await p.on_unload()
    # 停机期间换 key：重启时也会解除（指纹存在账本里）
    p2 = P.SearchHubPlugin()
    p2._ctx = ctx  # type: ignore[attr-defined]
    cfg2 = P.HubConfig()
    cfg2.sources.keenable.api_key = "keen_correct"
    p2._plugin_config_instance = cfg2  # type: ignore[attr-defined]
    await p2.on_load()
    p2.health.failure("keenable", _auth_err(P))
    await p2.on_unload()
    p3 = P.SearchHubPlugin()
    p3._ctx = ctx  # type: ignore[attr-defined]
    cfg3 = P.HubConfig()
    cfg3.sources.keenable.api_key = "keen_newer"
    p3._plugin_config_instance = cfg3  # type: ignore[attr-defined]
    await p3.on_load()
    assert p3.health.available("keenable")
    await p3.on_unload()


async def test_053_evidence_marks_used(tmp_path):
    import plugin as P

    p = P.SearchHubPlugin()
    ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=None, maisaka=None,
                          logger=SimpleNamespace(warning=lambda *a, **k: None))
    p._ctx = ctx  # type: ignore[attr-defined]
    type(p).ctx = property(lambda self: self._ctx)  # type: ignore[assignment]
    p._plugin_config_instance = P.HubConfig()  # type: ignore[attr-defined]
    await p.on_load()
    p.ledger.record("s", rrf_merge([("a", "a", 1, hits("a", 2))]))
    items = [_u("hi"), _u("当前时间：x")]
    await p.hub_evidence(items=items, session_id="s")
    used = p.ledger._db.execute("SELECT SUM(used) FROM entries WHERE session_id='s'").fetchone()[0]  # noqa: SLF001
    assert used == 2
    await p.on_unload()
