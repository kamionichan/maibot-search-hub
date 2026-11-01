"""第 4 阶段：以图搜（合成图 + MockTransport，不联网、不用真实图片）。"""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image, ImageDraw

from hub.health import Health
from hub.image import ImageInputError, dhash_bytes, hamming, prepare_image
from hub.image_exec import ImageExecutor, make_image_plan
from hub.image_sources import AnimeTraceProvider, ContainerImageProvider, SauceNaoProvider, TraceMoeProvider
from hub.net import Http, Outcome, SourceError


def synth(w=320, h=240, color=(200, 80, 60), fmt="PNG", seed=0, alpha=False):
    mode = "RGBA" if alpha else "RGB"
    img = Image.new(mode, (w, h), (255, 255, 255, 0) if alpha else "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20 + seed, 20, w // 2, h // 2), fill=color)
    d.ellipse((w // 2, h // 3, w - 20, h - 20 - seed), fill=(30, 120, 200))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def scaled(data, w, h, fmt="JPEG"):
    """同一张图缩放 + 重编码（模拟 CDN 缩略图）。"""
    img = Image.open(io.BytesIO(data)).convert("RGB").resize((w, h))
    buf = io.BytesIO()
    img.save(buf, fmt, quality=70)
    return buf.getvalue()


def http_with(handler):
    return Http(transport=httpx.MockTransport(handler))


# ---------------- 清洗与 dHash ----------------
def test_prepare_strips_metadata_and_flattens_alpha():
    src = Image.new("RGB", (64, 64), "red")
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"     # Make
    buf = io.BytesIO()
    src.save(buf, "JPEG", exif=exif.tobytes())
    p = prepare_image(buf.getvalue())
    out = Image.open(io.BytesIO(p.jpeg))
    assert out.format == "JPEG" and not out.getexif() and b"SecretCamera" not in p.jpeg
    pa = prepare_image(synth(alpha=True))
    assert Image.open(io.BytesIO(pa.jpeg)).getpixel((5, 5)) == pytest.approx((255, 255, 255), abs=3)


def test_prepare_rejects_bad_input():
    with pytest.raises(ImageInputError):
        prepare_image(b"not an image")
    with pytest.raises(ImageInputError):
        prepare_image(b"")


def test_dhash_same_vs_different():
    src = synth()
    a = prepare_image(src)
    a_small = dhash_bytes(scaled(src, 160, 120))
    b = dhash_bytes(synth(color=(20, 200, 20), seed=60))
    assert hamming(a.dhash, a_small) <= 8
    assert hamming(a.dhash, b) > 8


# ---------------- 各引擎适配器 ----------------
TRACE_OK = {"frameCount": 1, "error": "", "quota": 100, "quotaUsed": 1, "result": [
    {"anilist": {"id": 21034, "title": {"native": "ご注文はうさぎですか？？", "romaji": "Gochuumon wa Usagi desu ka??",
                                        "chinese": "請問您今天要來點兔子嗎？？", "english": None},
                 "isAdult": False, "siteUrl": "https://anilist.co/anime/21034", "seasonYear": 2015},
     "filename": "x.mp4", "episode": 1, "from": 272.1, "to": 282.6, "at": 279.7, "similarity": 0.992},
    {"anilist": {"id": 21034, "title": {"native": "dup"}, "isAdult": False, "siteUrl": "https://anilist.co/anime/21034"},
     "episode": 1, "at": 280, "similarity": 0.99},
    {"anilist": {"id": 9, "title": {"romaji": "Adult"}, "isAdult": True, "siteUrl": "https://anilist.co/anime/9"},
     "episode": 2, "at": 10, "similarity": 0.95},
    {"anilist": {"id": 5, "title": {"romaji": "Weak"}, "isAdult": False, "siteUrl": "https://anilist.co/anime/5"},
     "episode": 3, "at": 10, "similarity": 0.57}]}


async def test_tracemoe_mapping_dedup_and_adult_filter():
    seen = {}

    def h(req):
        seen["url"], seen["ct"], seen["n"] = str(req.url), req.headers["content-type"], len(req.content)
        return httpx.Response(200, json=TRACE_OK)

    hits = await TraceMoeProvider(http_with(h)).search_image(b"\xff\xd8jpeg", max_results=3)
    assert seen["url"].endswith("/search?anilistInfo") and seen["ct"] == "image/jpeg" and seen["n"] == 6
    assert [x.level for x in hits] == ["hit", "weak"]                   # 去重 + 成人向过滤
    assert hits[0].title == "請問您今天要來點兔子嗎？？" and "第 1 集" in hits[0].snippet and "4:39" in hits[0].snippet
    adult = await TraceMoeProvider(http_with(h), allow_adult=True).search_image(b"x", max_results=3)
    assert any(x.nsfw for x in adult)


@pytest.mark.parametrize("status,body,check", [
    (402, {"error": "Search quota depleted (quota per 24 hours: 100, used: 100)"}, lambda e: e.quota_exhausted),
    (402, {"error": "Concurrency limit exceeded"}, lambda e: not e.quota_exhausted and e.retry_after),
    (400, {"error": "Failed to process image"}, lambda e: e.outcome is Outcome.REJECTED),
    (503, {"error": "Search queue is full"}, lambda e: e.outcome is Outcome.UNKNOWN),
])
async def test_tracemoe_errors(status, body, check):
    with pytest.raises(SourceError) as ei:
        await TraceMoeProvider(http_with(lambda r: httpx.Response(status, json=body))).search_image(b"x")
    assert check(ei.value)


async def test_animetrace_mapping_and_errors():
    seen = {}

    def ok(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"code": 0, "ai": False, "trace_id": "t", "data": [
            {"box": [0.1, 0.1, 0.5, 0.5], "box_id": "b", "not_confident": False,
             "character": [{"work": "ご注文はうさぎですか？", "character": "天々座理世"}, {"work": "w", "character": "备选"}]}]})

    hits = await AnimeTraceProvider(http_with(ok)).search_image(b"abc")
    assert seen["body"]["base64"] == base64.b64encode(b"abc").decode() and seen["body"]["is_multi"] == 1
    assert hits[0].title == "天々座理世（ご注文はうさぎですか？）" and hits[0].level == "likely" and hits[1].level == "weak"
    with pytest.raises(SourceError) as ei:
        await AnimeTraceProvider(http_with(lambda r: httpx.Response(500, json={"code": 17722, "zh_message": "图片下载失败"}))).search_image(b"x")
    assert "图片下载失败" in ei.value.message
    empty = await AnimeTraceProvider(http_with(lambda r: httpx.Response(200, json={"code": 0, "data": []}))).search_image(b"x")
    assert empty == []


SAUCE_OK = {"header": {"status": 0, "long_remaining": 99, "short_remaining": 3}, "results": [
    {"header": {"similarity": "93.5", "index_id": 5, "index_name": "Index #5: Pixiv Images - 1.jpg", "hidden": 0,
                "thumbnail": "https://img3.saucenao.com/a.jpg"},
     "data": {"title": "作品", "member_name": "画师A", "pixiv_id": 123, "ext_urls": ["https://www.pixiv.net/artworks/123"]}},
    {"header": {"similarity": "70", "index_name": "Index #9: Danbooru", "hidden": 1},
     "data": {"ext_urls": ["https://danbooru.donmai.us/post/show/1"]}},
    {"header": {"similarity": "85.5", "index_name": "Index #21: Anime", "thumbnail": "https://img3.saucenao.com/b.jpg"},
     "data": {"source": "某番", "part": "3", "est_time": "00:12:34 / 00:24:00", "ext_urls": ["https://anidb.net/anime/1"]}},
    {"header": {"similarity": "75", "index_name": "Index #34: deviantArt"},
     "data": {"title": "弱", "ext_urls": ["https://deviantart.com/view/1"]}},
    {"header": {"similarity": "52", "index_name": "Index #34: deviantArt"},
     "data": {"title": "丢弃", "ext_urls": ["https://deviantart.com/view/2"]}}]}


async def test_saucenao_mapping_hidden_filter_and_multipart():
    seen = {}

    def h(req):
        seen["url"], seen["ctype"] = str(req.url), req.headers["content-type"]
        return httpx.Response(200, json=SAUCE_OK)

    hits = await SauceNaoProvider(http_with(h), "k").search_image(b"jpegbytes", max_results=5)
    assert "output_type=2" in seen["url"] and "api_key=k" in seen["url"] and "hide=2" in seen["url"]
    assert seen["ctype"].startswith("multipart/form-data")
    assert [x.level for x in hits] == ["hit", "likely", "weak"] and hits[0].url == "https://www.pixiv.net/artworks/123"
    assert "画师A" in hits[0].snippet and hits[1].kind == "scene" and "第 3 集" in hits[1].snippet
    assert not hits[0].extra["thumbnail"] and hits[1].extra["thumbnail"].endswith("b.jpg")   # 只给疑似项做验证


async def test_saucenao_allow_adult_keeps_and_labels_hidden():
    """0.5.7：开启 allow_adult 时，标为隐藏的结果保留并标注成人向；关闭时过滤（见上一条）。"""
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json=SAUCE_OK)

    hits = await SauceNaoProvider(http_with(h), "k", allow_adult=True).search_image(b"x", max_results=5)
    assert "hide=0" in seen["url"]
    adult = [x for x in hits if "danbooru" in x.url]
    assert adult and adult[0].nsfw and not any(x.nsfw for x in hits if x not in adult)
    from hub.image_exec import _to_result
    r = _to_result(adult, [], ["saucenao"], [])
    assert "成人向" in r.results[0].snippet


async def test_saucenao_errors():
    with pytest.raises(SourceError) as ei:
        await SauceNaoProvider(http_with(lambda r: httpx.Response(200, json={"header": {"status": -1, "message": "Invalid API key"}})), "k").search_image(b"x")
    assert ei.value.auth_failed
    with pytest.raises(SourceError) as ei:
        await SauceNaoProvider(http_with(lambda r: httpx.Response(200, text="<html>Just a moment...</html>")), "k").search_image(b"x")
    assert ei.value.outcome is Outcome.UNKNOWN
    with pytest.raises(SourceError) as ei:
        await SauceNaoProvider(http_with(lambda r: httpx.Response(429, json={})), "k").search_image(b"x")
    assert ei.value.status == 429


async def test_container_request_and_errors():
    seen = {}

    def h(req):
        seen["auth"], seen["body"] = req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"engine": "yandex", "candidates": [
            {"title": "页面", "url": "https://example.org/p", "source": "example.org", "match_type": "source_candidate"},
            {"title": "", "url": "javascript:x", "source": "", "match_type": "visual"}]})

    p = ContainerImageProvider(http_with(h), "http://pic:8080/", "secret", "yandex")
    hits = await p.search_image(b"img")
    assert seen["auth"] == "Bearer secret" and seen["body"]["engine"] == "yandex" and seen["body"]["max_results"] == 5
    assert len(hits) == 1 and hits[0].level == "weak" and p.info.name == "imgc_yandex"
    for status, body, check in [(429, {"error": "busy"}, lambda e: e.retry_after),
                                (502, {"error": "search_failed", "reason": "access_limited"}, lambda e: e.outcome is Outcome.REJECTED),
                                (502, {"error": "search_failed", "reason": "parse_failed"}, lambda e: e.outcome is Outcome.UNKNOWN),
                                (401, {"error": "unauthorized"}, lambda e: e.auth_failed)]:
        with pytest.raises(SourceError) as ei:
            await ContainerImageProvider(http_with(lambda r, s=status, b=body: httpx.Response(s, json=b)), "http://pic", "k", "bing").search_image(b"x")
        assert check(ei.value), status


