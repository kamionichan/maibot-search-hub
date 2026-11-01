"""来源健康：按错误类型冷却 + 延迟统计（D16、D36）。第 3 阶段起可导出/导入，跨重启保留。"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field

from .net import Outcome, SourceError

LATENCY_PRIOR = {"fast": 2.0, "medium": 8.0, "slow": 30.0}


@dataclass
class _State:
    cooldown_until: float = 0.0
    reason: str = ""
    consecutive: dict[str, int] = field(default_factory=dict)
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    ok: int = 0
    fail: int = 0


class Health:
    def __init__(self) -> None:
        self._s: dict[str, _State] = {}

    def _st(self, name: str) -> _State:
        return self._s.setdefault(name, _State())

    def available(self, name: str) -> bool:
        return time.monotonic() >= self._st(name).cooldown_until

    def cooldown_reason(self, name: str) -> str:
        st = self._st(name)
        return st.reason if time.monotonic() < st.cooldown_until else ""

    def p90(self, name: str, prior: str) -> float:
        lat = sorted(self._st(name).latencies)
        if len(lat) < 5:
            return LATENCY_PRIOR.get(prior, 8.0)
        return lat[min(len(lat) - 1, int(len(lat) * 0.9))]

    def success(self, name: str, seconds: float) -> None:
        st = self._st(name)
        st.latencies.append(seconds)
        st.consecutive.clear()
        st.ok += 1
        st.cooldown_until = 0.0
        st.reason = ""

    def failure(self, name: str, err: SourceError) -> None:
        if err.budget:
            return   # 预算挡下不是来源的问题，不计失败、不冷却
        st = self._st(name)
        st.fail += 1
        now = time.monotonic()
        if err.unavailable:
            self._cool(st, now, 30 * 60, err.message)
            return
        if err.outcome is Outcome.REJECTED:
            if err.auth_failed:
                self._cool(st, now, 24 * 3600, "鉴权失败，请检查 key")
            elif err.quota_exhausted:
                self._cool(st, now, 24 * 3600, "额度用尽")
            elif err.status == 429 or err.retry_after:
                self._cool(st, now, err.retry_after or 180, "触发限流")
            else:
                self._cool(st, now, 60, err.message)
            return
        key = err.outcome.value
        st.consecutive[key] = st.consecutive.get(key, 0) + 1
        if st.consecutive[key] >= 3:
            self._cool(st, now, 60 if err.outcome is Outcome.NOT_SENT else 120,
                       "连续连接失败" if err.outcome is Outcome.NOT_SENT else "连续出错")
            st.consecutive[key] = 0

    @staticmethod
    def _cool(st: _State, now: float, seconds: float, reason: str) -> None:
        st.cooldown_until = max(st.cooldown_until, now + seconds)
        st.reason = reason

    def reset(self, name: str) -> None:
        """0.5.3：管理员换了 key 等情况下解除冷却（延迟统计保留）。"""
        st = self._s.get(name)
        if st is not None:
            st.cooldown_until, st.reason = 0.0, ""
            st.consecutive.clear()

    def dump(self) -> str:
        """冷却换算成墙钟时间保存（monotonic 跨进程无意义）。"""
        now_m, now_w = time.monotonic(), time.time()
        out = {}
        for n, st in self._s.items():
            left = st.cooldown_until - now_m
            out[n] = {"lat": list(st.latencies)[-50:], "ok": st.ok, "fail": st.fail,
                      "cd_until": now_w + left if left > 0 else 0, "reason": st.reason if left > 0 else ""}
        return json.dumps(out)

    def load(self, raw: str | None) -> None:
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        now_m, now_w = time.monotonic(), time.time()
        for n, d in data.items():
            st = self._st(n)
            st.latencies.extend(float(x) for x in d.get("lat", [])[-50:])
            st.ok, st.fail = int(d.get("ok", 0)), int(d.get("fail", 0))
            left = float(d.get("cd_until", 0)) - now_w
            if left > 0:
                st.cooldown_until, st.reason = now_m + left, str(d.get("reason", ""))

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        return {n: {"ok": s.ok, "fail": s.fail, "cooldown_s": max(0, round(s.cooldown_until - now)), "reason": s.reason}
                for n, s in self._s.items()}
