"""第 3 阶段来源（D33、D47）：填 key / 地址即自动启用（D35、D39）。

每个适配器只做三件事：按官方文档构造请求、把响应映射成 Hit、把厂商特有的错误归到 D36 的三类里。
字段依据（2026-10-29 核实，见 docs/TEXT_SOURCE_SURVEY.md 与 docs/KEYS.md）：
- Exa        POST https://api.exa.ai/search，x-api-key；results[].title/url/publishedDate/highlights[]；402=额度
- Brave      GET  https://api.search.brave.com/res/v1/web/search，X-Subscription-Token；web.results[].title/url/description/page_age；freshness=pd/pw/pm/py
- Firecrawl  POST https://api.firecrawl.dev/v2/search，Bearer；data.web[].title/description/url；tbs=qdr:d/w/m；402=额度
- Linkup     POST https://api.linkup.so/v1/search，Bearer；必填 q/depth/outputType；results[].name/url/content；429=限流或额度
- Parallel   POST https://api.parallel.ai/v1/search，x-api-key；必填 search_queries；results[].url/title/publish_date/excerpts[]
- Serper     POST https://google.serper.dev/search，X-API-KEY；organic[].title/link/snippet/date；tbs=qdr:d/w/m
- Perplexity POST https://api.perplexity.ai/search，Bearer；results[].title/url/snippet/date
- LangSearch POST https://api.langsearch.com/v1/web-search，Bearer；格式同博查，contents.text 返回正文片段；免费档每日额度
- 博查       POST https://api.bochaai.com/v1/web-search，Bearer；data.webPages.value[].name/url/snippet/summary/datePublished；403=余额不足
- 百度千帆    POST https://qianfan.baidubce.com/v2/ai_search/web_search，Bearer；references[].title/url/content|snippet/date；
              查询上限 72 字符（汉字算 2）；search_recency_filter=week/month/semiyear/year
- 千问联网搜索（Agent）DashScope text-generation，enable_search + search_options.enable_source=true + forced_search；
              output.search_info.search_results[].title/url/site_name；只取来源，不用模型生成的回答（证据优先）
- SearXNG    GET {base}/search?format=json；results[].title/url/content/publishedDate/engines；format 未开启时 403
"""

from __future__ import annotations

import re
from typing import Any

from .net import Http, Outcome, SourceError
from .providers import Hit, ProviderInfo, _clip, _valid_url

FRESH_DAYS = {"today": 1, "recent": 30}


def _first(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _require(data: Any, *path: str) -> Any:
    cur = data
    try:
        for k in path:
            cur = cur[k]
    except (KeyError, TypeError, IndexError):
        raise SourceError(Outcome.UNKNOWN, "返回格式不符") from None
    return cur


def _hits(source: str, rows: Any, *, title: str, url: str, snippet, date: str | None = None) -> list[Hit]:
    out: list[Hit] = []
    if not isinstance(rows, list):
        raise SourceError(Outcome.UNKNOWN, "返回格式不符")
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not _valid_url(r.get(url)):
            continue
        snip = snippet(r) if callable(snippet) else r.get(snippet)
        out.append(Hit(title=_clip(r.get(title) or r.get(url), 120), url=r[url], snippet=_clip(snip, 300),
                       published_at=(str(r.get(date)) if date and r.get(date) else None), source=source, rank=i + 1))
    return out


class _Keyed:
    """需要 key 的来源共用：key 为空时不会被构造（plugin 负责）。"""

    name = ""

    def __init__(self, http: Http, api_key: str) -> None:
        self._http = http
        self._key = api_key.strip()

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:  # pragma: no cover
        raise NotImplementedError


# ---------------- 境外通用 API ----------------
class ExaProvider(_Keyed):
    URL = "https://api.exa.ai/search"

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("exa", "exa", "fast", paid=True, zero_marginal_cost=False, has_dates=True, label="Exa")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"query": query[:400], "type": "auto", "numResults": max_results,
                                "contents": {"highlights": {"maxCharacters": 400}}}
        if freshness in FRESH_DAYS:
            from datetime import datetime, timedelta, timezone
            since = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS[freshness])
            body["startPublishedDate"] = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        data = await self._http.post_json(self.URL, body, headers={"x-api-key": self._key})
        rows = _require(data, "results")
        return _hits(self.info.name, rows, title="title", url="url", date="publishedDate",
                     snippet=lambda r: " ".join(r.get("highlights") or []) or r.get("summary") or r.get("text") or "")


