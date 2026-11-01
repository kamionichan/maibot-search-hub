import asyncio
import json
import time

import httpx
import pytest

from hub.executor import Executor, make_plan
from hub.grants import GrantBook
from hub.health import Health
from hub.jobs import Job, JobManager
from hub.ledger import Ledger
from hub.merge import judge, normalize_url, rrf_merge
from hub.net import Http, Outcome, SourceError
from hub.providers import Hit, KeenableProvider, ProviderInfo, TavilyProvider, classify_host_error
from hub import cards


# ---------------- 假来源 ----------------
class Fake:
    def __init__(self, name, hits=None, delay=0.0, error=None, family=None, zmc=True):
        self.info = ProviderInfo(name=name, family=family or name, latency="fast", paid=False,
                                 zero_marginal_cost=zmc, has_dates=False)
        self.hits = hits or []
        self.delay = delay
        self.error = error
        self.calls = 0

    async def search(self, query, *, max_results):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.hits)


def hits(prefix, n, domain=None):
    return [Hit(f"{prefix} python 最新 版本 {i}", f"https://{domain or prefix + str(i)}.example.com/p{i}",
                f"关于 python 最新 版本 的内容 {i}") for i in range(n)]


# ---------------- 授权 ----------------
def test_grant_one_shot_and_tool_bound():
    g = GrantBook()
    t = g.issue(session_id="s1", logical_turn_id="t", tool="search", caller="planner")
    assert g.consume(t, "other") is None          # 工具不符：作废
    t = g.issue(session_id="s1", logical_turn_id="t", tool="search", caller="planner")
    got = g.consume(t, "search")
    assert got and got.session_id == "s1"
    assert g.consume(t, "search") is None         # 一次性
    assert g.consume(None, "search") is None
    assert g.consume("forged", "search") is None


def test_grant_expiry():
    g = GrantBook(ttl=0.01)
    t = g.issue(session_id="s", logical_turn_id="", tool="search", caller="planner")
    time.sleep(0.03)
    assert g.consume(t, "search") is None


# ---------------- 网络分类 ----------------
def _http(handler):
    return Http(transport=httpx.MockTransport(handler))


async def test_classify_4xx_429_5xx_and_connect():
    async def run(handler):
        http = _http(handler)
        try:
            await http.post_json("https://x.test/a", {})
        except SourceError as e:
            return e
        finally:
            await http.aclose()

    e = await run(lambda r: httpx.Response(429, headers={"retry-after": "30"}))
    assert e.outcome is Outcome.REJECTED and e.retry_after == 30
    e = await run(lambda r: httpx.Response(401))
    assert e.outcome is Outcome.REJECTED and e.auth_failed
    e = await run(lambda r: httpx.Response(502))
    assert e.outcome is Outcome.UNKNOWN

    def boom(r):
        raise httpx.ConnectError("refused")
    e = await run(boom)
    assert e.outcome is Outcome.NOT_SENT

    def rt(r):
        raise httpx.ReadTimeout("slow")
    e = await run(rt)
    assert e.outcome is Outcome.UNKNOWN

    e = await run(lambda r: httpx.Response(200, content=b"not json"))
    assert e.outcome is Outcome.UNKNOWN


def test_host_error_classification():
    assert classify_host_error("联网服务返回 HTTP 429").outcome is Outcome.REJECTED
    assert classify_host_error("联网服务返回 HTTP 503").outcome is Outcome.UNKNOWN
    assert classify_host_error("联网服务请求超时").outcome is Outcome.UNKNOWN
    assert classify_host_error("联网服务连接失败：ConnectError").outcome is Outcome.NOT_SENT
    assert classify_host_error("莫名其妙").outcome is Outcome.UNKNOWN


async def test_keenable_and_tavily_parsing():
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if req.url.host == "api.keenable.ai":
            assert req.url.path == "/v1/search/public"
            assert req.headers["x-keenable-title"]
            return httpx.Response(200, json={"results": [
                {"title": "Python", "url": "https://python.org/downloads", "snippet": "3.15"},
                {"title": "bad", "url": "javascript:alert(1)"}]})
        assert req.headers["x-tavily-access-mode"] == "keyless"
        assert body["include_published_date"] is True
        return httpx.Response(200, json={"results": [
            {"title": "T", "url": "https://t.example/a", "content": "c", "published_date": "2026-10-01"}]})

    http = _http(handler)
    k = await KeenableProvider(http).search("python", max_results=3)
    assert [h.url for h in k] == ["https://python.org/downloads"]
    t = await TavilyProvider(http).search("python", max_results=3)
    assert t[0].published_at == "2026-10-01"
    await http.aclose()


async def test_tavily_keyless_limit_text_is_rejected():
    http = _http(lambda r: httpx.Response(200, json={"message": "You have hit the keyless limit..."}))
    with pytest.raises(SourceError) as ei:
        await TavilyProvider(http).search("x", max_results=3)
    assert ei.value.outcome is Outcome.REJECTED
    await http.aclose()