# ---------------- 编排 ----------------
class FakeImg:
    def __init__(self, name, hits=None, error=None, container=False, delay=0.0, web=False):
        from hub.providers import ProviderInfo
        self.info = ProviderInfo(name, name, "medium", False, True, False,
                                 extra={"image": True, "container": container, "web": web})
        self.hits, self.error, self.calls, self.delay = hits or [], error, 0, delay

    async def search_image(self, jpeg, *, max_results=3):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.hits)


def ih(level, url="https://a.example.com/x", kind="source", src="", **extra):
    from hub.image_sources import ImageHit
    return ImageHit("t", url, "s", kind, 0.9, level, src, extra=extra)


class Prep:
    jpeg, dhash, sha1 = b"x", 0, "h"


def test_routes_by_intent():
    ps = [FakeImg("tracemoe"), FakeImg("saucenao"), FakeImg("animetrace"), FakeImg("img_yandex", web=True),
          FakeImg("img_bing", web=True), FakeImg("imgc_google_lens", container=True)]
    names = lambda plan: [p.info.name for p in plan.steps]
    assert names(make_image_plan(ps, Health(), depth="normal", intent="identify_scene", image=Prep())) == [
        "tracemoe", "saucenao", "img_yandex"]
    assert names(make_image_plan(ps, Health(), depth="normal", intent="identify_character", image=Prep()))[0] == "animetrace"
    assert names(make_image_plan(ps, Health(), depth="normal", intent="identify_person", image=Prep())) == [
        "img_yandex", "img_bing", "imgc_google_lens"]
    assert names(make_image_plan(ps, Health(), depth="deep", intent="auto", image=Prep())) == [
        "saucenao", "tracemoe", "img_yandex", "animetrace", "img_bing", "imgc_google_lens"]
    assert names(make_image_plan(ps, Health(), depth="quick", intent="auto", image=Prep())) == ["saucenao"]
    with pytest.raises(SourceError) as ei:
        make_image_plan(ps[:3], Health(), depth="normal", intent="similar_image", image=Prep())
    assert "Yandex" in ei.value.message


