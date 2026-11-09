"""本机发起的“像浏览器一样”的访问节流（0.6.1，D84）。

适用：camofox 浏览器（Google Lens）、直接请求网页的以图搜引擎（Yandex、百度识图、Bing）。
不适用：正式 API（trace.moe、SauceNAO、AnimeTrace、SerpApi、文字搜索来源）——它们有自己的配额与限流。

每个引擎一个 Pacer：
- 同一时间只跑一个（asyncio.Lock）；
- 两次之间至少隔 min_gap 秒，再随机多等 0~jitter 秒；
- 每小时最多 per_hour 次（滑动窗口）；
- 排队最多等 max_wait 秒，等不到就放弃这一步（SourceError NOT_SENT + budget=True：不计失败、不进冷却，
  交给别的引擎），并写明“约 N 秒/分钟后可用”。
- step()：一次查询内部各操作之间的随机停顿。

Pacer 按引擎名存在模块级字典里：配置重载（换 Provider 对象）不会把计数清零。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from .net import Outcome, SourceError


def _human(seconds: float) -> str:
    s = max(1, int(seconds + 0.999))
    return f"{s} 秒" if s < 90 else f"{(s + 59) // 60} 分钟"


class Pacer:
    def __init__(self, label: str, *, min_gap: float, jitter: float, per_hour: int, max_wait: float,
                 step_range: tuple[float, float] = (0.0, 0.0)) -> None:
        self.label = label
        self.configure(min_gap=min_gap, jitter=jitter, per_hour=per_hour, max_wait=max_wait, step_range=step_range)
        self._lock = asyncio.Lock()
        self._last = 0.0                 # 上一次放行的时间（monotonic）
        self._next = 0.0                 # 下一次最早可以放行的时间
        self._hour: deque[float] = deque()
        self._waiting = 0

    def configure(self, *, min_gap: float, jitter: float, per_hour: int, max_wait: float,
                  step_range: tuple[float, float] = (0.0, 0.0)) -> None:
        self.min_gap, self.jitter = max(0.0, float(min_gap)), max(0.0, float(jitter))
        self.per_hour, self.max_wait = max(0, int(per_hour)), max(0.0, float(max_wait))
        self.step_range = (max(0.0, step_range[0]), max(step_range[0], step_range[1]))

    # ---- 状态（/search status 用） ----
    def status(self) -> str:
        now = time.monotonic()
        self._trim(now)
        used = len(self._hour)
        parts = [f"近一小时 {used}" + (f"/{self.per_hour}" if self.per_hour else "") + " 次"]
        if self._next > now:
            parts.append(f"约 {_human(self._next - now)}后可用")
        if self._waiting:
            parts.append(f"{self._waiting} 个在排队")
        return "，".join(parts)

    def _trim(self, now: float) -> None:
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()

    def _hour_wait(self, now: float) -> float:
        self._trim(now)
        if self.per_hour and len(self._hour) >= self.per_hour:
            return 3600 - (now - self._hour[0]) + 1
        return 0.0

    def _refuse(self, wait: float, why: str) -> SourceError:
        return SourceError(Outcome.NOT_SENT, f"{self.label} {why}，约 {_human(wait)}后可用（本插件限速，防止被网站拦截）",
                           budget=True, retry_after=None)

    @asynccontextmanager
    async def slot(self, ready: "Callable[[], str] | None" = None) -> AsyncIterator[None]:
        """占用一次访问名额：排队、守间隔、守每小时上限。等不到就抛 SourceError（budget=True）。

        R62-07：排队和守间隔共用一个绝对截止时间（入口时刻 + max_wait），拿到锁后只用剩下的时间。
        R62-08：ready() 在真正放行前调用（拿到锁、间隔也等完之后）；返回非空字符串表示这时已经不该发了
        （例如排队期间别的请求刚让这个引擎进了冷却），就不发、不计次数，抛 SourceError（budget=True）。"""
        start = time.monotonic()
        deadline = start + self.max_wait
        hw = self._hour_wait(start)
        if hw > 0:
            raise self._refuse(hw, f"已达每小时 {self.per_hour} 次上限")
        expect = max(0.0, self._next - start)
        if expect > self.max_wait:
            raise self._refuse(expect, "刚用过，两次之间要隔一段时间")
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=max(0.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                raise self._refuse(max(self._next - time.monotonic(), 1.0), "正在被别的搜索使用") from None
        finally:
            self._waiting -= 1
        try:
            now = time.monotonic()
            wait = self._next - now
            if now + max(0.0, wait) > deadline + 0.01:
                raise self._refuse(max(wait, 1.0), "刚用过，两次之间要隔一段时间")
            hw = self._hour_wait(now)
            if hw > 0:
                raise self._refuse(hw, f"已达每小时 {self.per_hour} 次上限")
            if wait > 0:
                await asyncio.sleep(wait)
            if time.monotonic() > deadline + 0.05:          # R63-07：晚醒了，截止已过就不放行
                raise self._refuse(1.0, "排队太久")
            if ready is not None:
                why = ready()
                if why:
                    raise SourceError(Outcome.NOT_SENT, why, budget=True)
            now = time.monotonic()
            self._last = now
            self._hour.append(now)
            try:
                yield
            finally:
                # 间隔从这次结束时算起，再加随机抖动
                done = time.monotonic()
                self._next = done + self.min_gap + random.uniform(0.0, self.jitter)
        finally:
            self._lock.release()

    async def step(self) -> None:
        """一次查询内部各操作之间的随机停顿。"""
        lo, hi = self.step_range
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))


class SitePacer:
    """候选图下载按网站节流（用户 2026-11-09 同意）：同一个网站同一时间只下一张，两次之间隔 gap 秒再加 0~jitter 随机；
    不同网站照样并行，不设每小时上限。只在内存里（每次下载都很短，跨重启没有意义）。

    等待受调用方传入的截止时间约束：等不到就返回 False，调用方跳过这张图。

    R63-05：每个网站一个条目（锁、下次可用时间、使用者数）。回收只删“没人持有、没人排队、间隔已过”的条目，
    不会出现同一网站两把锁，也不会丢掉还没到期的间隔。
    R63-06：读取器在**每一跳**按那一跳的实际网站取名额（只包住单次请求，不跨跳持有，不会互相等死）。
    R63-07：拿到锁、等完间隔之后再核对截止时间，过了就不放行。"""

    MAX_SITES = 512

    class _Site:
        __slots__ = ("lock", "next", "users")

        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.next = 0.0
            self.users = 0       # 持有或排队中的调用数

    def __init__(self, gap: float = 1.0, jitter: float = 1.0) -> None:
        self.gap, self.jitter = gap, jitter
        self._sites: dict[str, SitePacer._Site] = {}

    @staticmethod
    def site(url: str) -> str:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
        parts = host.split(".")
        # 粗略取主域名：i.pinimg.com 与 www.pinterest.com 不合并，但 a.b.example.com 与 c.example.com 合并
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def _reclaim(self) -> None:
        if len(self._sites) <= self.MAX_SITES:
            return
        now = time.monotonic()
        idle = [k for k, st in self._sites.items() if st.users == 0 and not st.lock.locked() and st.next <= now]
        for k in idle[: max(1, len(idle) // 2)]:
            del self._sites[k]

    @asynccontextmanager
    async def slot(self, url: str, deadline: float) -> AsyncIterator[bool]:
        key = self.site(url)
        self._reclaim()
        st = self._sites.get(key)
        if st is None:
            st = self._sites[key] = SitePacer._Site()
        st.users += 1
        try:
            if deadline - time.monotonic() <= 0:
                yield False
                return
            try:
                await asyncio.wait_for(st.lock.acquire(), timeout=max(0.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                yield False
                return
            try:
                wait = st.next - time.monotonic()
                if wait > 0:
                    if time.monotonic() + wait > deadline:
                        yield False
                        return
                    await asyncio.sleep(wait)
                if time.monotonic() > deadline:        # R63-07：晚醒了，截止已过就不放行
                    yield False
                    return
                try:
                    yield True
                finally:
                    st.next = time.monotonic() + self.gap + random.uniform(0.0, self.jitter)
            finally:
                st.lock.release()
        finally:
            st.users -= 1

    def hop(self, deadline: float) -> "Callable[[str], Any]":
        """给读取器用的每跳钩子：hop(url) 返回一个异步上下文，产出是否放行。"""
        return lambda url: self.slot(url, deadline)


DOWNLOADS = SitePacer()


_PACERS: dict[str, Pacer] = {}

# 默认值（可在配置页「高级 → 以图搜图限速」调）
BROWSER_DEFAULT = dict(min_gap=60.0, jitter=30.0, per_hour=6, max_wait=20.0, step_range=(0.8, 2.5))
WEB_DEFAULT = dict(min_gap=5.0, jitter=5.0, per_hour=30, max_wait=15.0, step_range=(0.3, 1.2))


def pacer(name: str, label: str, **cfg: float) -> Pacer:
    """取（或建）某个引擎的节流器；每次调用都按最新配置更新参数，计数保留。"""
    p = _PACERS.get(name)
    if p is None:
        p = _PACERS[name] = Pacer(label, **cfg)       # type: ignore[arg-type]
        _restore(name, p)
    else:
        p.label = label
        p.configure(**cfg)                            # type: ignore[arg-type]
    return p


def all_status() -> dict[str, str]:
    return {name: p.status() for name, p in _PACERS.items()}


def dump() -> dict:
    """存进插件数据库：重启（更新插件、重启麦麦）后间隔和每小时计数不清零。monotonic 换成墙钟时间存。

    R62-09：这次没建起来的引擎（例如重启时被关掉了），之前保存的状态原样保留（过期的小时记录裁掉），
    不能因为这次没用到就清零。"""
    wall, mono = time.time(), time.monotonic()
    out: dict = {}
    for name, st in _SAVED.items():
        if name in _PACERS or not isinstance(st, dict):
            continue
        try:
            hour = [float(t) for t in st.get("hour") or [] if 0 <= wall - float(t) < 3600]
            out[name] = {"next": float(st.get("next") or 0), "hour": hour, "gap": float(st.get("gap") or 0)}
        except (TypeError, ValueError):
            continue
    for name, p in _PACERS.items():
        out[name] = {"next": wall + (p._next - mono), "hour": [wall + (t - mono) for t in p._hour],
                     "gap": p.min_gap + p.jitter}
    return out


_SAVED: dict = {}


def load(data: object) -> None:
    """读回 dump() 的内容；之后建立的同名 Pacer 会接上这些状态。"""
    global _SAVED
    _SAVED = data if isinstance(data, dict) else {}
    for name, p in _PACERS.items():
        _restore(name, p)


def _restore(name: str, p: Pacer) -> None:
    st = _SAVED.get(name)
    if not isinstance(st, dict):
        return
    wall, mono = time.time(), time.monotonic()
    try:
        nxt = float(st.get("next") or 0)
        # R62-11：系统时间往回调了，保存的“未来”记录会变得很远。小时记录只收过去一小时内的；
        # 间隔最多恢复成一个完整间隔（min_gap + jitter），不会恢复出几个小时的“暂不可用”
        hour = [float(t) for t in st.get("hour") or [] if 0 <= wall - float(t) < 3600]
    except (TypeError, ValueError):
        return
    cap = p.min_gap + p.jitter
    p._next = max(p._next, mono + min(max(0.0, nxt - wall), cap))
    if not p._hour:
        p._hour.extend(sorted(mono + (t - wall) for t in hour))


def reset_for_tests() -> None:
    _PACERS.clear()
    _SAVED.clear()