class BraveProvider(_Keyed):
    URL = "https://api.search.brave.com/res/v1/web/search"
    FRESH = {"today": "pd", "recent": "pm"}

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("brave", "brave", "fast", paid=True, zero_marginal_cost=False, has_dates=True,
                                 label="Brave")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        params: dict[str, Any] = {"q": query[:400], "count": max_results, "extra_snippets": "true"}
        if freshness in self.FRESH:
            params["freshness"] = self.FRESH[freshness]
        data = await self._http.get_json(self.URL, params, headers={"X-Subscription-Token": self._key,
                                                                    "Accept": "application/json"})
        web = data.get("web") if isinstance(data, dict) else None
        if web is None:
            if isinstance(data, dict) and "type" in data:
                return []  # 合法响应但没有网页结果
            raise SourceError(Outcome.UNKNOWN, "返回格式不符")
        return _hits(self.info.name, web.get("results") or [], title="title", url="url", date="page_age",
                     snippet=lambda r: _first(r.get("description"), " ".join(r.get("extra_snippets") or [])))


class FirecrawlProvider(_Keyed):
    URL = "https://api.firecrawl.dev/v2/search"
    TBS = {"today": "qdr:d", "recent": "qdr:m"}

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("firecrawl", "firecrawl", "medium", paid=True, zero_marginal_cost=False,
                                 has_dates=False, label="Firecrawl")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"query": query[:400], "limit": max_results, "sources": [{"type": "web"}]}
        if freshness in self.TBS:
            body["tbs"] = self.TBS[freshness]
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"},
                                          read_timeout=30.0)
        if isinstance(data, dict) and data.get("success") is False:
            raise SourceError(Outcome.REJECTED, _clip(data.get("error") or "Firecrawl 拒绝", 80))
        rows = _require(data, "data", "web")
        return _hits(self.info.name, rows, title="title", url="url", snippet="description")


class LinkupProvider(_Keyed):
    URL = "https://api.linkup.so/v1/search"

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("linkup", "linkup", "medium", paid=True, zero_marginal_cost=False, has_dates=False,
                                 label="Linkup")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"q": query[:400], "depth": "standard", "outputType": "searchResults",
                                "maxResults": max_results}
        if freshness in FRESH_DAYS:
            from datetime import date, timedelta
            body["fromDate"] = (date.today() - timedelta(days=FRESH_DAYS[freshness])).isoformat()
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"},
                                          read_timeout=30.0)
        rows = [r for r in _require(data, "results") if isinstance(r, dict) and r.get("type", "text") == "text"]
        return _hits(self.info.name, rows, title="name", url="url", snippet="content")


class ParallelProvider(_Keyed):
    URL = "https://api.parallel.ai/v1/search"

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("parallel", "parallel", "fast", paid=True, zero_marginal_cost=False,
                                 has_dates=True, label="Parallel")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"objective": query[:400], "search_queries": [query[:200]], "mode": "fast",
                                "advanced_settings": {"max_results": max_results}}
        data = await self._http.post_json(self.URL, body, headers={"x-api-key": self._key})
        rows = _require(data, "results")
        return _hits(self.info.name, rows, title="title", url="url", date="publish_date",
                     snippet=lambda r: " ".join(r.get("excerpts") or []))


class SerperProvider(_Keyed):
    URL = "https://google.serper.dev/search"
    TBS = {"today": "qdr:d", "recent": "qdr:m"}

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        # 返回的是 Google 结果：与 host_web(google 系) 不同族，但与将来的其他 Google 转售商同族
        self.info = ProviderInfo("serper", "google", "fast", paid=True, zero_marginal_cost=False, has_dates=True,
                                 label="Serper（Google）")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"q": query[:400], "num": max_results}
        if freshness in self.TBS:
            body["tbs"] = self.TBS[freshness]
        data = await self._http.post_json(self.URL, body, headers={"X-API-KEY": self._key})
        rows = data.get("organic") if isinstance(data, dict) else None
        if rows is None:
            raise SourceError(Outcome.UNKNOWN, "返回格式不符")
        return _hits(self.info.name, rows, title="title", url="link", snippet="snippet", date="date")


