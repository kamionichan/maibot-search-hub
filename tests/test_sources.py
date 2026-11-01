"""第 3 阶段：各来源适配器（MockTransport，按官方文档构造 fixture，不联网）。"""

import json

import httpx
import pytest

from hub.net import Http, Outcome, SourceError
from hub.sources import (BaiduProvider, BochaProvider, LangSearchProvider, BraveProvider, ExaProvider, FirecrawlProvider, LinkupProvider,
                         ParallelProvider, PerplexityProvider, QwenSearchProvider, SearxngProvider, SerperProvider,
                         baidu_trim)


def http_with(handler):
    return Http(transport=httpx.MockTransport(handler))


def capture(status=200, payload=None, headers=None):
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(status, json=payload if payload is not None else {}, headers=headers or {})

    return seen, handler


# ---------------- 请求构造 + 字段映射 ----------------
async def test_exa():
    seen, h = capture(payload={"requestId": "x", "results": [
        {"title": "LLM 综述", "url": "https://arxiv.org/abs/2307.06435", "publishedDate": "2023-11-16T01:36:32.547Z",
         "highlights": ["Such requirements", "have limited"]},
        {"title": "bad", "url": "javascript:alert(1)"}], "costDollars": {"total": 0.007}})
    hits = await ExaProvider(http_with(h), "k").search("llm", max_results=5, freshness="recent")
    assert seen["headers"]["x-api-key"] == "k" and seen["body"]["numResults"] == 5
    assert "startPublishedDate" in seen["body"] and seen["body"]["contents"]["highlights"]["maxCharacters"] == 400
    assert len(hits) == 1 and hits[0].snippet == "Such requirements have limited" and hits[0].published_at.startswith("2023")


async def test_brave_get_params_and_mapping():
    seen, h = capture(payload={"type": "search", "web": {"results": [
        {"title": "Brave", "url": "https://brave.com/", "description": "", "extra_snippets": ["额外 1", "额外 2"],
         "page_age": "2026-10-01T00:00:00"}]}})
    hits = await BraveProvider(http_with(h), "k").search("brave", max_results=3, freshness="today")
    assert seen["method"] == "GET" and "freshness=pd" in seen["url"] and "count=3" in seen["url"]
    assert seen["headers"]["x-subscription-token"] == "k"
    assert hits[0].snippet == "额外 1 额外 2" and hits[0].published_at.startswith("2026")


async def test_brave_no_web_section_is_empty_not_error():
    _, h = capture(payload={"type": "search", "query": {"original": "x"}})
    assert await BraveProvider(http_with(h), "k").search("x", max_results=3) == []


async def test_firecrawl():
    seen, h = capture(payload={"success": True, "data": {"web": [
        {"title": "Firecrawl", "description": "描述", "url": "https://firecrawl.dev"}]}, "creditsUsed": 2})
    hits = await FirecrawlProvider(http_with(h), "k").search("q", max_results=4, freshness="recent")
    assert seen["headers"]["authorization"] == "Bearer k" and seen["body"]["tbs"] == "qdr:m"
    assert seen["body"]["sources"] == [{"type": "web"}] and hits[0].snippet == "描述"


async def test_linkup_required_fields_and_skips_images():
    seen, h = capture(payload={"results": [
        {"type": "text", "name": "微软年报", "url": "https://microsoft.com/ar24", "content": "增长 23%", "favicon": ""},
        {"type": "image", "name": "图", "url": "https://img.example.com/a.png"}]})
    hits = await LinkupProvider(http_with(h), "k").search("q", max_results=5)
    assert seen["body"]["q"] == "q" and seen["body"]["depth"] == "standard" and seen["body"]["outputType"] == "searchResults"
    assert [x.title for x in hits] == ["微软年报"]


async def test_parallel():
    seen, h = capture(payload={"search_id": "s", "session_id": "x", "results": [
        {"url": "https://a.example.com", "title": None, "publish_date": "2026-09-01", "excerpts": ["片段一", "片段二"]}]})
    hits = await ParallelProvider(http_with(h), "k").search("q", max_results=5)
    assert seen["headers"]["x-api-key"] == "k" and seen["body"]["search_queries"] == ["q"]
    assert hits[0].title == "https://a.example.com" and hits[0].snippet == "片段一 片段二"


