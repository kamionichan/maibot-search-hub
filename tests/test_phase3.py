"""第 3 阶段：编排（排序、Agent 规则、付费不对冲）、预算、健康持久化。"""

import asyncio
import time

import pytest

from hub.budget import Budget
from hub.executor import Executor, is_chinese, make_plan
from hub.health import Health
from hub.net import Outcome, SourceError
from hub.providers import Hit, ProviderInfo


class P:
    def __init__(self, name, *, paid=False, family=None, agent=False, n=4, delay=0.0, error=None):
        self.info = ProviderInfo(name, family or name, "fast", paid=paid, zero_marginal_cost=not paid,
                                 has_dates=False, extra={"agent": True} if agent else {})
        self.n, self.delay, self.error, self.calls, self.freshness = n, delay, error, 0, None

    async def search(self, query, *, max_results, freshness="any"):
        self.calls += 1
        self.freshness = freshness
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [Hit(f"{query} {self.info.name} {i}", f"https://{self.info.name}{i}.example.com/x",
                    f"{query} 内容 {i}") for i in range(self.n)]


def budget(limits=None):
    store = {}
    return Budget(lambda k: store.get(k, 0), lambda k, n: store.__setitem__(k, store.get(k, 0) + n), limits), store


def names(plan):
    return [p.info.name for p in plan.primary], [p.info.name for p in plan.hedge_pool]


def test_language_ordering_and_family_dedup():
    ps = [P("keenable"), P("baidu", paid=True), P("exa", paid=True), P("host_web", family="exa")]
    zh = make_plan(ps, Health(), depth="normal", query="原神最新版本更新了什么")
    en = make_plan(ps, Health(), depth="normal", query="python latest release")
    assert names(zh)[0] == ["baidu"] and names(en)[0] == ["exa"]
    assert "host_web" not in names(en)[1]          # 与 exa 同族，被去重
    assert is_chinese("原神 6.2") and not is_chinese("rust 1.91 release notes")


def test_priority_overrides_default():
    ps = [P("keenable"), P("baidu", paid=True)]
    plan = make_plan(ps, Health(), depth="normal", query="原神", priority=["keenable"])
    assert names(plan)[0] == ["keenable"]


def test_agent_only_in_deep_or_discussion():
    ps = [P("keenable"), P("qwen", paid=True, agent=True)]
    assert "qwen" not in sum(names(make_plan(ps, Health(), depth="normal", query="原神")), [])
    assert "qwen" in sum(names(make_plan(ps, Health(), depth="deep", query="原神")), [])
    disc = make_plan(ps, Health(), depth="normal", query="原神", intent="discussion")
    assert names(disc)[0] == ["qwen"]
    # 点名可以绕过 deep 限制
    assert names(make_plan(ps, Health(), depth="quick", query="x", provider="qwen"))[0] == ["qwen"]


def test_daily_limit_excludes_source_and_named_errors():
    b, store = budget({"baidu": 1})
    store[b._key("baidu")] = 1
    ps = [P("keenable"), P("baidu", paid=True)]
    plan = make_plan(ps, Health(), depth="normal", query="原神", budget=b)
    assert names(plan)[0] == ["keenable"] and plan.skipped == [("baidu", "今日额度已用完")]
    with pytest.raises(SourceError) as ei:
        make_plan(ps, Health(), depth="normal", query="原神", provider="baidu", budget=b)
    assert "额度" in ei.value.message


async def test_per_search_paid_budget_and_charge():
    b, store = budget()
    a, c, d = P("baidu", paid=True, n=1), P("exa", paid=True, n=1), P("bocha", paid=True, n=1)
    plan = make_plan([a, c, d], Health(), depth="quick", query="原神")
    r = await Executor(Health(), budget=b).run(plan, "原神")
    assert r.spent == ["baidu"] and store[b._key("baidu")] == 1   # quick 只许 1 次付费：不足也不升级到第二个付费源
    assert c.calls == 0


async def test_normal_does_not_hedge_concurrently_to_paid():
    slow = P("baidu", paid=True, delay=2.5, n=1)
    paid2 = P("exa", paid=True)
    plan = make_plan([slow, paid2], Health(), depth="normal", query="原神")
    b, _ = budget()
    t = asyncio.create_task(Executor(Health(), budget=b).run(plan, "原神"))
    await asyncio.sleep(2.2)                     # 超过 P90 先验（2 秒），但主源还没返回
    assert paid2.calls == 0                      # D36：付费来源之间不对冲
    r = await t
    assert paid2.calls == 1 and r.spent == ["baidu", "exa"]   # 主源返回不足后才升级


async def test_normal_hedges_to_free_while_paid_runs():
    slow = P("baidu", paid=True, delay=3.0)
    free = P("keenable")
    plan = make_plan([slow, free], Health(), depth="normal", query="原神")
    t0 = time.monotonic()
    r = await Executor(Health(), budget=budget()[0]).run(plan, "原神")
    assert free.calls == 1 and "keenable" in r.used_sources and time.monotonic() - t0 < 2.9


async def test_deep_agent_used_when_insufficient():
    a = P("keenable", n=1)
    b_ = P("baidu", paid=True, n=1)
    q = P("qwen", paid=True, agent=True, n=3)
    plan = make_plan([a, b_, q], Health(), depth="deep", query="原神", deep_width=2)
    assert names(plan) == (["baidu", "keenable"], ["qwen"])
    r = await Executor(Health(), budget=budget()[0]).run(plan, "原神")
    assert q.calls == 1 and "qwen" in r.used_sources


async def test_freshness_passed_to_new_sources():
    p = P("exa", paid=True)
    await Executor(Health()).run(make_plan([p], Health(), depth="quick", query="x"), "x", freshness="today")
    assert p.freshness == "today"


async def test_not_sent_refunds_per_search_budget():
    b, store = budget()
    bad = P("baidu", paid=True, error=SourceError(Outcome.NOT_SENT, "ConnectError"))
    good = P("exa", paid=True, n=4)
    plan = make_plan([bad, good], Health(), depth="quick", query="原神")
    r = await Executor(Health(), budget=b).run(plan, "原神")
    assert "exa" in r.used_sources            # 没发出去的那次不占本次搜索的付费额度


def test_health_dump_load_keeps_cooldown_and_latency():
    h = Health()
    for s in (1.0, 1.5, 2.0, 2.5, 3.0):
        h.success("exa", s)
    h.failure("bocha", SourceError(Outcome.REJECTED, "HTTP 401", status=401, auth_failed=True))
    h2 = Health()
    h2.load(h.dump())
    assert not h2.available("bocha") and "key" in h2.cooldown_reason("bocha")
    assert h2.p90("exa", "fast") == 3.0
    h2.load("not json")                          # 坏数据不抛异常


def test_langsearch_is_last_resort_for_chinese():
    ps = [P("bocha", paid=True), P("langsearch"), P("keenable"), P("baidu", paid=True)]
    plan = make_plan(ps, Health(), depth="normal", query="原神 新版本")
    order = sum(names(plan), [])
    assert order[0] == "baidu" and order.index("keenable") < order.index("bocha") < order.index("langsearch")
