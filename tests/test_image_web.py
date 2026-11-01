"""D54：内置 Yandex / Bing（MockTransport；结构按 2026-10-29 实测的真实响应仿造，不含真实页面）。"""

import html
import json

import httpx
import pytest

from hub.image_web import BingImageProvider, YandexImageProvider, parse_bing_guess, parse_yandex
from hub.net import Outcome, SourceError


def yandex_page(sites, tags=("rize tedeza", "аниме")):
    state = {"initialState": {"cbirSites": {"sites": sites},
                              "cbirTags": {"tags": [{"text": t, "url": "/x"} for t in tags]}}}
    attr = html.escape(json.dumps(state, ensure_ascii=False), quote=True)
    return f'<html><body><div class="Root" id="ImagesApp-abc" data-state="{attr}"></div></body></html>'


SITES = [
    {"title": "Nico Nico Rize - GochiUsa", "url": "https://www.youtube.com/watch?v=Ery&utm_medium=organic&utm_source=yandex",
     "domain": "youtube.com", "description": "<b>Rize</b> 片段", "thumb": {"url": "//avatars.mds.yandex.net/i?id=1"},
     "originalImage": {"url": "https://i.ytimg.com/vi/Ery/maxresdefault.jpg", "width": 1280, "height": 720}},
    {"title": "dup", "url": "https://www.youtube.com/watch?v=Ery&utm_source=yandex", "domain": "youtube.com",
     "thumb": {"url": "//a"}, "originalImage": {}},
    {"title": "bad", "url": "javascript:alert(1)", "domain": "x"},
]


def transport(routes, log=None):
    def h(req: httpx.Request):
        if log is not None:
            log.append((req.method, str(req.url), dict(req.headers)))
        for key, fn in routes:
            if key in str(req.url):
                return fn(req)
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_parse_yandex_ok_and_changed():
    sites, tags = parse_yandex(yandex_page(SITES))
    assert len(sites) == 3 and tags == ["rize tedeza", "аниме"]
    with pytest.raises(SourceError, match="data-state"):
        parse_yandex("<html>no state</html>")
    with pytest.raises(SourceError, match="cbirSites"):
        parse_yandex('<div id="ImagesApp-x" data-state="{&quot;initialState&quot;:{}}"></div>')


async def test_yandex_upload_redirect_and_mapping():
    log = []
    routes = [("cbir_id=", lambda r: httpx.Response(200, text=yandex_page(SITES))),
              ("/images/search", lambda r: httpx.Response(302, headers={"location": "/images/search?cbir_id=1/2&rpt=imageview"}))]
    p = YandexImageProvider(transport=transport(routes, log))
    hits = await p.search_image(b"\xff\xd8jpeg", max_results=5)
    assert log[0][0] == "POST" and "rpt=imageview" in log[0][1] and log[0][2]["accept-encoding"] == "identity"
    assert "Mozilla" in log[0][2]["user-agent"]
    pages = [h for h in hits if h.kind == "visual"]
    assert len(pages) == 1 and pages[0].url == "https://www.youtube.com/watch?v=Ery"          # 去 utm、去重、丢非法 URL
    assert pages[0].extra["thumbnail"] == "https://avatars.mds.yandex.net/i?id=1" and "1280×720" in pages[0].snippet
    assert "<b>" not in pages[0].snippet and pages[0].level == "weak"
    guess = [h for h in hits if h.kind == "guess"]
    assert guess and "rize tedeza" in guess[0].title


async def test_yandex_captcha_and_offsite_redirect():
    cap = [("/images/search", lambda r: httpx.Response(302, headers={"location": "https://yandex.com/showcaptcha?x=1"}))]
    with pytest.raises(SourceError) as ei:
        await YandexImageProvider(transport=transport(cap)).search_image(b"x")
    assert ei.value.outcome is Outcome.REJECTED and ei.value.retry_after
    body = [("/images/search", lambda r: httpx.Response(200, text='<form action="/checkcaptcha">'))]
    with pytest.raises(SourceError, match="验证"):
        await YandexImageProvider(transport=transport(body)).search_image(b"x")
    off = [("/images/search", lambda r: httpx.Response(302, headers={"location": "https://evil.example/"}))]
    with pytest.raises(SourceError, match="非预期"):
        await YandexImageProvider(transport=transport(off)).search_image(b"x")
    gz = [("/images/search", lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x"))]
    with pytest.raises(SourceError, match="压缩"):
        await YandexImageProvider(transport=transport(gz)).search_image(b"x")
    loop = [("/images/search", lambda r: httpx.Response(302, headers={"location": "/images/search?a=1"}))]
    with pytest.raises(SourceError, match="跳转|上限"):
        await YandexImageProvider(transport=transport(loop)).search_image(b"x")
    lim = [("/images/search", lambda r: httpx.Response(429))]
    with pytest.raises(SourceError) as ei:
        await YandexImageProvider(transport=transport(lim)).search_image(b"x")
    assert ei.value.status == 429


def test_parse_bing_guess():
    assert parse_bing_guess("/search?q=Gochuumon%20Wa%20Usagi%20Desu%20Ka%3F%20Rize&bcid=x") == "Gochuumon Wa Usagi Desu Ka? Rize"
    assert parse_bing_guess("/search?q=Bing%20Visual%20Search&bcid=x") == ""
    assert parse_bing_guess("/search?bcid=x") == ""


async def test_bing_guess_flow():
    log = []
    routes = [("iss=sbiupload", lambda r: httpx.Response(302, headers={"location": "/images/search?view=detailV2&insightsToken=bcid_AB.c-1&iss=SBIUPLOADGET"})),
              ("insightsToken=", lambda r: httpx.Response(302, headers={"location": "/search?q=Gochuumon%20Wa%20Usagi%20Desu%20Ka%3F%20Rize&bcid=y"}))]
    hits = await BingImageProvider(transport=transport(routes, log)).search_image(b"img")
    assert [h.kind for h in hits] == ["guess"] and hits[0].extra["guess"] == "Gochuumon Wa Usagi Desu Ka? Rize"
    assert log[0][0] == "POST" and "iss=sbiupload" in log[0][1]
    generic = [routes[0], ("insightsToken=", lambda r: httpx.Response(302, headers={"location": "/search?q=Bing%20Visual%20Search"}))]
    assert await BingImageProvider(transport=transport(generic)).search_image(b"img") == []
    broken = [("iss=sbiupload", lambda r: httpx.Response(200, text="<html></html>"))]
    with pytest.raises(SourceError, match="流程已变化"):
        await BingImageProvider(transport=transport(broken)).search_image(b"img")


def test_plugin_builds_web_engines_by_config():
    from hub.config import HubConfig
    from hub.net import Http
    from plugin import build_image_providers
    cfg = HubConfig()
    names = [p.info.name for p in build_image_providers(cfg, Http())]
    assert names == ["tracemoe", "animetrace", "img_yandex", "img_bing"]
    cfg.image.web_engines = "bing, nope"
    assert [p.info.name for p in build_image_providers(cfg, Http())][-1] == "img_bing"
    cfg.image.web_enabled = False
    assert not any(p.info.extra.get("web") for p in build_image_providers(cfg, Http()))