async def test_stops_on_hit_and_falls_back_on_error():
    a = FakeImg("saucenao", error=SourceError(Outcome.REJECTED, "额度或频率超限", status=429))
    b = FakeImg("tracemoe", hits=[ih("hit", kind="scene")])
    c = FakeImg("animetrace", hits=[ih("likely", kind="character")])
    plan = make_image_plan([a, b, c], Health(), depth="normal", intent="auto", image=Prep())
    r = await ImageExecutor(Health()).run(plan, "q")
    assert a.calls == 1 and b.calls == 1 and c.calls == 0          # 命中即停
    assert r.used_sources == ["tracemoe"] and not r.missing and r.results[0].snippet.startswith("[命中·番剧截图]")


async def test_no_hit_marks_missing_image_weak():
    a = FakeImg("tracemoe", hits=[ih("weak", kind="scene")])
    r = await ImageExecutor(Health()).run(make_image_plan([a], Health(), depth="normal", intent="identify_scene",
                                                          image=Prep()), "q")
    assert r.missing == ["image_weak"] and "[弱线索" in r.results[0].snippet


async def test_container_candidates_verified_by_dhash():
    src = synth()
    target = prepare_image(src)
    same = scaled(src, 200, 150)
    other = synth(color=(20, 200, 20), seed=60)

    class Reader:
        async def fetch_bytes(self, url, *, max_bytes, accept):
            if "fail" in url:
                raise SourceError(Outcome.NOT_SENT, "连接失败")
            return same if "same" in url else other

    cont = FakeImg("imgc_google_lens", container=True, hits=[ih("weak", "https://cdn.example.com/same.jpg", kind="visual"),
                                                        ih("weak", "https://cdn.example.com/other.png", kind="visual"),
                                                        ih("weak", "https://cdn.example.com/fail.jpg", kind="visual")])

    class P2:
        jpeg, dhash, sha1 = target.jpeg, target.dhash, target.sha1

    r = await ImageExecutor(Health(), Reader()).run(
        make_image_plan([cont], Health(), depth="normal", intent="similar_image", image=P2()), "q")
    tags = {m.url.rsplit("/", 1)[1]: m.snippet.split("]")[0] for m in r.results}
    assert tags["same.jpg"] == "[命中·相似图片·同图" and "缩略图不符" in tags["other.png"] and "未验证" in tags["fail.jpg"]
    assert not r.missing


