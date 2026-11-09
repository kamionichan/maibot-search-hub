"""0.6.1（D76）：百度识图、Google Lens（SerpApi / camofox）、限频、同图附缩略图。
MockTransport + 合成图，不联网、不用真实图片。结构按 2026-11-08 实测响应仿造（Q53）。"""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from hub.health import Health
from hub.image import prepare_image
from hub.image_exec import ImageExecutor, make_image_plan
from hub.image_more import (BaiduImageProvider, CamofoxLensProvider, SerpApiLensProvider, parse_baidu)
from hub.net import Http, Outcome, SourceError

from test_image import FakeImg, Prep, ih, scaled, synth


def baidu_page(same, guess="动漫角色"):
    cards = [{"cardName": "cardHeader", "tplData": {"pano": {"list": [{"subTitle": guess}]}}},
             {"cardName": "same", "tplData": {"title": "图片来源", "list": same}},
             {"cardName": "simipic", "tplData": {"firstUrl": "https://graph.baidu.com/ajax/pcsimi?x=1"}}]
    return f"<html><script>window.cardData = {json.dumps(cards, ensure_ascii=False)};\nwindow.other=1;</script></html>"


SAME = [{"url": "http://www.duitang.com/blog/?id=1&utm_source=x", "image_src": "http://mms1.baidu.com/it/u=1",
         "title": ["天天座理世 - 堆糖"], "releaseTime": 1771343006000, "isDeadlink": 0, "abstract": "<b>点兔</b>"},
        {"url": "http://www.bilibili.com/video/BV1", "image_src": "", "title": ["【点兔】"], "releaseTime": 0},
        {"url": "http://dead.example.com/", "title": ["dead"], "isDeadlink": 1},
        {"url": "javascript:alert(1)", "title": ["bad"]}]


def route(routes, log=None):
    def h(req: httpx.Request):
        if log is not None:
            log.append((req.method, str(req.url), req))
        for key, fn in routes:
            if key in str(req.url):
                return fn(req)
        return httpx.Response(404)
    return httpx.MockTransport(h)


# ---------------- 百度识图 ----------------
def test_parse_baidu_ok_and_changed():
    same, guess = parse_baidu(baidu_page(SAME))
    assert len(same) == 4 and guess == "动漫角色"
    with pytest.raises(SourceError, match="cardData"):
        parse_baidu("<html>nothing</html>")


async def test_baidu_upload_and_mapping():
    log = []
    routes = [("/upload", lambda r: httpx.Response(200, json={"status": 0, "data": {"url": "https://graph.baidu.com/s?sign=1"}})),
              ("/s?sign=1", lambda r: httpx.Response(200, text=baidu_page(SAME)))]
    p = BaiduImageProvider(transport=route(routes, log))
    hits = await p.search_image(b"\xff\xd8jpeg", max_results=5)
    assert log[0][0] == "POST" and b'name="image"' in log[0][2].read() and b'name="from"' in log[0][2].read()
    pages = [h for h in hits if h.kind == "source"]
    assert [h.url for h in pages] == ["http://www.duitang.com/blog/?id=1", "http://www.bilibili.com/video/BV1"]
    assert pages[0].published_at == "2026-02-17" and pages[0].extra["thumbnail"] == "http://mms1.baidu.com/it/u=1"
    assert pages[0].title == "天天座理世 - 堆糖" and "点兔" in pages[0].snippet and pages[1].published_at is None
    assert hits[-1].kind == "guess" and "动漫角色" in hits[-1].title