async def test_serper():
    seen, h = capture(payload={"organic": [{"title": "G", "link": "https://g.example.com", "snippet": "s",
                                            "date": "Oct 1, 2026"}]})
    hits = await SerperProvider(http_with(h), "k").search("q", max_results=5, freshness="today")
    assert seen["headers"]["x-api-key"] == "k" and seen["body"] == {"q": "q", "num": 5, "tbs": "qdr:d"}
    assert hits[0].url == "https://g.example.com"


async def test_perplexity():
    seen, h = capture(payload={"results": [{"title": "P", "url": "https://p.example.com", "snippet": "s",
                                            "date": "2026-10-01"}]})
    hits = await PerplexityProvider(http_with(h), "k").search("q", max_results=5, freshness="recent")
    assert seen["body"]["search_recency_filter"] == "month" and hits[0].published_at == "2026-10-01"


async def test_bocha_mapping_uses_date_published_only():
    seen, h = capture(payload={"code": 200, "log_id": "x", "msg": None, "data": {"webPages": {"value": [
        {"name": "博查", "url": "https://bocha.cn/a", "snippet": "短", "summary": "长摘要", "datePublished": "2026-10-01T08:00:00+08:00",
         "dateLastCrawled": "2026-10-02T00:00:00Z"}]}}})
    hits = await BochaProvider(http_with(h), "k").search("原神", max_results=5, freshness="recent")
    assert seen["body"]["freshness"] == "noLimit" and seen["body"]["summary"] is True   # recent 仍交给博查自己改写
    assert hits[0].snippet == "长摘要" and hits[0].published_at.startswith("2026-10-01")


async def test_bocha_403_is_quota():
    _, h = capture(status=403, payload={"code": 403, "msg": "余额不足"})
    with pytest.raises(SourceError) as ei:
        await BochaProvider(http_with(h), "k").search("x", max_results=5)
    assert ei.value.quota_exhausted and not ei.value.auth_failed


def test_baidu_trim_counts_cjk_double():
    q = "原神" * 40
    out = baidu_trim(q)
    assert sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in out) <= 72 and out
    assert baidu_trim("python release schedule 2026") == "python release schedule 2026"


async def test_baidu_request_and_error_code():
    seen, h = capture(payload={"request_id": "r", "references": [
        {"id": 1, "title": "百度", "url": "https://baidu.com/a", "web_anchor": "a", "snippet": "摘要", "date": "2026-10-01",
         "type": "web"},
        {"id": 2, "title": "视频", "url": "https://v.example.com", "type": "video"}]})
    hits = await BaiduProvider(http_with(h), "k").search("原神 版本", max_results=5, freshness="today")
    assert seen["body"]["search_recency_filter"] == "week"
    assert seen["body"]["resource_type_filter"] == [{"type": "web", "top_k": 5}]
    assert [x.title for x in hits] == ["百度"] and hits[0].published_at == "2026-10-01"
    _, h2 = capture(payload={"request_id": "r", "code": "216201", "message": "quota exceeded"})
    with pytest.raises(SourceError) as ei:
        await BaiduProvider(http_with(h2), "k").search("x", max_results=5)
    assert ei.value.outcome is Outcome.REJECTED and ei.value.quota_exhausted


async def test_qwen_only_uses_search_info_sources():
    seen, h = capture(payload={"output": {"choices": [{"message": {"content": "模型自己写的回答，不应进入结果"}}],
                                          "search_info": {"search_results": [
                                              {"site_name": "知乎", "index": 1, "title": "Qwen3-Next 解析",
                                               "url": "https://zhuanlan.zhihu.com/p/1"}]}}})
    hits = await QwenSearchProvider(http_with(h), "k").search("qwen 新模型", max_results=5, freshness="recent")
    opts = seen["body"]["parameters"]["search_options"]
    assert opts["enable_source"] and opts["forced_search"] and opts["freshness"] == 30
    assert seen["url"].startswith("https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation")
    assert [x.title for x in hits] == ["Qwen3-Next 解析"] and "模型自己写" not in hits[0].snippet