class PerplexityProvider(_Keyed):
    URL = "https://api.perplexity.ai/search"

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("perplexity", "perplexity", "fast", paid=True, zero_marginal_cost=False,
                                 has_dates=True, label="Perplexity")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"query": query[:400], "max_results": max_results}
        if freshness == "today":
            body["search_recency_filter"] = "day"
        elif freshness == "recent":
            body["search_recency_filter"] = "month"
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"})
        rows = _require(data, "results")
        return _hits(self.info.name, rows, title="title", url="url", snippet="snippet", date="date")


# ---------------- 中文 API ----------------
class BochaProvider(_Keyed):
    URL = "https://api.bochaai.com/v1/web-search"
    FRESH = {"today": "oneDay", "recent": "oneMonth"}

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        # TEXT_SOURCE_SURVEY 2.1：与 LangSearch 很可能同源
        self.info = ProviderInfo("bocha", "bocha", "fast", paid=True, zero_marginal_cost=False, has_dates=True,
                                 label="博查")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        # 官方建议 noLimit 由它自己改写时间范围；只有明确要求"今天"时才收窄
        body = {"query": query[:400], "summary": True, "count": max_results,
                "freshness": "oneDay" if freshness == "today" else "noLimit"}
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"},
                                          quota_codes=frozenset({403}))
        if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
            raise SourceError(Outcome.REJECTED, _clip(data.get("msg") or f"博查错误 {data.get('code')}", 80))
        rows = _require(data, "data", "webPages", "value")
        # 只用 datePublished：dateLastCrawled 实为 UTC+8 却标 Z（官方文档承认）
        return _hits(self.info.name, rows, title="name", url="url", date="datePublished",
                     snippet=lambda r: _first(r.get("summary"), r.get("snippet")))


class LangSearchProvider(_Keyed):
    """LangSearch：免费档，每日 token 额度（UTC 0 点重置）。响应格式与博查相同（code/log_id/msg/data.webPages）。
    TEXT_SOURCE_SURVEY 2.1 曾猜测与博查同一索引；2026-10-29 实测同一查询两者结果完全不重合 → 改为独立一族。
    实测中文查询返回大量外文结果，只作免费兜底（排序靠后）。"""

    URL = "https://api.langsearch.com/v1/web-search"
    FRESH = {"today": "oneDay", "recent": "oneMonth"}

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("langsearch", "langsearch", "fast", paid=False, zero_marginal_cost=True, has_dates=True,
                                 label="LangSearch")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {"query": query[:400], "count": max_results,
                                "contents": {"text": {"maxCharacters": 600}}}
        if freshness in self.FRESH:
            body["freshness"] = self.FRESH[freshness]
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"})
        if isinstance(data, dict) and data.get("code") not in (None, 200, "200"):
            raise SourceError(Outcome.REJECTED, _clip(data.get("msg") or f"LangSearch 错误 {data.get('code')}", 80))
        rows = _require(data, "data", "webPages", "value")
        return _hits(self.info.name, rows, title="name", url="url", date="datePublished",
                     snippet=lambda r: _first(r.get("text"), r.get("summary"), r.get("snippet")))


def baidu_trim(query: str, limit: int = 72) -> str:
    """百度千帆查询上限 72 字符，汉字算 2（TEXT_SOURCE_SURVEY 2.2）。按词截断，不切半个词。"""
    out, n = [], 0
    for tok in re.findall(r"\s+|[\u4e00-\u9fff]|[^\s\u4e00-\u9fff]+", query.strip()):
        w = sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in tok)
        if n + w > limit:
            break
        out.append(tok)
        n += w
    return "".join(out).strip()


class BaiduProvider(_Keyed):
    URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    FRESH = {"today": "week", "recent": "month"}   # 最短档是 week

    def __init__(self, http: Http, api_key: str) -> None:
        super().__init__(http, api_key)
        self.info = ProviderInfo("baidu", "baidu", "fast", paid=True, zero_marginal_cost=False, has_dates=True,
                                 label="百度")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": baidu_trim(query)}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": max_results}],
        }
        if freshness in self.FRESH:
            body["search_recency_filter"] = self.FRESH[freshness]
        data = await self._http.post_json(self.URL, body, headers={"Authorization": f"Bearer {self._key}"})
        if isinstance(data, dict) and data.get("code"):
            msg = str(data.get("message") or data.get("code"))
            quota = bool(re.search(r"quota|limit|额度|余额|欠费", msg, re.I))
            raise SourceError(Outcome.REJECTED, _clip(msg, 80), quota_exhausted=quota)
        rows = [r for r in (_require(data, "references") or []) if isinstance(r, dict) and r.get("type", "web") == "web"]
        hits = _hits(self.info.name, rows, title="title", url="url", date="date",
                     snippet=lambda r: _first(r.get("snippet"), r.get("content")))
        for h in hits:  # 实测 date 形如 "2026-10-28 09:44:03"，不带时区，是北京时间
            if h.published_at and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", h.published_at):
                h.published_at = h.published_at.replace(" ", "T") + "+08:00"
        return hits