async def test_baidu_offsite_redirect_and_captcha():
    off = [("/upload", lambda r: httpx.Response(200, json={"data": {"url": "https://evil.example.com/s"}}))]
    with pytest.raises(SourceError, match="没有接受"):
        await BaiduImageProvider(transport=route(off)).search_image(b"x")
    cap = [("/upload", lambda r: httpx.Response(200, json={"data": {"url": "https://graph.baidu.com/s?x"}})),
           ("/s?x", lambda r: httpx.Response(200, text='<div id="captcha">验证</div>'))]
    with pytest.raises(SourceError) as ei:
        await BaiduImageProvider(transport=route(cap)).search_image(b"x")
    assert ei.value.retry_after == 1800      # 遇验证：冷却，不绕过
    bad = [("/upload", lambda r: httpx.Response(200, text="not json"))]
    with pytest.raises(SourceError, match="不是 JSON"):
        await BaiduImageProvider(transport=route(bad)).search_image(b"x")


# ---------------- SerpApi ----------------
def serp_handler(log, *, upload=None, search=None):
    def h(req):
        log.append(req)
        if req.url.path == "/image":
            return upload or httpx.Response(200, json={"message": "ok", "image_id": "IMG1"})
        return search or httpx.Response(200, json={
            "search_metadata": {"status": "Success"},
            "exact_matches": [{"title": "原帖", "link": "https://www.pixiv.net/artworks/1", "source": "pixiv",
                               "thumbnail": "https://encrypted-tbn0.gstatic.com/a"}],
            "visual_matches": [{"title": "相似", "link": "https://x.com/a/status/2", "source": "X",
                                "thumbnail": "https://encrypted-tbn0.gstatic.com/b"},
                               {"title": "google", "link": "https://www.google.com/search?q=1", "source": "g"}]})
    return h


async def test_serpapi_upload_then_search():
    log = []
    p = SerpApiLensProvider(Http(transport=httpx.MockTransport(serp_handler(log))), "KEY")
    hits = await p.search_image(synth(fmt="JPEG"), max_results=5)
    assert log[0].url.path == "/image" and log[0].url.params["api_key"] == "KEY" and b'name="image"' in log[0].read()
    q = log[1].url.params
    assert q["engine"] == "google_lens" and q["image_id"] == "IMG1" and q["api_key"] == "KEY"
    assert [h.url for h in hits] == ["https://www.pixiv.net/artworks/1", "https://x.com/a/status/2"]   # google 链接丢掉
    assert hits[0].kind == "source" and "完全匹配" in hits[0].snippet and hits[1].kind == "visual"
    assert p.info.paid and p.info.extra["lens"]


async def test_serpapi_shrinks_large_upload():
    log = []
    big = Image.effect_noise((1800, 1800), 120).convert("RGB")
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=95)
    assert len(buf.getvalue()) > 500 * 1024
    await SerpApiLensProvider(Http(transport=httpx.MockTransport(serp_handler(log))), "K").search_image(buf.getvalue())
    assert len(log[0].read()) <= 500 * 1024 + 2000


@pytest.mark.parametrize("resp,check", [
    (httpx.Response(401, json={"error": "Invalid API key."}), lambda e: e.auth_failed),
    (httpx.Response(429, json={"error": "Your account has run out of searches."}), lambda e: e.quota_exhausted),
    (httpx.Response(200, json={"error": "Invalid image format"}), lambda e: e.outcome is Outcome.REJECTED),
    (httpx.Response(502, json={}), lambda e: e.outcome is Outcome.UNKNOWN),
])
async def test_serpapi_errors(resp, check):
    p = SerpApiLensProvider(Http(transport=httpx.MockTransport(serp_handler([], upload=resp))), "K")
    with pytest.raises(SourceError) as ei:
        await p.search_image(b"\xff\xd8x")
    assert check(ei.value)


