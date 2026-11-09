"""0.6.1 第三次复核（R63）的回归测试。合成数据，不联网。"""

import asyncio
import io
import time
from datetime import datetime, timezone

import httpx
import pytest
from PIL import Image

from hub import verify
from hub.net import Outcome, SourceError
from hub.pace import SitePacer

from test_plugin import FakeChain, FakeProvider, _grant, plug  # noqa: F401
from test_verify import FakeLLM


# ---------------- R63-01 摘录 ----------------
def test_r63_01_answer_after_hit_kept():
    """审查者的形状：命中词正好落在一块的末尾，答案命令在下一块——0.6.1 复审修复后仍会把命令截掉。"""
    from hub.reader import excerpt
    head = "Nginx reload 的正确配置方法是"
    body = "背" * (500 - len(head)) + head + "执行 nginx -s reload 即可生效" + "后" * 1700
    t, trunc = excerpt(body, "Nginx reload", 500)
    assert "nginx -s reload" in t and len(t) <= 500 and trunc
    body2 = ("这是部署背景，介绍服务器环境和历史情况" * 24 + head + "执行 nginx -s reload 即可生效。" + "后面是无关的文字内容" * 90)
    assert "nginx -s reload" in excerpt(body2, "Nginx reload", 500)[0]


def test_r63_01_no_hit_returns_head_not_empty():
    from hub.reader import excerpt
    body = "本文介绍服务器部署时常见的配置项和注意事项。" * 40
    t, trunc = excerpt(body, "我想知道怎么让改动马上生效", 500)
    assert t and body.startswith(t[:20]) and trunc


def test_r63_01_short_text_unchanged():
    from hub.reader import excerpt
    assert excerpt("Nginx reload：执行 nginx -s reload。", "Nginx reload", 500) == ("Nginx reload：执行 nginx -s reload。", False)


def test_r63_01_longer_but_unrelated_excerpt_does_not_replace_snippet():
    import plugin as P
    assert not P._covers("完全无关的一大段文字" * 20, "Nginx reload 正确方法", "Nginx reload")
    assert P._covers("EDG 分部重组 正文内容" * 20, "EDG 分裂 摘要", "EDG 分裂")       # 换了说法但还有关注词


# ---------------- R63-02 读原文中途取消 ----------------
class GoneThenHang(FakeChain):
    def __init__(self):
        super().__init__()
        self.seen = asyncio.Event()

    async def read(self, url):
        from hub.reader import PageGone
        if "n1." in url:
            self.seen.set()
            raise PageGone(Outcome.REJECTED, "网页不存在或已被删除（HTTP 404/410）", status=404)
        await asyncio.sleep(30)


class Five(FakeProvider):
    async def search(self, q, *, max_results):
        from hub.providers import Hit
        return [Hit(f"EDG 分裂 新闻{i}", f"https://n{i}.example.com/a", f"EDG 分裂 摘要{i}", "2026-11-05")
                for i in range(1, 6)]


@pytest.mark.parametrize("how", ["cancel", "shutdown"])
async def test_r63_02_404_read_before_cancel_is_written(plug, how):
    from hub import cards
    plug._providers = [Five()]
    rd = GoneThenHang()
    plug._reader = rd
    plug.config.search.sync_window_seconds = 1.0
    res = await plug.search(**await _grant(plug, args={"query": "EDG 分裂", "depth": "normal"}))
    assert "后台" in res["content"]
    await asyncio.wait_for(rd.seen.wait(), 3)
    await asyncio.sleep(0.05)
    jid = plug.jobs.active("sess-A")[0].job_id
    if how == "cancel":
        plug.jobs.cancel("sess-A", jid)
        await asyncio.sleep(0.1)
    else:
        await plug.jobs.shutdown()
    v = dict(plug.ledger._db.execute("SELECT url, verdict FROM entries WHERE session_id='sess-A'").fetchall())
    assert v["https://n1.example.com/a"] == "invalid"
    assert "n1.example.com" not in cards.evidence_block(plug.ledger.recent("sess-A", 0))