# ---------------- 合并与判定 ----------------
def test_normalize_url_and_rrf_family_dedup():
    assert normalize_url("http://www.A.com/x/?utm_source=1&b=2") == normalize_url("https://a.com/x?b=2")
    same = [Hit("t", "https://a.com/x", "s")]
    m = rrf_merge([("tavily", "tavily", 1.0, same), ("host_web", "tavily", 1.0, same)])
    assert len(m) == 1 and m[0].sources == ["tavily", "host_web"]
    one_family = m[0].score
    m2 = rrf_merge([("tavily", "tavily", 1.0, same), ("keenable", "keenable", 1.0, same)])
    assert m2[0].score > one_family   # 不同族才算多源印证


def test_judge_gaps():
    good = rrf_merge([("a", "a", 1, hits("a", 3)), ("b", "b", 1, hits("b", 2))])
    assert judge("python 最新版本", good, depth="normal", freshness="any", intent="auto") == []
    few = rrf_merge([("a", "a", 1, hits("a", 1))])
    assert "few" in judge("python 最新版本", few, depth="normal", freshness="any", intent="auto")
    same_site = rrf_merge([("a", "a", 1, hits("a", 4, domain="one"))])
    assert "diversity" in judge("python 最新版本", same_site, depth="normal", freshness="any", intent="auto")
    off = rrf_merge([("a", "a", 1, [Hit(f"汇率 {i}", f"https://x{i}.com", "美元") for i in range(4)])])
    assert "relevance" in judge("python 最新版本", off, depth="normal", freshness="any", intent="auto")
    assert "fresh" in judge("python 最新版本", good, depth="normal", freshness="recent", intent="auto")


# ---------------- 执行器 ----------------
async def test_quick_falls_back_once_on_failure():
    h = Health()
    a = Fake("keenable", error=SourceError(Outcome.REJECTED, "429", status=429))
    b = Fake("tavily", hits=hits("b", 3))
    plan = make_plan([a, b], h, depth="quick")
    r = await Executor(h).run(plan, "python 最新版本")
    assert r.used_sources == ["tavily"] and a.calls == 1


async def test_unknown_never_retries_same_source_but_not_sent_retries_once():
    h = Health()
    u = Fake("keenable", error=SourceError(Outcome.UNKNOWN, "ReadTimeout"))
    plan = make_plan([u], h, depth="quick")
    r = await Executor(h).run(plan, "q")
    assert u.calls == 1 and r.error_kind == "unknown" and not r.used_sources
    h2 = Health()
    n = Fake("keenable", error=SourceError(Outcome.NOT_SENT, "ConnectError"))
    r = await Executor(h2).run(make_plan([n], h2, depth="quick"), "q")
    assert n.calls == 2 and r.error_kind == "not_sent"


async def test_normal_hedges_when_primary_slow():
    h = Health()
    slow = Fake("keenable", hits=hits("a", 3), delay=3.0)   # 冷启动 P90=2s
    fast = Fake("tavily", hits=hits("b", 3), delay=0.05)
    t0 = time.monotonic()
    r = await Executor(h).run(make_plan([slow, fast], h, depth="normal"), "python 最新版本")
    assert time.monotonic() - t0 < 2.8
    assert "tavily" in r.used_sources


async def test_normal_hedges_when_insufficient():
    h = Health()
    thin = Fake("keenable", hits=hits("a", 1))
    more = Fake("tavily", hits=hits("b", 3))
    r = await Executor(h).run(make_plan([thin, more], h, depth="normal"), "python 最新版本")
    assert set(r.used_sources) == {"keenable", "tavily"} and r.missing == []


async def test_named_provider_unavailable_errors_instead_of_switching():
    h = Health()
    a = Fake("keenable", hits=hits("a", 3))
    with pytest.raises(SourceError):
        make_plan([a], h, depth="normal", provider="tavily")


async def test_family_dedup_prefers_plugin_source():
    h = Health()
    host = Fake("host_web", family="tavily")
    tav = Fake("tavily", hits=hits("b", 3))
    plan = make_plan([host, tav], h, depth="deep")
    assert [p.info.name for p in plan.primary] == ["tavily"]


async def test_cooldown_after_429():
    h = Health()
    a = Fake("keenable", error=SourceError(Outcome.REJECTED, "429", status=429))
    await Executor(h).run(make_plan([a], h, depth="quick"), "q")
    assert not h.available("keenable")


# ---------------- 任务：同步窗口与原子归属 ----------------
async def _mgr():
    led = Ledger(None)
    m = JobManager(led, merge_window=0.05)
    delivered = []

    async def deliver(sid, jobs):
        delivered.append((sid, [j.job_id for j in jobs]))
        return True
    m.deliver = deliver
    return led, m, delivered


async def test_sync_when_fast_and_no_delivery():
    led, m, delivered = await _mgr()
    h = Health()
    p = Fake("keenable", hits=hits("a", 3))
    job = Job(led.next_job_id(), "s1", "python 最新版本", "quick", "auto", "any", "planner")
    await m.start(job, make_plan([p], h, depth="quick"), Executor(h), window=1.0)
    assert job.owner == "sync" and job.entries[0].sn == 1
    await asyncio.sleep(0.2)
    assert delivered == []
    assert "S1" in cards.sync_result(job)