# ---------------- camofox ----------------
class Camo:
    """仿 camofox REST：/tabs、/tabs/{id}/evaluate、/navigate、DELETE /sessions。"""

    def __init__(self, *, blocked=False, results=None, auth=None):
        self.blocked, self.auth, self.calls, self.uploaded = blocked, auth, [], None
        self.results = results if results is not None else [
            ["Smoggy 去 BLG - 什么值得买", "https://www.google.com/goto?url=AAA", "什么值得买", "https://encrypted-tbn0.gstatic.com/t"],
            ["官方 X", "https://www.google.com/goto?url=BBB", "X", ""],
        ]
        self.real = {"AAA": "https://post.smzdm.com/p/1", "BBB": "https://www.google.com/still-google"}

    def __call__(self, req: httpx.Request):
        self.calls.append((req.method, req.url.path, req.headers.get("authorization")))
        body = json.loads(req.content or b"{}")
        if self.auth and req.headers.get("authorization") != f"Bearer {self.auth}":
            return httpx.Response(403, json={"error": "forbidden"})
        if req.method == "POST" and req.url.path == "/tabs":
            return httpx.Response(200, json={"tabId": "T1", "url": body["url"]})
        if req.url.path.endswith("/evaluate"):
            expr = body["expression"]
            if "form.submit()" in expr:
                b64 = expr.split("atob('", 1)[1].split("'", 1)[0]
                self.uploaded = base64.b64decode(b64)
                return httpx.Response(200, json={"ok": True, "result": "submitted"})
            return httpx.Response(200, json={"ok": True, "result": {
                "blocked": self.blocked, "res": [] if self.blocked else self.results,
                "path": "/sorry/index" if self.blocked else "/search", "host": "www.google.com"}})
        if req.url.path.endswith("/navigate"):
            key = body["url"].split("url=")[1]
            return httpx.Response(200, json={"ok": True, "url": self.real.get(key, "")})
        if req.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


async def test_camofox_lens_flow_and_cleanup():
    cam = Camo()
    p = CamofoxLensProvider("http://camo:9377/", "SECRET", transport=httpx.MockTransport(cam))
    p.POLL = 0.01
    hits = await p.search_image(b"\xff\xd8jpegbytes", max_results=5)
    assert cam.uploaded == b"\xff\xd8jpegbytes"                              # 图片在页面里提交，不需要公网地址
    assert [h.url for h in hits] == ["https://post.smzdm.com/p/1"]          # 仍在 google 的丢掉
    assert hits[0].extra["thumbnail"].startswith("https://encrypted-tbn0") and hits[0].kind == "visual"
    assert cam.calls[-1][0] == "DELETE" and all(c[2] == "Bearer SECRET" for c in cam.calls)


async def test_camofox_blocked_cools_down_and_still_cleans_up():
    cam = Camo(blocked=True)
    p = CamofoxLensProvider("http://camo:9377", transport=httpx.MockTransport(cam))
    p.POLL = 0.01
    with pytest.raises(SourceError) as ei:
        await p.search_image(b"x")
    assert ei.value.retry_after == 7200 and "未尝试绕过" in ei.value.message
    assert cam.calls[-1][0] == "DELETE"
    h = Health()
    h.failure("camofox_lens", ei.value)
    assert not h.available("camofox_lens")


async def test_camofox_auth_failure():
    p = CamofoxLensProvider("http://camo:9377", "WRONG", transport=httpx.MockTransport(Camo(auth="RIGHT")))
    with pytest.raises(SourceError) as ei:
        await p.search_image(b"x")
    assert ei.value.auth_failed


def _fast_pacer(**kw):
    from hub.pace import Pacer
    cfg = dict(min_gap=0.0, jitter=0.0, per_hour=0, max_wait=5.0, step_range=(0.0, 0.0))
    cfg.update(kw)
    return Pacer("测试", **cfg)


async def test_pacer_hourly_cap():
    g = _fast_pacer(per_hour=2)
    for _ in range(2):
        async with g.slot():
            pass
    with pytest.raises(SourceError) as ei:
        async with g.slot():
            pass
    assert ei.value.budget and ei.value.outcome is Outcome.NOT_SENT and "每小时 2 次" in ei.value.message