# ---------------- R63-03 英文日期 ----------------
@pytest.mark.parametrize("text,want", [
    ("The version was published on 2024-04-24 and this article is updated as its status changes.", None),
    ("It was updated on 2024-04-24 to fix a bug.", None),
    ("该版本发布于 2024-04-24，本文持续维护。", None),
    ("Published: 2024-04-24\nbody", "2024-04-24"),
    ("Updated on 2024-04-24\nbody", "2024-04-24"),
    ("By Jane · Published on 2024-04-24 · 3 min read", "2024-04-24"),
    ("发布时间：2024-04-24", "2024-04-24"),
])
def test_r63_03_only_metadata_dates_are_strict(text, want):
    assert verify.page_date(text) == want


def test_r63_03_sentence_does_not_override_model_direct():
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    need = verify.time_need("Node.js 最新状态", "", "recent", "auto", now)
    body = "The version was published on 2024-04-24 and this article is updated as its status changes. " * 3
    rule = verify.rule_check("Node.js status", need, title="Node.js", snippet="s", body=body, published_at=None, now=now)
    merged = verify.merge_checks({1: rule}, {1: verify.Check(verify.DIRECT, "当前状态", "model")})
    assert merged[1].verdict == verify.DIRECT


# ---------------- R63-04 共享任务被取消 ----------------
async def test_r63_04_waiter_gets_none_when_shared_task_cancelled():
    from hub.jobs import Job, JobManager
    from hub.ledger import Ledger
    jm = JobManager(Ledger(None))
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return "x"
    job = Job("J1", "s", "q", "quick", "find_image", "any", "planner")
    owner = asyncio.create_task(jm.run_tracked(job, work))
    await asyncio.sleep(0.02)
    waiter = asyncio.create_task(jm.wait_tracked(job, 5))
    await asyncio.sleep(0.02)
    jm.cancel("s", "J1")
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await waiter is None                         # 受控返回，不跟着抛取消


async def test_r63_04_waiter_own_cancel_still_propagates():
    from hub.jobs import Job, JobManager
    from hub.ledger import Ledger
    jm = JobManager(Ledger(None))
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return "x"
    job = Job("J1", "s", "q", "quick", "find_image", "any", "planner")
    owner = asyncio.create_task(jm.run_tracked(job, work))
    await asyncio.sleep(0.02)
    waiter = asyncio.create_task(jm.wait_tracked(job, 5))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert await owner == "x"                           # 原任务不受影响


async def test_r63_04_plugin_waiter_returns_controlled_error(plug):
    from test_image_find import Reader, http
    plug.config.searxng.base_url = "http://searx.local"
    plug._apply_config()
    plug._http = http()
    gate = asyncio.Event()

    class Gate(Reader):
        async def fetch_bytes(self, url, *, max_bytes, accept):
            await gate.wait()
            return await super().fetch_bytes(url, max_bytes=max_bytes, accept=accept)
    plug._builtin = Gate()
    a = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    b = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    plug.jobs.cancel("sess-A", plug.jobs.active("sess-A")[0].job_id)
    ra, rb = await a, await b
    assert not ra["success"] and "已取消" in ra["error"]
    assert not rb["success"] and "没有重复发起" in rb["error"]


# ---------------- R63-05 / 07 SitePacer ----------------
async def test_r63_05_reclaim_keeps_waiters_and_pending_gap():
    sp = SitePacer(gap=0.3, jitter=0.0)
    sp.MAX_SITES = 4
    for i in range(6):
        async with sp.slot(f"https://s{i}.test/x", time.monotonic() + 5):
            pass
    async with sp.slot("https://a.example.com/1", time.monotonic() + 5):
        pass
    t0 = time.monotonic()
    for i in range(6, 12):                   # 触发回收；a.example.com 的间隔还没过
        async with sp.slot(f"https://s{i}.test/x", time.monotonic() + 5):
            pass
    async with sp.slot("https://a.example.com/2", time.monotonic() + 5) as ok:
        assert ok
        assert time.monotonic() - t0 >= 0.25          # 间隔没有因为回收丢掉


