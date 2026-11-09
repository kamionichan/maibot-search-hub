"""D81：按关键词找图（MockTransport + 合成图，不联网）。"""

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image, ImageDraw

from hub import image_find
from hub.net import Http, Outcome, SourceError

from test_plugin import _grant, plug  # noqa: F401  (fixture)


def png(color=(200, 60, 60), w=400, h=300, seed=0):
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((20 + seed * 7, 20, w // 2, h // 2), fill=color)
    d.ellipse((w // 2, h // 3 - seed * 5, w - 20, h - 20), fill=(30, 120, 200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


SEARX = {"results": [
    {"title": "Steam Deck OLED", "url": "https://www.example.com/deck", "img_src": "https://img.example.com/a.png",
     "thumbnail_src": "https://tn.example.com/a.jpg", "engine": "bing images"},
    {"title": "deck porn xxx", "url": "https://bad.example.com/x", "img_src": "https://img.example.com/adult.png"},
    {"title": "dup", "url": "https://www.example.com/deck2", "img_src": "https://img.example.com/a.png"},
    {"title": "tiny icon", "url": "https://www.example.com/i", "img_src": "https://img.example.com/tiny.png"},
    {"title": "broken", "url": "https://www.example.com/b", "img_src": "https://img.example.com/404.png",
     "thumbnail_src": "https://tn.example.com/b.jpg"},
    {"title": "no img", "url": "https://www.example.com/n"},
]}
TAVILY = {"query": "q", "results": [], "images": [
    {"url": "https://img2.example.com/t1.png", "title": "", "description": "A black Steam Deck handheld"},
    "https://img2.example.com/t2.png"]}


class Reader:
    """仿 BuiltinReader.fetch_bytes：按网址返回字节。"""

    def __init__(self):
        self.urls = []
        self.files = {"https://img.example.com/a.png": png(),
                      "https://img.example.com/adult.png": png((0, 200, 0), seed=3),
                      "https://img.example.com/tiny.png": png(w=60, h=40),
                      "https://tn.example.com/b.jpg": png((240, 200, 0), seed=6),
                      "https://img2.example.com/t1.png": png((120, 0, 200), seed=9),
                      "https://img2.example.com/t2.png": png((0, 0, 0), seed=12)}

    async def fetch_bytes(self, url, *, max_bytes, accept):
        self.urls.append(url)
        if url not in self.files:
            raise SourceError(Outcome.REJECTED, "网页返回 HTTP 404", status=404)
        return self.files[url]

    async def aclose(self):
        pass


def http(log=None, searx=SEARX, tavily=TAVILY):
    def h(req):
        if log is not None:
            log.append(req)
        if req.url.path == "/search" and req.url.host == "searx.local":
            return httpx.Response(200, json=searx)
        if req.url.host == "api.tavily.com":
            return httpx.Response(200, json=tavily)
        return httpx.Response(404)
    return Http(transport=httpx.MockTransport(h))


async def test_find_merges_dedupes_and_validates():
    log = []
    hp = http(log)
    srcs = [image_find.SearxngImages(hp, "http://searx.local/"), image_find.TavilyImages(hp)]
    res = await image_find.find_images("Steam Deck", srcs, Reader(), want=3, safe=False)
    q = log[0].url.params
    assert q["categories"] == "images" and q["safesearch"] == "0" and q["format"] == "json"
    tav = json.loads(next(r for r in log if r.url.host == "api.tavily.com").content)
    assert tav["include_images"] is True and "X-Tavily-Access-Mode" in next(r for r in log if r.url.host == "api.tavily.com").headers
    titles = [p.found.title for p in res.picked]
    # 交错合并：searxng 第一张、tavily 第一张……；重复网址只算一次；太小的图丢掉
    assert titles[0] == "Steam Deck OLED" and "A black Steam Deck handheld" in titles[1]
    assert all(p.width >= 120 and p.jpeg[:2] == b"\xff\xd8" for p in res.picked)
    assert res.candidates == 6 and len(res.picked) == 3      # 默认不过滤成人内容


async def test_safe_mode_drops_adult_and_uses_safesearch():
    log = []
    hp = http(log)
    res = await image_find.find_images("Steam Deck", [image_find.SearxngImages(hp, "http://searx.local")], Reader(),
                                       want=4, safe=True)
    assert log[0].url.params["safesearch"] == "2"
    assert all("porn" not in p.found.title for p in res.picked)


async def test_falls_back_to_thumbnail_and_counts_failures():
    rd = Reader()
    searx = {"results": [{"title": "broken", "url": "https://www.example.com/b", "img_src": "https://img.example.com/404.png",
                          "thumbnail_src": "https://tn.example.com/b.jpg"},
                         {"title": "gone", "url": "https://www.example.com/g", "img_src": "https://img.example.com/gone.png"}]}
    res = await image_find.find_images("x", [image_find.SearxngImages(http(searx=searx), "http://searx.local")], rd,
                                       want=2, safe=False)
    assert [p.found.title for p in res.picked] == ["broken"] and res.failed_downloads == 1
    assert "https://tn.example.com/b.jpg" in rd.urls


async def test_source_errors_recorded():
    def h(req):
        return httpx.Response(200, json={"answer": "rate limited"})
    res = await image_find.find_images("x", [image_find.TavilyImages(Http(transport=httpx.MockTransport(h)))], Reader(),
                                       want=2, safe=False)
    assert not res.picked and res.attempts[0]["out"] == "rejected"


def test_encode_rejects_non_images_and_bombs():
    assert image_find.encode(b"not an image") is None
    assert image_find.encode(png(w=60, h=60)) is None
    enc = image_find.encode(png(w=2000, h=1500))
    assert enc is not None
    jpeg, w, h, _ = enc
    assert (w, h) == (2000, 1500) and max(Image.open(io.BytesIO(jpeg)).size) == 768


# ---------------- 接到 search 工具 ----------------
async def test_search_find_image_returns_media(plug):
    plug.config.searxng.base_url = "http://searx.local"
    plug._apply_config()
    plug._http = http()
    plug._builtin = Reader()
    res = await plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"}))
    assert res["success"] and len(res["content_items"]) == 2          # 默认交 2 张
    assert res["content_items"][0]["content_type"] == "image" and "图1：" in res["content"]
    assert "attach_pic" in res["content"] and "不要说自己发不了图" in res["content"]
    tr = plug.ledger._db.execute("SELECT intent, outcome FROM trace").fetchone()
    assert tr == ("find_image", "finished")


async def test_search_find_image_switch_and_no_source(plug):
    plug.config.image.find_enabled = False
    res = await plug.search(**await _grant(plug, args={"query": "x", "intent": "find_image"}))
    assert not res["success"] and "关闭了找图" in res["error"]
    plug.config.image.find_enabled = True
    plug.config.tavily.enabled = False
    plug._apply_config()
    res = await plug.search(**await _grant(plug, args={"query": "x", "intent": "find_image"}))
    assert not res["success"] and "没有可用的图片搜索来源" in res["error"]



# ---------------- 0.6.1 审查修复（R61-10/11/12/13/14/15） ----------------
def _ico_with_png(side):
    import struct
    im = Image.new("L", (side, side))
    b = io.BytesIO()
    im.save(b, "PNG", optimize=True, compress_level=9)
    data = b.getvalue()
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22) + data


def test_r61_10_ico_rejected_before_decode(monkeypatch):
    from PIL import PngImagePlugin
    loads = []
    orig = PngImagePlugin.PngImageFile.load

    def spy(self, *a, **k):
        loads.append(self.size)
        return orig(self, *a, **k)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", spy)
    assert image_find.encode(_ico_with_png(3000)) is None
    assert loads == []                     # 拒绝之前没有解码内嵌 PNG


async def test_r61_12_stops_when_enough_and_respects_deadline():
    class SlowReader(Reader):
        async def fetch_bytes(self, url, *, max_bytes, accept):
            if "t2" in url:
                await asyncio.sleep(5)
            return await super().fetch_bytes(url, max_bytes=max_bytes, accept=accept)
    t0 = asyncio.get_running_loop().time()
    hp = http()
    res = await image_find.find_images("x", [image_find.TavilyImages(hp)], SlowReader(), want=1, safe=False, budget_s=3)
    assert len(res.picked) == 1 and asyncio.get_running_loop().time() - t0 < 1.5     # 拿够就停，不等慢的
    t0 = asyncio.get_running_loop().time()
    res = await image_find.find_images("x", [image_find.TavilyImages(hp)], SlowReader(), want=2, safe=False, budget_s=1.6)
    assert asyncio.get_running_loop().time() - t0 < 2.2 and len(res.picked) == 1     # 到点收手，不等 5 秒的那张


async def test_r61_13_safe_checks_thumbnail_url():
    searx = {"results": [{"title": "ok", "url": "https://www.example.com/a", "img_src": "https://img.example.com/404.png",
                          "thumbnail_src": "https://tn.example.com/porn/explicit.jpg"}]}
    res = await image_find.find_images("x", [image_find.SearxngImages(http(searx=searx), "http://searx.local")], Reader(),
                                       want=1, safe=True)
    assert res.candidates == 0 and not res.picked


async def test_r61_14_searxng_engines_passed():
    log = []
    await image_find.find_images("x", [image_find.SearxngImages(http(log), "http://searx.local", "bing images")], Reader(),
                                 want=1, safe=False)
    assert log[0].url.params["engines"] == "bing images"


async def test_r61_11_15_find_is_tracked_job_and_trace_shows(plug):
    plug.config.searxng.base_url = "http://searx.local"
    plug._apply_config()
    plug._http = http()
    gate = asyncio.Event()

    class Gate(Reader):
        async def fetch_bytes(self, url, *, max_bytes, accept):
            await gate.wait()
            return await super().fetch_bytes(url, max_bytes=max_bytes, accept=accept)
    plug._builtin = Gate()
    t = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.2)
    act = plug.jobs.active("sess-A")
    assert len(act) == 1 and act[0].intent == "find_image"
    assert "已取消" in plug.jobs.cancel("sess-A", act[0].job_id)
    res = await t
    assert not res["success"] and "已取消" in res["error"]
    gate.set()
    res = await plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"}))
    assert res["success"]
    jid = res["content"].split()[1]
    _, text, _ = await plug.cmd_trace(stream_id="sess-A", matched_groups={"job": jid})
    assert "searxng ok" in text and "s" in text          # R61-15：不再 KeyError



# ---------------- 0.6.1 复审修复 R62-12/15/16 ----------------
def _find_plug(plug, reader):
    plug.config.searxng.base_url = "http://searx.local"
    plug._apply_config()
    plug._http = http()
    plug._builtin = reader


async def test_r62_12_same_query_shares_one_job(plug):
    gate = asyncio.Event()
    n = {"fetch": 0}

    class Gate(Reader):
        async def fetch_bytes(self, url, *, max_bytes, accept):
            n["fetch"] += 1
            await gate.wait()
            return await super().fetch_bytes(url, max_bytes=max_bytes, accept=accept)
    _find_plug(plug, Gate())
    a = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    first = n["fetch"]
    b = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    assert len(plug.jobs.active("sess-A")) == 1 and n["fetch"] == first      # 第二次没有另开
    gate.set()
    ra, rb = await a, await b
    assert ra["success"] and rb["success"] and len(rb["content_items"]) == len(ra["content_items"])


async def test_r62_12_find_obeys_session_running_limit(plug):
    plug.config.search.per_session_running = 1
    plug.config.search.per_session_queue = 1
    plug._apply_config()
    plug.jobs.per_running, plug.jobs.per_queue = 1, 1
    plug.jobs._sem.clear()
    gate = asyncio.Event()
    running = {"now": 0, "peak": 0}

    class Gate(Reader):
        async def fetch_bytes(self, url, *, max_bytes, accept):
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
            try:
                await gate.wait()
                return await super().fetch_bytes(url, max_bytes=max_bytes, accept=accept)
            finally:
                running["now"] -= 1
    _find_plug(plug, Gate())
    a = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    b = asyncio.create_task(plug.search(**await _grant(plug, args={"query": "Switch 2", "intent": "find_image"})))
    await asyncio.sleep(0.1)
    st = sorted(j.status for j in plug.jobs.active("sess-A"))
    assert st == ["queued", "running"]
    gate.set()
    await a, await b


async def test_r62_15_bad_source_does_not_leave_sibling_running():
    class Bad:
        name = "bad"

        async def search(self, q, *, limit, safe):
            raise RecursionError("deep json")
    stopped = []

    class Slow:
        name = "slow"

        async def search(self, q, *, limit, safe):
            try:
                await asyncio.sleep(5)
            finally:
                stopped.append(1)
            return []
    res = await image_find.find_images("x", [Bad(), Slow()], Reader(), want=1, safe=False, budget_s=1.0)
    assert res.attempts[0]["src"] == "bad" and "RecursionError" in res.attempts[0]["note"]
    assert stopped == [1]                    # 另一个来源的任务已结束，没有留在后台


async def test_r62_15_deep_json_is_invalid_json():
    from hub.net import Http
    deep = "[" * 200000 + "]" * 200000

    def h(req):
        return httpx.Response(200, content=('{"results":' + deep + "}").encode(), headers={"content-type": "application/json"})
    with pytest.raises(SourceError, match="无效 JSON"):
        await Http(transport=httpx.MockTransport(h)).get_json("https://x.test/", {})


async def test_r62_16_find_job_written_to_jobs_table(plug):
    _find_plug(plug, Reader())
    res = await plug.search(**await _grant(plug, args={"query": "Steam Deck", "intent": "find_image"}))
    assert res["success"]
    rows = plug.ledger.jobs("sess-A")
    assert rows and rows[0]["status"] == "finished"