async def test_rate_limited_lens_is_skipped_not_failure():
    cam = Camo()
    p = CamofoxLensProvider("http://camo:9377", transport=httpx.MockTransport(cam), pacer=_fast_pacer(per_hour=1))
    async with p.pacer.slot():                                              # 已用完
        pass
    plan = make_image_plan([p], Health(), depth="deep", intent="similar_image", image=Prep())
    h = Health()
    r = await ImageExecutor(h).run(plan, "q")
    assert h.available("camofox_lens") and r.attempts[-1].outcome == "skipped" and not cam.calls
    assert r.error_kind == "limited"



# ---------------- 编排：Lens 的位置与条件 ----------------
def lens(name="serpapi_lens", **kw):
    f = FakeImg(name, **kw)
    f.info.extra["lens"] = True
    return f


def names(plan):
    return [p.info.name for p in plan.steps]


def test_routes_include_baidu_and_lens():
    ps = [FakeImg("tracemoe"), FakeImg("saucenao"), FakeImg("animetrace"), FakeImg("img_yandex", web=True),
          FakeImg("img_baidu", web=True), FakeImg("img_bing", web=True), lens()]
    assert names(make_image_plan(ps, Health(), depth="normal", intent="similar_image", image=Prep())) == [
        "img_yandex", "img_baidu", "img_bing", "serpapi_lens"]
    auto = names(make_image_plan(ps, Health(), depth="normal", intent="auto", image=Prep()))
    assert auto[:4] == ["saucenao", "tracemoe", "img_yandex", "img_baidu"] and auto[-1] == "serpapi_lens"
    assert names(make_image_plan(ps, Health(), depth="quick", intent="auto", image=Prep())) == ["saucenao"]  # quick 不补 Lens
    assert names(make_image_plan(ps, Health(), depth="normal", intent="auto", image=Prep(), provider="serpapi_lens"))[0] == "serpapi_lens"


async def test_lens_only_when_nothing_hit():
    a = FakeImg("img_yandex", web=True, hits=[ih("hit", kind="visual")])
    L = lens()
    r = await ImageExecutor(Health()).run(make_image_plan([a, L], Health(), depth="normal", intent="similar_image",
                                                          image=Prep()), "q")
    assert L.calls == 0
    a2 = FakeImg("img_yandex", web=True, hits=[ih("weak", kind="visual")])
    L2 = lens()
    await ImageExecutor(Health()).run(make_image_plan([a2, L2], Health(), depth="normal", intent="similar_image",
                                                      image=Prep()), "q")
    assert L2.calls == 1
    # deep：前面命中但只有一类结果，Lens 照样补查
    L3 = lens()
    a3 = FakeImg("img_yandex", web=True, hits=[ih("hit", kind="visual")])
    await ImageExecutor(Health()).run(make_image_plan([a3, L3], Health(), depth="deep", intent="similar_image",
                                                      image=Prep()), "q")
    assert L3.calls == 1


async def test_serpapi_counts_daily_budget():
    from hub.budget import Budget
    store = {}
    b = Budget(lambda k: store.get(k, 0), lambda k, n: store.__setitem__(k, store.get(k, 0) + n), {"serpapi_lens": 1})
    L = lens()
    L.info.paid = True
    plan = make_image_plan([FakeImg("img_yandex", web=True), L], Health(), depth="normal", intent="similar_image", image=Prep())
    await ImageExecutor(Health(), budget=b).run(plan, "q")
    assert L.calls == 1
    L2 = lens()
    L2.info.paid = True
    plan = make_image_plan([FakeImg("img_yandex", web=True), L2], Health(), depth="normal", intent="similar_image", image=Prep())
    r = await ImageExecutor(Health(), budget=b).run(plan, "q")
    assert L2.calls == 0 and any("上限" in a.note for a in r.attempts)