class QwenSearchProvider(_Keyed):
    """D33：Agent 类来源只在 deep 档或 discussion 时使用，不进 quick。只取 search_info 里的来源，不用生成的回答。"""

    DEFAULT_BASE = "https://dashscope.aliyuncs.com"
    PATH = "/api/v1/services/aigc/text-generation/generation"
    FRESH = {"today": 7, "recent": 30}

    def __init__(self, http: Http, api_key: str, base_url: str = "", model: str = "qwen3.7-flash") -> None:
        super().__init__(http, api_key)
        self._url = (base_url.strip().rstrip("/") or self.DEFAULT_BASE) + self.PATH
        self._model = model.strip() or "qwen3.7-flash"
        self.info = ProviderInfo("qwen", "qwen", "slow", paid=True, zero_marginal_cost=False, has_dates=False,
                                 label="千问联网搜索", extra={"agent": True})

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        opts: dict[str, Any] = {"enable_source": True, "forced_search": True, "search_strategy": "turbo"}
        if freshness in self.FRESH:
            opts["freshness"] = self.FRESH[freshness]
        body = {
            "model": self._model,
            "input": {"messages": [
                {"role": "system", "content": "只做联网检索，用一句话概括检索到的内容即可。"},
                {"role": "user", "content": query[:400]},
            ]},
            "parameters": {"enable_search": True, "search_options": opts, "result_format": "message",
                           "max_tokens": 64},
        }
        data = await self._http.post_json(self._url, body, headers={"Authorization": f"Bearer {self._key}"},
                                          read_timeout=45.0)
        if isinstance(data, dict) and data.get("code") and not data.get("output"):
            msg = str(data.get("message") or data.get("code"))
            quota = bool(re.search(r"Arrearage|quota|余额|欠费", msg, re.I))
            raise SourceError(Outcome.REJECTED, _clip(msg, 80), quota_exhausted=quota,
                              auth_failed="InvalidApiKey" in str(data.get("code")))
        info = (_require(data, "output") or {}).get("search_info")
        if not isinstance(info, dict):
            # 没有 search_info = 没有触发搜索（限流时不报错也不搜，见阿里文档 FAQ）
            raise SourceError(Outcome.REJECTED, "千问未执行联网搜索（可能触发限流）")
        rows = info.get("search_results") or []
        hits = _hits(self.info.name, rows, title="title", url="url", snippet=lambda r: r.get("site_name") or "")
        return hits[:max_results]


# ---------------- 自建 ----------------
class SearxngProvider:
    TIME = {"today": "day", "recent": "month"}

    def __init__(self, http: Http, base_url: str, *, engines: str = "", family: str = "searxng") -> None:
        self._http = http
        self._base = base_url.strip().rstrip("/")
        self._engines = engines.strip()
        self.info = ProviderInfo("searxng", family or "searxng", "medium", paid=False, zero_marginal_cost=True,
                                 has_dates=True, label="SearXNG")

    async def search(self, query: str, *, max_results: int, freshness: str = "any") -> list[Hit]:
        params: dict[str, Any] = {"q": query[:400], "format": "json"}
        if self._engines:
            params["engines"] = self._engines
        if freshness in self.TIME:
            params["time_range"] = self.TIME[freshness]
        try:
            data = await self._http.get_json(self._base + "/search", params)
        except SourceError as err:
            if err.status == 403:
                raise SourceError(Outcome.REJECTED, "SearXNG 未开启 JSON 格式（settings.yml 的 search.formats 需加 json）",
                                  status=403, unavailable=True) from None
            raise
        rows = _require(data, "results")
        return _hits(self.info.name, rows, title="title", url="url", snippet="content",
                     date="publishedDate")[:max_results]