async def test_qwen_no_search_info_means_not_searched():
    _, h = capture(payload={"output": {"choices": [{"message": {"content": "直接回答"}}]}})
    with pytest.raises(SourceError) as ei:
        await QwenSearchProvider(http_with(h), "k").search("x", max_results=5)
    assert ei.value.outcome is Outcome.REJECTED


async def test_qwen_custom_base_url():
    seen, h = capture(payload={"output": {"search_info": {"search_results": []}}})
    await QwenSearchProvider(http_with(h), "k", base_url="https://ws1.cn-beijing.maas.aliyuncs.com/").search(
        "x", max_results=5)
    assert seen["url"].startswith("https://ws1.cn-beijing.maas.aliyuncs.com/api/v1/")


async def test_searxng_and_json_disabled():
    seen, h = capture(payload={"results": [{"title": "S", "url": "https://s.example.com", "content": "c",
                                            "publishedDate": None, "engines": ["bing"]}]})
    hits = await SearxngProvider(http_with(h), "http://127.0.0.1:8080/", engines="bing").search(
        "q", max_results=5, freshness="today")
    assert seen["url"].startswith("http://127.0.0.1:8080/search?") and "format=json" in seen["url"]
    assert "time_range=day" in seen["url"] and "engines=bing" in seen["url"] and hits[0].title == "S"
    _, h403 = capture(status=403, payload={})
    with pytest.raises(SourceError) as ei:
        await SearxngProvider(http_with(h403), "http://x").search("q", max_results=5)
    assert ei.value.unavailable and "json" in ei.value.message


# ---------------- 通用错误归类 ----------------
@pytest.mark.parametrize("cls", [ExaProvider, FirecrawlProvider, LinkupProvider, ParallelProvider, SerperProvider,
                                 PerplexityProvider, BochaProvider, BaiduProvider, LangSearchProvider])
async def test_common_errors(cls):
    for status, check in [(401, lambda e: e.auth_failed), (429, lambda e: e.outcome is Outcome.REJECTED),
                          (402, lambda e: e.quota_exhausted), (500, lambda e: e.outcome is Outcome.UNKNOWN)]:
        _, h = capture(status=status, payload={"error": "x"}, headers={"retry-after": "30"} if status == 429 else None)
        with pytest.raises(SourceError) as ei:
            await cls(http_with(h), "k").search("q", max_results=3)
        assert check(ei.value), (cls.__name__, status)
    _, bad = capture(payload={"unexpected": True})
    with pytest.raises(SourceError) as ei:
        await cls(http_with(bad), "k").search("q", max_results=3)
    assert ei.value.outcome is Outcome.UNKNOWN


async def test_connect_error_is_not_sent():
    def boom(req):
        raise httpx.ConnectError("no route")
    with pytest.raises(SourceError) as ei:
        await ExaProvider(http_with(boom), "k").search("q", max_results=3)
    assert ei.value.outcome is Outcome.NOT_SENT


async def test_langsearch_same_family_as_bocha_and_free():
    seen, h = capture(payload={"code": 200, "log_id": "x", "msg": None, "data": {"webPages": {"value": [
        {"name": "LangSearch", "url": "https://langsearch.com", "datePublished": None, "text": "正文片段"}]}}})
    p = LangSearchProvider(http_with(h), "k")
    hits = await p.search("q", max_results=5, freshness="recent")
    assert seen["body"]["freshness"] == "oneMonth" and seen["body"]["contents"] == {"text": {"maxCharacters": 600}}
    assert hits[0].snippet == "正文片段" and p.info.family == "langsearch" and not p.info.paid


async def test_baidu_naive_datetime_is_beijing_time():
    _, h = capture(payload={"request_id": "r", "references": [
        {"id": 1, "title": "t", "url": "https://baidu.com/a", "web_anchor": "a", "date": "2026-10-28 09:44:03"}]})
    hits = await BaiduProvider(http_with(h), "k").search("x", max_results=5)
    assert hits[0].published_at == "2026-10-28T09:44:03+08:00"
