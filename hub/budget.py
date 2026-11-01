"""付费调用预算（D42）。

- 每次搜索：quick 1、normal 2、deep 5 次付费调用；其中 Agent 类来源最多 1 次。
- 每个来源每日上限（按免费额度折算，可配置；0 = 不限）。按本地日期计数，持久化在 ledger 的 counters 表。
- 只有"已发出"的调用才计数（not_sent 不计）：D36 规定状态未知的请求可能已计费，宁可多计。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

PER_SEARCH = {"quick": 1, "normal": 2, "deep": 5}
AGENT_PER_SEARCH = 1

# 按免费额度折算的默认每日上限（KEYS.md）。用户可在配置里覆盖。
DEFAULT_DAILY = {
    "tavily": 33,       # 每月 1000 次
    "exa": 45,          # 0.5.6：本插件用 Auto 模式，$7/千次；每月 $10 ≈ 1428 次，31 天 × 45 = 1395 次
    "brave": 33,        # 每月 $5 ≈ 1000 次
    "firecrawl": 16,    # 每月 1000 credits ≈ 500 次搜索
    "linkup": 60,       # 未申请（D51）；额度以官网为准
    "parallel": 160,    # 每月 5000 次
    "serper": 40,       # 一次性 2500 次
    "perplexity": 0,    # 无免费额度：默认不限（填 key 即表示愿意付费）
    "bocha": 30,        # 一次性 1000 次，3 个月有效（试用期后 Tier0 每天 1000 次）
    "baidu": 50,        # 免费额度按天发放：每月 1500 ≈ 每天 50
    "qwen": 30,         # 联网搜索 ¥3–4/千次 + token；保守值
}


@dataclass
class SearchBudget:
    """一次搜索任务内的付费额度。"""

    paid_left: int
    agent_left: int = AGENT_PER_SEARCH
    spent: list[str] = field(default_factory=list)


class Budget:
    def __init__(self, counter_get: Callable[[str], int], counter_add: Callable[[str, int], None],
                 daily_limits: dict[str, int] | None = None) -> None:
        self._get = counter_get
        self._add = counter_add
        self.daily = dict(DEFAULT_DAILY)
        if daily_limits:
            self.daily.update({k: int(v) for k, v in daily_limits.items() if v is not None and int(v) >= 0})

    @staticmethod
    def _key(name: str) -> str:
        return f"daily:{time.strftime('%Y-%m-%d')}:{name}"

    def used_today(self, name: str) -> int:
        return self._get(self._key(name))

    def daily_left(self, name: str) -> int | None:
        limit = self.daily.get(name, 0)
        if limit <= 0:
            return None
        return max(0, limit - self.used_today(name))

    def for_search(self, depth: str) -> SearchBudget:
        return SearchBudget(PER_SEARCH.get(depth, 2))

    def allows(self, info: Any, sb: SearchBudget) -> tuple[bool, str]:
        if not info.paid:
            return True, ""
        if sb.paid_left <= 0:
            return False, "本次搜索的付费调用次数已用完"
        if info.extra.get("agent") and sb.agent_left <= 0:
            return False, "本次搜索的 Agent 调用次数已用完"
        left = self.daily_left(info.name)
        if left is not None and left <= 0:
            return False, f"今日调用已达上限（{self.daily[info.name]} 次）"
        return True, ""

    def charge(self, info: Any, sb: SearchBudget) -> None:
        if not info.paid:
            return
        sb.paid_left -= 1
        if info.extra.get("agent"):
            sb.agent_left -= 1
        sb.spent.append(info.name)
        self._add(self._key(info.name), 1)