async def test_background_when_slow_then_proactive_once():
    led, m, delivered = await _mgr()
    h = Health()
    p = Fake("keenable", hits=hits("a", 3), delay=0.3)
    job = Job(led.next_job_id(), "s1", "python 最新版本", "quick", "auto", "any", "planner")
    await m.start(job, make_plan([p], h, depth="quick"), Executor(h), window=0.05)
    assert job.owner == "background"
    assert "这不是“没查到”" in cards.backgrounded(job, "10 秒", "planner")
    await asyncio.sleep(0.6)
    assert delivered == [("s1", [job.job_id])]


async def test_two_background_jobs_merge_into_one_card():
    led, m, delivered = await _mgr()
    m.merge_window = 0.3
    h = Health()
    p = Fake("keenable", hits=hits("a", 3), delay=0.1)
    j1 = Job(led.next_job_id(), "s1", "q1", "quick", "auto", "any", "planner")
    j2 = Job(led.next_job_id(), "s1", "q2", "quick", "auto", "any", "planner")
    await m.start(j1, make_plan([p], h, depth="quick"), Executor(h), window=0.01)
    await m.start(j2, make_plan([p], h, depth="quick"), Executor(h), window=0.01)
    await asyncio.sleep(0.8)
    assert len(delivered) == 1 and set(delivered[0][1]) == {j1.job_id, j2.job_id}


async def test_stale_job_is_parked_not_delivered():
    led, m, delivered = await _mgr()
    m.stale_seconds = 0.0
    h = Health()
    p = Fake("keenable", hits=hits("a", 3), delay=0.1)
    j = Job(led.next_job_id(), "s1", "q", "quick", "auto", "any", "planner")
    await m.start(j, make_plan([p], h, depth="quick"), Executor(h), window=0.01)
    await asyncio.sleep(0.4)
    assert delivered == []


async def test_admission_limit_and_merge_same_query():
    led, m, _ = await _mgr()
    m.per_running, m.per_queue = 1, 0
    h = Health()
    p = Fake("keenable", hits=hits("a", 3), delay=0.5)
    j = Job(led.next_job_id(), "s1", "q", "quick", "auto", "any", "planner", norm_query="q")
    await m.start(j, make_plan([p], h, depth="quick"), Executor(h), window=0.01)
    same, reason = m.admit("s1", "q", "quick")
    assert same is j and reason == ""
    other, reason = m.admit("s1", "other", "quick")
    assert other is None and "已有 1 个搜索" in reason
    await m.shutdown()


async def test_shutdown_marks_interrupted():
    led, m, _ = await _mgr()
    h = Health()
    p = Fake("keenable", hits=hits("a", 3), delay=5)
    j = Job(led.next_job_id(), "s1", "q", "quick", "auto", "any", "planner")
    await m.start(j, make_plan([p], h, depth="quick"), Executor(h), window=0.01)
    await m.shutdown()
    rows = led.jobs("s1")
    assert rows[0]["status"] in ("interrupted", "cancelled")


def test_ledger_same_url_same_sn_and_cards_under_limit():
    led = Ledger(None)
    merged = rrf_merge([("a", "a", 1, hits("a", 5))])
    e1 = led.record("s", merged)
    e2 = led.record("s", merged)
    assert [e.sn for e in e1] == [e.sn for e in e2] and not e2[0].new
    assert led.record("other", merged)[0].sn == 1   # 会话之间编号独立
    job = Job("J9", "s", "q" * 500, "deep", "auto", "any", "planner")
    from hub.executor import RunResult
    long = [Hit("标题" * 100, f"https://x{i}.com", "片段" * 400) for i in range(10)]
    job.result = RunResult(rrf_merge([("a", "a", 1, long)]), ["few"], [], ["keenable"])
    job.entries = led.record("s", job.result.results)
    assert len(cards.sync_result(job)) <= 3000
    assert len(cards.result_card([job, job, job])) <= 3000


async def test_host_web_unconfigured_is_not_retried_and_cools_long():
    h = Health()
    hw = Fake("host_web", error=classify_host_error("统一联网功能未启用"))
    r = await Executor(h).run(make_plan([hw], h, depth="quick"), "q")
    assert hw.calls == 1 and not h.available("host_web") and r.error_kind == "not_sent"
    assert "未配置" in h.cooldown_reason("host_web")


def test_manifest_declares_needed_capabilities():
    """第 4 阶段实测：漏声明 maisaka.context.resolve_image，取图全部失败。用到的每个 ctx 能力都要在 manifest 里声明。"""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    caps = set(json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["capabilities"])
    src = (root / "plugin.py").read_text(encoding="utf-8") + "".join(
        p.read_text(encoding="utf-8") for p in (root / "hub").glob("*.py"))
    needed = {"maisaka.context.resolve_image": "resolve_image(", "maisaka.proactive.trigger": "trigger_proactive(",
              "send.text": "send.text(", "web.search": "web.search(", "web.fetch": "web.fetch(", "api.call": "api.call(",
              "component.list_loaded_plugins": "list_loaded_plugins("}
    for cap, marker in needed.items():
        if marker in src:
            assert cap in caps, cap
