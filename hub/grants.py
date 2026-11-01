"""D21 精简版一次性授权。

after_response Hook（order=late）为本插件每个工具调用签发一次性 grant，
登记 grant → {session_id, logical_turn_id, tool, kind}。工具端原子消费，只信任登记表里的 session_id。
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

GRANT_FIELD = "_hub_grant"
# 模型可能自己填写的身份类字段：签发时一律删除，工具端也不信任。
IDENTITY_FIELDS = ("stream_id", "chat_id", "session_id", "_hub_grant", "user_id", "platform")


@dataclass(frozen=True)
class Grant:
    session_id: str
    logical_turn_id: str
    tool: str
    issued_at: float
    caller: str  # "planner" | "subagent"
    task: str = ""  # 子代理任务指纹（D46 限次用）


class GrantBook:
    def __init__(self, ttl: float = 300.0, capacity: int = 512) -> None:
        self._ttl = ttl
        self._cap = capacity
        self._items: OrderedDict[str, Grant] = OrderedDict()

    def issue(self, *, session_id: str, logical_turn_id: str, tool: str, caller: str, task: str = "") -> str:
        self._evict()
        token = secrets.token_urlsafe(32)
        self._items[token] = Grant(session_id, logical_turn_id, tool, time.monotonic(), caller, task)
        while len(self._items) > self._cap:
            self._items.popitem(last=False)
        return token

    def consume(self, token: object, tool: str) -> Grant | None:
        if not isinstance(token, str) or not token:
            return None
        grant = self._items.pop(token, None)  # 原子：pop 之后不可能再被消费
        if grant is None or grant.tool != tool:
            return None
        if time.monotonic() - grant.issued_at > self._ttl:
            return None
        return grant

    def _evict(self) -> None:
        now = time.monotonic()
        while self._items:
            token, grant = next(iter(self._items.items()))
            if now - grant.issued_at <= self._ttl:
                break
            self._items.pop(token, None)

    def __len__(self) -> int:
        return len(self._items)