async def test_all_engines_failed_reports_error():
    a = FakeImg("tracemoe", error=SourceError(Outcome.REJECTED, "额度用尽", quota_exhausted=True))
    r = await ImageExecutor(Health()).run(make_image_plan([a], Health(), depth="quick", intent="identify_scene",
                                                          image=Prep()), "q")
    assert not r.used_sources and r.error_kind == "rejected" and "额度" in r.error


async def test_saucenao_likely_upgraded_only_when_thumbnail_matches():
    src = synth()
    target = prepare_image(src)

    class Reader:
        async def fetch_bytes(self, url, *, max_bytes, accept):
            return scaled(src, 150, 112) if "same" in url else synth(color=(20, 200, 20), seed=60)

    class P2:
        jpeg, dhash, sha1 = target.jpeg, target.dhash, target.sha1

    eng = FakeImg("saucenao", hits=[ih("likely", "https://www.pixiv.net/a", thumbnail="https://img3.saucenao.com/same.jpg"),
                                    ih("likely", "https://deviantart.com/b", thumbnail="https://img3.saucenao.com/other.jpg")])
    eng.info.extra["verify_thumbs"] = True
    r = await ImageExecutor(Health(), Reader()).run(
        make_image_plan([eng], Health(), depth="normal", intent="image_source", image=P2()), "q")
    tags = {m.url: m.snippet.split("]")[0] for m in r.results}
    assert tags["https://www.pixiv.net/a"] == "[命中·出处·同图"
    assert tags["https://deviantart.com/b"] == "[疑似·出处·缩略图不符"


def test_057_prompts():
    """0.5.7：工具说明劝阻闲聊搜索；参考资料提示要求先用资料、允许引用原文。"""
    from hub import cards
    assert "闲聊" in cards.TOOL_DESCRIPTION and "原句" in cards.TOOL_DESCRIPTION
    block = cards.evidence_block([{"sn": 1, "url": "https://a.test/", "title": "t", "snippet": "s",
                                   "published_at": None, "last_seen": 0}])
    assert "先把资料里的答案告诉对方" in block and "原文" in block