async def test_r63_05_same_site_never_two_holders_under_reclaim():
    sp = SitePacer(gap=0.0, jitter=0.0)
    sp.MAX_SITES = 2
    active, peak = 0, 0

    async def use(u):
        nonlocal active, peak
        async with sp.slot(u, time.monotonic() + 5) as ok:
            assert ok
            if "same" in u:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.02)
            if "same" in u:
                active -= 1
    urls = [f"https://same.test/{i}" for i in range(5)] + [f"https://o{i}.test/" for i in range(10)]
    await asyncio.gather(*(use(u) for u in urls))
    assert peak == 1


async def test_r63_07_past_deadline_not_allowed():
    sp = SitePacer(gap=0.0, jitter=0.0)
    async with sp.slot("https://x.test/a", time.monotonic() - 0.1) as ok:
        assert ok is False


# ---------------- R63-06 重定向汇合到同一网站 ----------------
async def test_r63_06_redirects_to_same_site_are_serialized():
    from hub.reader import BuiltinReader
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), "red").save(buf, "PNG")
    png = buf.getvalue()
    active, peak = 0, 0

    async def handler(req):
        nonlocal active, peak
        host = req.headers.get("host", req.url.host)
        if host in ("r0.test", "r1.test"):
            return httpx.Response(302, headers={"location": "https://cdn.target.test/img.png"})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            return await handler(req)

    async def resolver(host, port):
        return ["93.184.216.34"]
    rd = BuiltinReader(httpx.AsyncClient(transport=T()), resolver=resolver, pin_ip=False)
    sp = SitePacer(gap=0.0, jitter=0.0)
    from hub.image_find import paced_fetch
    dl = time.monotonic() + 5
    await asyncio.gather(paced_fetch(rd, sp, "https://r0.test/a.png", dl, 1 << 20),
                         paced_fetch(rd, sp, "https://r1.test/b.png", dl, 1 << 20))
    assert peak == 1


# ---------------- R63-08 卸载不标已送回 ----------------
async def test_r63_08_unload_interrupted_find_not_marked_delivered():
    from hub.jobs import Job, JobManager
    from hub.ledger import Ledger
    led = Ledger(None)
    jm = JobManager(led)

    async def work():
        await asyncio.sleep(30)
    job = Job("J1", "s", "q", "quick", "find_image", "any", "planner")
    t = asyncio.create_task(jm.run_tracked(job, work))
    await asyncio.sleep(0.02)
    await jm.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert job.status == "interrupted"
    assert led.interrupted_undelivered("s") == ["J1"]


# ---------------- R63-09 清理总时限 ----------------
async def test_r63_09_cleanup_total_bound_on_cancel():
    from test_image_more import Camo
    from hub.image_more import CamofoxLensProvider
    cam = Camo()
    in_delete = asyncio.Event()

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            await req.aread()
            if req.method == "DELETE":
                in_delete.set()
                await asyncio.sleep(10)
            return cam(req)
    p = CamofoxLensProvider("http://camo:9377", transport=T())
    p.POLL = p.OPEN_WAIT = p.POLL_JITTER = 0.0
    p.CLEANUP_TIMEOUT = 0.6
    t = asyncio.create_task(p.search_image(b"\xff\xd8x"))
    await asyncio.wait_for(in_delete.wait(), 3)
    t0 = time.monotonic()
    await asyncio.sleep(0.4)                 # 清理已经过了 0.4 秒
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert time.monotonic() - t0 < 0.8       # 原来会再给 0.6 秒，共约 1.0 秒