# ---------------- 同图附缩略图 ----------------
async def test_same_image_thumbs_collected_and_adult_filtered():
    src = synth()
    target = prepare_image(src)
    same = scaled(src, 200, 150)

    class Reader:
        async def fetch_bytes(self, url, *, max_bytes, accept):
            return same

    h1 = ih("weak", "https://cdn.example.com/a.jpg", kind="visual", thumbnail="https://t/a.jpg")
    h2 = ih("weak", "https://cdn.example.com/b.jpg", kind="visual", thumbnail="https://t/b.jpg")
    h2.nsfw = True
    web = FakeImg("img_yandex", web=True, hits=[h1, h2])

    class P2:
        jpeg, dhash, sha1 = target.jpeg, target.dhash, target.sha1

    ex = ImageExecutor(Health(), Reader())
    await ex.run(make_image_plan([web], Health(), depth="normal", intent="similar_image", image=P2()), "q")
    assert len(ex.same_image_thumbs(allow_adult=False)) == 1
    assert len(ex.same_image_thumbs(allow_adult=True)) == 2
    assert len(ex.same_image_thumbs(allow_adult=True, limit=1)) == 1


def test_plugin_builds_new_engines_by_config():
    from hub.config import HubConfig
    from plugin import build_image_providers
    cfg = HubConfig()
    assert [p.info.name for p in build_image_providers(cfg, Http())] == [
        "tracemoe", "animetrace", "img_yandex", "img_baidu", "img_bing"]
    cfg.image.web_engines = "bing"
    assert [p.info.name for p in build_image_providers(cfg, Http())][-2:] == ["img_baidu", "img_bing"]
    cfg.image.baidu_enabled = False
    assert "img_baidu" not in [p.info.name for p in build_image_providers(cfg, Http())]
    cfg.image.camofox_url = "http://camo:9377"
    assert build_image_providers(cfg, Http())[-1].info.name == "camofox_lens"
    cfg.image.serpapi_key = "k"
    names_ = [p.info.name for p in build_image_providers(cfg, Http())]
    assert names_[-1] == "serpapi_lens" and "camofox_lens" not in names_     # 两个都填：只用 SerpApi
    cfg.image.enabled = False
    assert build_image_providers(cfg, Http()) == []


def test_old_container_fields_are_ignored():
    """0.6.1（D77）去掉容器：已装用户 config.toml 里残留的 container_* 不能让配置加载失败。"""
    from hub.config import HubConfig
    cfg = HubConfig.model_validate({"image": {"container_url": "http://picimagesearch-api:8080", "container_key": "k",
                                              "container_engines": "google_lens", "saucenao_key": "s"}})
    assert cfg.image.saucenao_key == "s" and not hasattr(cfg.image, "container_url")



# ---------------- 0.6.1 审查修复（R61-16/17/18/19/26，上传压缩） ----------------
def test_upload_is_compressed_to_1280_and_1mb():
    # 第七次复核找到的偶发失败：原来用 4000×3000 噪声图（解码后约 36 MB），机器内存紧时 MemoryError。
    # 2400×1800 仍大于 1280、压缩前 JPEG 也超过 1 MB，测的东西不变
    from hub.image import prepare_image
    big = Image.effect_noise((2400, 1800), 100).convert("RGB")
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=95)
    p = prepare_image(buf.getvalue())
    assert max(p.width, p.height) == 1280 and len(p.jpeg) <= 1_000_000


async def test_r61_17_camofox_shrinks_to_700k_and_uuid_ids():
    big = Image.effect_noise((1280, 1280), 120).convert("RGB")
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=95)
    data = buf.getvalue()
    assert len(data) > 700 * 1024
    cam = Camo()
    p = CamofoxLensProvider("http://camo:9377", transport=httpx.MockTransport(cam))
    p.POLL = 0.01
    await p.search_image(data)
    assert len(cam.uploaded) <= 700 * 1024
    cam2 = Camo()
    p2 = CamofoxLensProvider("http://camo:9377", transport=httpx.MockTransport(cam2))
    p2.POLL = 0.01
    await p2.search_image(b"\xff\xd8x")
    assert cam.calls[-1][1] != cam2.calls[-1][1]            # 会话 ID 不同（DELETE /sessions/<uid>）


