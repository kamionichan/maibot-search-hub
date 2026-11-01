"""Provider 抽象与首批免 key 来源（第 1 阶段：host_web、keenable、tavily）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from .net import Http, Outcome, SourceError


@dataclass
class Hit:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    source: str = ""
    rank: int = 0


@dataclass
class ProviderInfo:
    name: str               # 配置名，也是 provider 点名用的名字
    family: str             # 索引族（D37）：同族结果不算多源印证
    latency: str            # fast / medium / slow（D16 冷启动估算）
    paid: bool              # 是否消耗付费额度（D42）
    zero_marginal_cost: bool  # 能否作为对冲目标（D36）
    has_dates: bool         # 结果是否带发布时间
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    info: ProviderInfo

    async def search(self, query: str, *, max_results: int) -> list[Hit]: ...


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _valid_url(url: Any) -> bool:
    """F05（0.5.2）：端口非法、主机名为空的 URL 在入口就丢掉，不让它在合并时抛异常。"""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


class KeenableProvider:
    PUBLIC = "https://api.keenable.ai/v1/search/public"
    KEYED = "https://api.keenable.ai/v1/search"

    def __init__(self, http: Http, api_key: str = "") -> None:
        self._http = http
        self._key = api_key.strip()
        self.info = ProviderInfo(
            name="keenable", family="keenable", latency="fast",
            paid=False,  # 带 key 时每月 10 万次免费额度，仍按免费计
            zero_marginal_cost=not self._key, has_dates=False,
            label="Keenable" + ("" if self._key else "（公共接口）"),
        )

    async def search(self, query: str, *, max_results: int) -> list[Hit]:
        body = {"query": query[:400], "max_results": max_results}
        if self._key:
            data = await self._http.post_json(self.KEYED, body, headers={"X-API-Key": self._key})
        else:
            data = await self._http.post_json(self.PUBLIC, body, headers={"X-Keenable-Title": "maibot-search-hub"})
        try:
            results = data["results"]
            hits = []
            for i, r in enumerate(results):
                if not _valid_url(r.get("url")):
                    continue
                hits.append(Hit(
                    title=_clip(r.get("title"), 120), url=r["url"],
                    snippet=_clip(r.get("description") or r.get("snippet"), 300),
                    published_at=r.get("published_at") or None, source=self.info.name, rank=i + 1,
                ))
            return hits
        except (KeyError, TypeError, AttributeError):
            raise SourceError(Outcome.UNKNOWN, "Keenable 返回格式不符") from None


class TavilyProvider:
    URL = "https://api.tavily.com/search"

    def __init__(self, http: Http, api_key: str = "") -> None:
        self._http = http
        self._key = api_key.strip()
        self.info = ProviderInfo(
            name="tavily", family="tavily", latency="fast",
            paid=bool(self._key), zero_marginal_cost=not self._key, has_dates=True,
            label="Tavily" + ("" if self._key else "（keyless）"),
        )

    async def search(self, query: str, *, max_results: int) -> list[Hit]:
        body = {"query": query[:400], "max_results": max_results, "include_published_date": True,
                "search_depth": "basic"}
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {"X-Tavily-Access-Mode": "keyless"}
        data = await self._http.post_json(self.URL, body, headers=headers)
        try:
            results = data["results"]
        except (KeyError, TypeError):
            # keyless 限流时返回自然语言说明（文档未给结构），视为被拒
            raise SourceError(Outcome.REJECTED, "Tavily 未返回结果（可能触发 keyless 限流）") from None
        hits = []
        for i, r in enumerate(results or []):
            if not isinstance(r, dict) or not _valid_url(r.get("url")):
                continue
            hits.append(Hit(
                title=_clip(r.get("title"), 120), url=r["url"], snippet=_clip(r.get("content"), 300),
                published_at=r.get("published_date") or None, source=self.info.name, rank=i + 1,
            ))
        return hits


_HOST_BACKEND_GUESS = ("tavily", "exa", "deepseek", "anysearch", "firecrawl", "stepsearch")


class HostWebProvider:
    """Host ctx.web.search。错误只有中文字符串，按 D36 归类。"""

    def __init__(self, ctx: Any, backend: str = "") -> None:
        self._ctx = ctx
        self._backend = backend.strip().lower()
        family = self._backend or "host_web"
        self.info = ProviderInfo(
            name="host_web", family=family, latency="fast", paid=False, zero_marginal_cost=True,
            has_dates=True, label="MaiBot 联网服务",
        )

    def learn_service_name(self, service: str) -> None:
        if self._backend:
            return
        low = service.lower()
        for b in _HOST_BACKEND_GUESS:
            if b in low:
                self.info.family = b
                return

    async def search(self, query: str, *, max_results: int) -> list[Hit]:
        try:
            res = await self._ctx.web.search(query[:1000], max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            raise classify_host_error(str(exc)) from None
        if not isinstance(res, dict):
            raise SourceError(Outcome.UNKNOWN, "Host 联网服务返回格式不符")
        if res.get("success") is False:
            raise classify_host_error(str(res.get("error") or ""))
        payload: dict[str, Any] = res["result"] if isinstance(res.get("result"), dict) else res
        if isinstance(payload.get("service"), str):
            self.learn_service_name(payload["service"])
        hits = []
        for i, r in enumerate(payload.get("results") or []):
            if not isinstance(r, dict) or not _valid_url(r.get("url")):
                continue
            hits.append(Hit(
                title=_clip(r.get("title"), 120), url=r["url"],
                snippet=_clip(r.get("snippet") or r.get("summary"), 300),
                published_at=r.get("published_at") or None, source=self.info.name, rank=i + 1,
            ))
        return hits


def classify_host_error(text: str) -> SourceError:
    """D36：ctx.web 只给中文字符串。无法识别的一律 unknown。"""
    import re

    if "未启用" in text or "未配置" in text or "缺少 API Key" in text or ("能力" in text and "拒绝" in text):
        # Host 侧没有配置联网服务：请求根本没发出，但重发也没有意义（HOST_FACTS「ctx.web 的错误形态」）
        return SourceError(Outcome.NOT_SENT, "Host 未配置联网服务", unavailable=True)
    m = re.search(r"HTTP (\d{3})", text)
    if m:
        code = int(m.group(1))
        if 400 <= code < 500:
            return SourceError(Outcome.REJECTED, f"HTTP {code}", status=code,
                               auth_failed=code in (401, 403), quota_exhausted=code in (402, 432, 433))
        return SourceError(Outcome.UNKNOWN, f"HTTP {code}", status=code)
    if "连接失败" in text and ("ConnectError" in text or "ConnectTimeout" in text):
        return SourceError(Outcome.NOT_SENT, "连接失败")
    return SourceError(Outcome.UNKNOWN, text or "未知错误")   # SourceError 自己先脱敏再截断
