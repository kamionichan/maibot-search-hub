"""规划与执行（D24、D33、D36、D37、D38、D42）。

规划规则：
- 候选 = 已启用、不在冷却中、今日额度未用完的来源；同族只保留一个（D37：插件自配优先于 host_web）。
- 排序：用户配置的 priority 优先；否则按查询语言（中文查询中文源在前）与 intent 选择默认顺序。
  同一档内，免费/零边际成本的来源在前（D42 省额度）。
- Agent 类来源（千问联网搜索）只在 deep 档、或 intent=discussion 时进入计划（D33），不进 quick。
- provider 点名：放在最前；点名的来源不可用时直接报错，不静默换源（D32）。
- quick：主源单发，主源失败才换下一个，最多换 1 次。
- normal：主源先发；超过它的 P90 仍未返回，或返回"不足"，就升级到下一个来源；最多 2 次升级。
  零边际成本来源可以"对冲"（主源还在跑时就并发）；付费来源之间不对冲，只在主源结束且不足时才发（D36）。
- deep：计划内 N 个来源（每族一个）同时发出（D38）。
- 每次搜索的付费调用次数受 D42 预算约束；预算不允许的来源跳过并记入 attempts。
- unknown 绝不重发同一来源；not_sent 允许同一来源重发 1 次。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .budget import Budget, SearchBudget
from .health import Health
from .merge import Merged, judge, rrf_merge
from .net import Outcome, SourceError
from .providers import Hit, Provider

# 默认顺序（priority 未配置时）。依据实测（PHASE1_RESULTS「第 3 阶段真实验收」）：
# 中文：百度千帆最好（日期准、全是中文结果）；Exa 中英文都好；博查一次性额度且对"7.1"这类版本号召回差，放后面；
# LangSearch 对中文查询返回俄/意/日文结果，只作最后的免费兜底。
ORDER_ZH = ("baidu", "keenable", "exa", "tavily", "searxng", "parallel", "brave", "serper", "linkup",
            "firecrawl", "perplexity", "bocha", "langsearch", "host_web", "qwen")
ORDER_EN = ("exa", "keenable", "tavily", "brave", "parallel", "searxng", "serper", "linkup", "perplexity",
            "firecrawl", "baidu", "bocha", "langsearch", "host_web", "qwen")
# 讨论/口碑类：Agent 与"能搜到论坛"的来源靠前
ORDER_DISCUSSION = ("qwen", "baidu", "keenable", "exa", "searxng", "tavily", "brave", "serper", "parallel",
                    "linkup", "firecrawl", "perplexity", "bocha", "langsearch", "host_web")
DEFAULT_WEIGHT = {"langsearch": 1.0, "keenable": 1.0, "tavily": 0.9, "host_web": 0.9, "bocha": 1.0, "baidu": 1.0, "exa": 1.0,
                  "brave": 1.0, "parallel": 0.95, "serper": 1.0, "linkup": 0.9, "firecrawl": 0.85,
                  "perplexity": 0.95, "searxng": 0.9, "qwen": 0.9}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def is_chinese(query: str) -> bool:
    return len(_CJK.findall(query)) >= max(1, len(query.strip()) // 6)


@dataclass
class Attempt:
    source: str
    started: float
    seconds: float = 0.0
    outcome: str = "pending"   # ok / not_sent / rejected / unknown / cancelled / skipped
    n: int = 0
    note: str = ""


@dataclass
class RunResult:
    results: list[Merged]
    missing: list[str]
    attempts: list[Attempt]
    used_sources: list[str]
    complete: bool = True
    error: str = ""            # 全部失败时的说明
    error_kind: str = ""       # rejected / unknown / not_sent / unavailable
    spent: list[str] = field(default_factory=list)


@dataclass
class Plan:
    depth: str
    primary: list[Provider]
    hedge_pool: list[Provider] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (来源, 原因)：额度等


def _order(query: str, intent: str, priority: list[str]) -> list[str]:
    base = ORDER_DISCUSSION if intent == "discussion" else (ORDER_ZH if is_chinese(query) else ORDER_EN)
    seen: list[str] = []
    for n in [*priority, *base]:
        if n and n not in seen:
            seen.append(n)
    return seen


def make_plan(providers: list[Provider], health: Health, *, depth: str, provider: str = "",
              deep_width: int = 3, query: str = "", intent: str = "auto", priority: list[str] | None = None,
              budget: Budget | None = None) -> Plan:
    skipped: list[tuple[str, str]] = []
    agent_ok = depth == "deep" or intent == "discussion"
    usable = []
    for p in providers:
        if not health.available(p.info.name):
            continue
        if p.info.extra.get("agent") and not agent_ok and p.info.name != provider:
            continue
        if budget is not None and p.info.paid:
            left = budget.daily_left(p.info.name)
            if left is not None and left <= 0:
                skipped.append((p.info.name, "今日额度已用完"))
                continue
        usable.append(p)
    order = {n: i for i, n in enumerate(_order(query, intent, priority or []))}
    usable.sort(key=lambda p: order.get(p.info.name, 99))
    # 同族去重：排在前面的保留（插件自配排在 host_web 前，自然优先）
    seen: set[str] = set()
    dedup = []
    for p in usable:
        if p.info.family in seen:
            continue
        seen.add(p.info.family)
        dedup.append(p)
    if provider:
        named = [p for p in providers if p.info.name == provider]
        if not named:
            raise SourceError(Outcome.NOT_SENT, f"来源 {provider} 未启用")
        if not health.available(provider):
            raise SourceError(Outcome.NOT_SENT, f"来源 {provider} 暂不可用：{health.cooldown_reason(provider)}")
        if any(n == provider for n, _ in skipped):
            raise SourceError(Outcome.NOT_SENT, f"来源 {provider} 今日额度已用完")
        rest = [p for p in dedup if p.info.family != named[0].info.family]
        dedup = [named[0], *rest]
    if not dedup:
        raise SourceError(Outcome.NOT_SENT, "没有可用的搜索来源" + (
            "（" + "；".join(f"{n}：{r}" for n, r in skipped) + "）" if skipped else ""))
    if depth == "deep":
        return Plan(depth, dedup[:deep_width], dedup[deep_width:], skipped)
    # normal / quick：Agent 类不做主源（除非点名或讨论类排在最前）
    return Plan(depth, [dedup[0]], dedup[1:], skipped)


class Executor:
    def __init__(self, health: Health, max_results: int = 5, budget: Budget | None = None) -> None:
        self.health = health
        self.max_results = max_results
        self.budget = budget
        self._sb: SearchBudget | None = None
        self._freshness = "any"
        # F05（0.5.2）：随跑随记。Job 超时或被取消时用 partial() 交付已拿到的结果，而不是丢掉。
        self._snapshot: Any = None
        # 搜索已结束但还没返回的请求（D36：不取消已发出的请求）。JobManager 卸载时统一收尾，不再无人管理。
        self.tails: set[asyncio.Task] = set()

    def partial(self) -> RunResult | None:
        """当前已拿到的结果（标为不完整）。还没有任何结果时返回 None。"""
        return self._snapshot() if self._snapshot is not None else None

    async def _search(self, p: Provider, query: str) -> list[Hit]:
        # 第 1 阶段的来源不接受 freshness；新来源接受
        if "freshness" in inspect.signature(p.search).parameters:
            return await p.search(query, max_results=self.max_results, freshness=self._freshness)
        return await p.search(query, max_results=self.max_results)

    async def _call(self, p: Provider, query: str, attempts: list[Attempt]) -> list[Hit]:
        """单个来源调用。not_sent 时同一来源重发 1 次；其余不重发。付费调用先过预算（D42）。"""
        for retry in range(2):
            if self.budget is not None and self._sb is not None:
                ok, why = self.budget.allows(p.info, self._sb)
                if not ok:
                    attempts.append(Attempt(p.info.name, time.monotonic(), outcome="skipped", note=why))
                    raise SourceError(Outcome.NOT_SENT, why, unavailable=True, budget=True)
            a = Attempt(p.info.name, time.monotonic())
            attempts.append(a)
            charged = False
            try:
                if self.budget is not None and self._sb is not None and p.info.paid:
                    self.budget.charge(p.info, self._sb)  # 发出即计费：unknown 也可能已扣费（D36）
                    charged = True
                hits = await self._search(p, query)
            except asyncio.CancelledError:
                a.outcome, a.seconds = "cancelled", time.monotonic() - a.started
                raise
            except SourceError as err:
                a.outcome, a.note, a.seconds = err.outcome.value, err.message, time.monotonic() - a.started
                if charged and err.outcome is Outcome.NOT_SENT and self._sb is not None:
                    self._sb.paid_left += 1   # 请求没发出去：本次搜索的额度退回（每日计数保守地不退）
                self.health.failure(p.info.name, err)
                if (err.outcome is Outcome.NOT_SENT and not err.unavailable and retry == 0
                        and self.health.available(p.info.name)):
                    await asyncio.sleep(1.0)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                err = SourceError(Outcome.UNKNOWN, type(exc).__name__)
                a.outcome, a.note, a.seconds = "unknown", err.message, time.monotonic() - a.started
                self.health.failure(p.info.name, err)
                raise err from None
            a.seconds = time.monotonic() - a.started
            a.outcome, a.n = "ok", len(hits)
            self.health.success(p.info.name, a.seconds)
            return hits
        raise SourceError(Outcome.UNKNOWN, "unreachable")  # pragma: no cover

    async def run(self, plan: Plan, query: str, *, freshness: str = "any", intent: str = "auto") -> RunResult:
        self._freshness = freshness
        if self.budget is not None:
            self._sb = self.budget.for_search(plan.depth)
        attempts: list[Attempt] = [Attempt(n, time.monotonic(), outcome="skipped", note=why) for n, why in plan.skipped]
        batches: list[tuple[str, str, float, list[Hit]]] = []
        errors: list[SourceError] = []
        used: list[str] = []

        def add(p: Provider, hits: list[Hit]) -> None:
            batches.append((p.info.name, p.info.family, DEFAULT_WEIGHT.get(p.info.name, 0.8), hits))
            used.append(p.info.name)

        def current() -> tuple[list[Merged], list[str]]:
            merged = rrf_merge(batches)
            return merged, judge(query, merged, depth=plan.depth, freshness=freshness, intent=intent)

        def snapshot() -> RunResult | None:
            if not used and not errors:
                return None
            r = self._finish(current, list(attempts), list(used), list(errors))
            r.complete = False
            return r

        self._snapshot = snapshot

        if plan.depth == "deep":
            # F05：每个来源一返回就登记，总时限到了也不会丢掉已返回的来源
            async def one(p: Provider) -> None:
                try:
                    add(p, await self._call(p, query, attempts))
                except SourceError as e:
                    errors.append(e)

            await asyncio.gather(*(one(p) for p in plan.primary))
            # 不足或有来源失败：从备选里再补 1 个（Agent 类优先，D33"结果不足时使用"）
            _, missing = current() if batches else ([], ["few"])
            if missing and plan.hedge_pool:
                pool = sorted(plan.hedge_pool, key=lambda p: 0 if p.info.extra.get("agent") else 1)
                for p in pool:
                    try:
                        add(p, await self._call(p, query, attempts))
                        break
                    except SourceError as e:
                        errors.append(e)
                        if e.unavailable:
                            continue
                        break
            return self._finish(current, attempts, used, errors)

        if plan.depth == "quick":
            for p in [*plan.primary, *[h for h in plan.hedge_pool if not h.info.extra.get("agent")]][:2]:
                try:
                    add(p, await self._call(p, query, attempts))
                    break
                except SourceError as e:
                    errors.append(e)
            return self._finish(current, attempts, used, errors)

        # normal：主源 + 对冲（最多 2 次升级）
        primary = plan.primary[0]
        hedges = list(plan.hedge_pool)
        upgrades = 0
        main = asyncio.create_task(self._call(primary, query, attempts))
        pending: dict[asyncio.Task, Provider] = {main: primary}
        delay = self.health.p90(primary.info.name, primary.info.latency)
        deadline_hedge = time.monotonic() + delay
        try:
            while pending:
                timeout = (max(0.0, deadline_hedge - time.monotonic())
                           if (hedges and upgrades < 2 and deadline_hedge != float("inf")) else None)
                done, _ = await asyncio.wait(pending.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    p = pending.pop(t)
                    try:
                        add(p, t.result())
                    except SourceError as e:
                        errors.append(e)
                _, missing = current() if batches else ([], ["few"])
                if batches and not missing:
                    break  # 足够了；剩下的任务不取消，但也不再等待（在后台跑完只更新健康统计）
                need_hedge = (not done) or (done and missing)
                if need_hedge and hedges and upgrades < 2:
                    # D36：还有请求在跑时只能对冲到零边际成本来源；付费来源要等所有在跑的请求结束且仍不足。
                    # F08（0.5.2）：以前只看“这一轮有没有请求结束”，免费对冲先返回且不足时，
                    # 付费主源还在跑就启动了第二个付费来源。
                    if pending:
                        free = [h for h in hedges if h.info.zero_marginal_cost]
                        if not free:
                            deadline_hedge = float("inf")
                            continue
                        nxt = free[0]
                        hedges.remove(nxt)
                    else:
                        nxt = hedges.pop(0)
                    upgrades += 1
                    pending[asyncio.create_task(self._call(nxt, query, attempts))] = nxt
                    deadline_hedge = time.monotonic() + self.health.p90(nxt.info.name, nxt.info.latency)
                elif not pending:
                    break
        finally:
            for t in pending:
                # D36：不取消已发出的请求（可能已计费）；结果到达后只用于健康统计。交给 JobManager 托管（F05）
                self.tails.add(t)
                t.add_done_callback(self._tail_done)
        return self._finish(current, attempts, used, errors)

    def _tail_done(self, t: asyncio.Task) -> None:
        self.tails.discard(t)
        if not t.cancelled():
            t.exception()   # 取走异常，避免“Task exception was never retrieved”

    def _finish(self, current, attempts: list[Attempt], used: list[str], errors: list[SourceError]) -> RunResult:
        r = self._finish_inner(current, attempts, used, errors)
        r.spent = list(self._sb.spent) if self._sb else []
        return r

    @staticmethod
    def _finish_inner(current, attempts: list[Attempt], used: list[str], errors: list[SourceError]) -> RunResult:
        if not used:
            kinds = {e.outcome for e in errors}
            kind = ("unknown" if Outcome.UNKNOWN in kinds else
                    "rejected" if Outcome.REJECTED in kinds else "not_sent")
            msg = "；".join(dict.fromkeys(e.message for e in errors)) or "没有可用的搜索来源"
            return RunResult([], ["few"], attempts, used, complete=False, error=msg, error_kind=kind)
        merged, missing = current()
        # 0.5.3：被预算挡下的来源不算“失败”（生产中 normal 档第 3 个付费来源被跳过，卡片却写“部分来源失败”）
        return RunResult(merged, missing, attempts, used, complete=not [e for e in errors if not e.budget])


def attempts_summary(attempts: list[Attempt]) -> list[dict[str, Any]]:
    return [{"src": a.source, "s": round(a.seconds, 2), "out": a.outcome, "n": a.n, "note": a.note} for a in attempts]