async def test_r61_16_camofox_body_limit_streamed():
    import gzip
    bomb = gzip.compress(b"{" + b" " * (8 * 1024 * 1024) + b"}")

    def h(req):
        return httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})
    p = CamofoxLensProvider("http://camo:9377", transport=httpx.MockTransport(h))
    with pytest.raises(SourceError, match="过大"):
        await p.search_image(b"\xff\xd8x")


async def test_r61_19_cleanup_has_its_own_short_timeout():
    cam = Camo()

    async def slow_delete(req):
        if req.method == "DELETE":
            await asyncio.sleep(10)
        return cam(req)

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            await req.aread()
            return await slow_delete(req)
    p = CamofoxLensProvider("http://camo:9377", transport=T())
    p.POLL, p.CLEANUP_TIMEOUT = 0.01, 0.2
    t0 = asyncio.get_running_loop().time()
    await p.search_image(b"\xff\xd8x")
    assert asyncio.get_running_loop().time() - t0 < 2


async def test_r61_18_cooldown_rechecked_before_each_step():
    from hub.health import Health
    from hub.net import SourceError as SE
    h = Health()
    L = lens()
    a = FakeImg("img_yandex", web=True)
    plan = make_image_plan([a, L], h, depth="deep", intent="similar_image", image=Prep())
    h.failure("serpapi_lens", SE(Outcome.REJECTED, "要求验证", retry_after=7200))   # 建计划之后才进入冷却
    r = await ImageExecutor(h).run(plan, "q")
    assert L.calls == 0 and any(x.outcome == "skipped" for x in r.attempts)



async def test_r61_01_serpapi_error_redacted_before_clip():
    from hub.net import set_secrets
    key = "sk-" + "A" * 40
    set_secrets([key])

    def h(req):
        return httpx.Response(400, json={"error": f"Bad request. Your key {key} has no access to this engine, see docs"})
    p = SerpApiLensProvider(Http(transport=httpx.MockTransport(h)), key)
    with pytest.raises(SourceError) as ei:
        await p.search_image(b"\xff\xd8x")
    assert "AAAA" not in ei.value.message and "***" in ei.value.message



# ---------------- 0.6.1 复审修复 R62-13：清理期间的取消不能被吞 ----------------
async def test_r62_13_cancel_during_cleanup_propagates_and_next_engine_not_called():
    from hub.jobs import Job, JobManager
    from hub.ledger import Ledger
    in_delete = asyncio.Event()
    cam = Camo()

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            await req.aread()
            if req.method == "DELETE":
                in_delete.set()
                await asyncio.sleep(0.5)
            return cam(req)
    p = CamofoxLensProvider("http://camo:9377", transport=T())
    p.POLL = p.OPEN_WAIT = p.POLL_JITTER = 0.0
    nxt = FakeImg("tracemoe")
    h = Health()
    plan = make_image_plan([p, nxt], h, depth="deep", intent="similar_image", image=Prep())
    plan.steps = [p, nxt]
    led = Ledger(None)
    jm = JobManager(led)
    job = Job("J1", "s", "以图搜", "deep", "similar_image", "any", "planner")
    job.image_hash = "h"
    started = asyncio.create_task(jm.start(job, plan, ImageExecutor(h), 5.0))
    await asyncio.wait_for(in_delete.wait(), 3)
    assert "已取消" in jm.cancel("s", "J1")
    await started
    assert job.status == "cancelled"
    assert nxt.calls == 0                   # 取消后没有继续下一个引擎
    assert any(c[0] == "DELETE" for c in cam.calls)      # 清理照样做了


async def test_r62_13_normal_cleanup_errors_still_swallowed():
    cam = Camo()

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            await req.aread()
            if req.method == "DELETE":
                raise httpx.ConnectError("down")
            return cam(req)
    p = CamofoxLensProvider("http://camo:9377", transport=T())
    p.POLL = p.OPEN_WAIT = p.POLL_JITTER = 0.0
    hits = await p.search_image(b"\xff\xd8x")
    assert hits                              # 清理失败不影响结果
