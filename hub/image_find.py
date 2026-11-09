"""按关键词找图（D81）：“给我发一张 XX 的图”。

流程：图片搜索（SearXNG images、Tavily include_images）→ 下载几张候选（内置读取器，公网校验、大小上限）
→ 校验是正常图片、去重、重新编码成 JPEG → 作为工具媒体（content_items）交给模型，由模型看过后决定发不发。

- 只发出搜索词，不上传任何图片。
- 默认不过滤真人和成人内容（用户决定）；打开“找图时过滤成人内容”后：SearXNG 用 safesearch=2，
  并按标题/网址里的明显词丢掉结果。真人照片没有可靠的自动识别办法，不做过滤。
- 同步返回（后台送回带不了图）；总时限内拿不到图就如实说没找到。
"""

from __future__ import annotations

import asyncio
import io
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .net import Http, Outcome, SourceError

ADULT_WORDS = re.compile(r"porn|xxx|hentai|nsfw|r-?18|sex|nude|naked|色情|成人|裸|18禁|里番|本子", re.I)
MIN_SIDE = 120            # 太小的图（图标、占位图）不要
OUT_SIDE = 768            # 交给模型的图最长边
MAX_BYTES = 6 * 1024 * 1024
FIND_BUDGET = 18.0        # 整条找图路径的时限（秒）
MAX_PIXELS = 16_000_000
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")


@dataclass
class Found:
    img_url: str
    page_url: str
    title: str
    source: str               # 哪个搜索来源：searxng / tavily
    thumb_url: str = ""
    desc: str = ""
    engine: str = ""          # SearXNG 下面的具体引擎（bing images 等）


@dataclass
class Picked:
    found: Found
    jpeg: bytes
    width: int
    height: int


@dataclass
class FindResult:
    picked: list[Picked] = field(default_factory=list)
    candidates: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)   # 每个来源：src/out/n/note
    failed_downloads: int = 0


def _http_url(u: Any) -> bool:
    return isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) < 2000


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


class SearxngImages:
    name = "searxng"
    label = "SearXNG 图片"

    def __init__(self, http: Http, base_url: str, engines: str = "") -> None:
        self._http = http
        self._base = base_url.strip().rstrip("/")
        self._engines = engines.strip()

    async def search(self, query: str, *, limit: int, safe: bool) -> list[Found]:
        params: dict[str, Any] = {"q": query[:200], "format": "json", "categories": "images",
                                  "safesearch": 2 if safe else 0}
        if self._engines:
            # R61-14：管理员填了“只用这些引擎”，找图也照样限制（不支持图片的引擎自然搜不到，不扩大范围）
            params["engines"] = self._engines
        data = await self._http.get_json(self._base + "/search", params, read_timeout=12.0)
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise SourceError(Outcome.UNKNOWN, "SearXNG 返回格式不符")
        out: list[Found] = []
        for r in rows:
            if not isinstance(r, dict) or not _http_url(r.get("img_src")):
                continue
            img = str(r["img_src"])
            page = str(r.get("url")) if _http_url(r.get("url")) else img
            thumb = str(r.get("thumbnail_src")) if _http_url(r.get("thumbnail_src")) else ""
            out.append(Found(img_url=img, page_url=page, title=str(r.get("title") or "")[:80], source=self.name,
                             thumb_url=thumb, engine=str(r.get("engine") or "")))
            if len(out) >= limit:
                break
        return out


class TavilyImages:
    name = "tavily"
    label = "Tavily 图片"
    URL = "https://api.tavily.com/search"

    def __init__(self, http: Http, api_key: str = "") -> None:
        self._http = http
        self._key = api_key.strip()
        self.paid = bool(self._key)

    async def search(self, query: str, *, limit: int, safe: bool) -> list[Found]:
        body = {"query": query[:200], "max_results": 3, "include_images": True, "include_image_descriptions": True,
                "search_depth": "basic"}
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {"X-Tavily-Access-Mode": "keyless"}
        data = await self._http.post_json(self.URL, body, headers=headers, read_timeout=15.0)
        imgs = data.get("images") if isinstance(data, dict) else None
        if not isinstance(imgs, list):
            raise SourceError(Outcome.REJECTED, "Tavily 未返回图片（可能触发 keyless 限流）")
        out: list[Found] = []
        for im in imgs:
            url = im.get("url") if isinstance(im, dict) else im
            if not _http_url(url):
                continue
            desc = str(im.get("description") or "") if isinstance(im, dict) else ""
            title = str(im.get("title") or "") if isinstance(im, dict) else ""
            out.append(Found(img_url=str(url), page_url=str(url), title=(title or desc)[:80], source=self.name,
                             desc=desc[:200]))
            if len(out) >= limit:
                break
        return out


async def paced_fetch(reader: Any, pacer: Any, url: str, deadline: float, max_bytes: int) -> bytes:
    """按网站限速下载一张图。读取器支持每跳钩子（BuiltinReader）时按每一跳的实际网站取名额（R63-06）；
    不支持的（测试替身等）退回只按初始网址取名额。"""
    import inspect
    if "hop" in inspect.signature(reader.fetch_bytes).parameters:
        return await reader.fetch_bytes(url, max_bytes=max_bytes, accept=("image/",), hop=pacer.hop(deadline))
    async with pacer.slot(url, deadline) as ok:
        if not ok:
            raise SourceError(Outcome.NOT_SENT, "等不到下载名额（按网站限速）")
        return await reader.fetch_bytes(url, max_bytes=max_bytes, accept=("image/",))


def adult_hint(f: Found) -> bool:
    return bool(ADULT_WORDS.search(f"{f.title} {f.desc} {f.page_url} {f.img_url} {f.thumb_url}"))   # R61-13：含缩略图网址


def encode(data: bytes) -> tuple[bytes, int, int, int] | None:
    """校验并重新编码：返回 (jpeg, 宽, 高, dHash)。不是图片、太小、像素过多（解压炸弹）时返回 None。
    在线程里调用（CPU 活）。"""
    from PIL import Image

    import warnings

    from .image import dhash_bytes
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # R61-10：格式在 open 时就限定。有的格式（ICO 内嵌 PNG）在 open 阶段就会解码，事后判断格式已经来不及
            with Image.open(io.BytesIO(data), formats=FORMATS) as probe:
                w, h = probe.size
                if w * h > MAX_PIXELS or min(w, h) < MIN_SIDE:
                    return None
                probe.seek(0)
                im = probe.convert("RGB")
        im.thumbnail((OUT_SIDE, OUT_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        jpeg = buf.getvalue()
    except Exception:  # noqa: BLE001 - 坏图直接丢
        return None
    dh = dhash_bytes(jpeg)
    return jpeg, w, h, (dh if dh is not None else 0)


async def find_images(query: str, sources: list[Any], reader: Any, *, want: int, safe: bool, budget_s: float = FIND_BUDGET,
                      cpu: Any = None, allow: Any = None, sites: Any = None) -> FindResult:
    """allow(source) -> (ok, why)：付费来源（填了 key 的 Tavily）先过预算；cpu(fn, *args)：图片解码线程池。

    R61-12：budget_s 是整条路径（搜索、下载、排队解码）的绝对截止时间；拿够 want 张就停，其余候选取消。
    已经在线程里跑的解码停不下来，靠 encode 的格式与像素限制保证单张开销有上限。"""
    t0 = time.monotonic()
    deadline = t0 + budget_s
    res = FindResult()

    def left() -> float:
        return deadline - time.monotonic()

    async def one(src: Any) -> list[Found]:
        s0 = time.monotonic()

        def note(out: str, n: int, why: str) -> None:
            res.attempts.append({"src": src.name, "out": out, "n": n, "note": why,
                                 "s": round(time.monotonic() - s0, 2)})      # R61-15：/search trace 要耗时
        if allow is not None:
            ok, why = allow(src)
            if not ok:
                note("skipped", 0, why)
                return []
        try:
            got = await asyncio.wait_for(src.search(query, limit=12, safe=safe), timeout=max(3.0, budget_s / 2))
        except asyncio.TimeoutError:
            note("timeout", 0, "超时")
            return []
        except SourceError as err:
            note(err.outcome.value, 0, err.message[:60])
            return []
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - R62-15：意外异常只让这个来源算失败
            note("unknown", 0, f"返回格式不符（{type(exc).__name__}）")
            return []
        note("ok", len(got), "")
        return got

    # R62-15：来源任务显式持有。一个来源抛了意外异常（例如深层 JSON 的 RecursionError），另一个来源也要取消并等它结束，
    # 不能留在后台；意外异常只让这个来源算失败，不让整次找图崩掉
    src_tasks = [asyncio.create_task(one(s)) for s in sources]
    try:
        got_all = await asyncio.gather(*src_tasks, return_exceptions=True)
    finally:
        for t in src_tasks:
            t.cancel()
        await asyncio.gather(*src_tasks, return_exceptions=True)
    batches: list[list[Found]] = []
    for s, g in zip(sources, got_all):
        if isinstance(g, BaseException):
            if isinstance(g, asyncio.CancelledError):
                raise g
            res.attempts.append({"src": s.name, "out": "unknown", "n": 0, "note": f"返回格式不符（{type(g).__name__}）",
                                 "s": 0.0})
            batches.append([])
        else:
            batches.append(g)
    # 交错合并（每个来源轮流取一张），按图片网址去重
    seen: set[str] = set()
    cands: list[Found] = []
    for i in range(max((len(b) for b in batches), default=0)):
        for b in batches:
            if i < len(b) and b[i].img_url not in seen:
                seen.add(b[i].img_url)
                if safe and adult_hint(b[i]):
                    continue
                cands.append(b[i])
    res.candidates = len(cands)
    if not cands or reader is None:
        return res

    sem = asyncio.Semaphore(4)
    from .pace import DOWNLOADS
    site_pacer = sites if sites is not None else DOWNLOADS
    run = cpu or (lambda fn, *a: asyncio.get_running_loop().run_in_executor(None, fn, *a))

    async def fetch(f: Found) -> Picked | None:
        async with sem:
            for url in (f.img_url, f.thumb_url):
                if not url:
                    continue
                if left() < 1.0:
                    return None
                try:
                    # 用户同意：候选图按网站限速（同一网站同一时间一张、间隔 1–2 秒）；R63-06：按每一跳的实际网站
                    data = await asyncio.wait_for(paced_fetch(reader, site_pacer, url, deadline - 1.0, MAX_BYTES),
                                                  timeout=min(8.0, left()))
                except Exception:  # noqa: BLE001 - 下载失败或等不到名额：换缩略图或下一张
                    continue
                if left() < 0.5:
                    return None
                try:
                    enc = await asyncio.wait_for(run(encode, data), timeout=max(0.5, left()))
                except asyncio.TimeoutError:
                    return None
                if enc is not None:
                    jpeg, w, h, dh = enc
                    p = Picked(f, jpeg, w, h)
                    p.dhash = dh          # type: ignore[attr-defined]
                    return p
            res.failed_downloads += 1
            return None

    from .image import hamming
    # 多下几张备用（有的站拒绝外链、有的是占位图），按原顺序取前 want 张，近似重复的只留一张
    pool = cands[: want * 3 + 2]
    tasks = [asyncio.create_task(fetch(f)) for f in pool]
    done_in_order: dict[int, Picked | None] = {}
    try:
        pending = set(tasks)
        nxt = 0
        while pending and len(res.picked) < want and left() > 0:
            done, pending = await asyncio.wait(pending, timeout=left(), return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                done_in_order[tasks.index(t)] = t.result() if not t.cancelled() and t.exception() is None else None
            # 按原顺序收：前面的还没好就先等，保持“排名靠前的优先”
            while nxt < len(tasks) and nxt in done_in_order:
                p = done_in_order[nxt]
                nxt += 1
                if p is None:
                    continue
                if any(hamming(getattr(p, "dhash", 0), getattr(q, "dhash", 1 << 70)) <= 6 for q in res.picked):
                    continue
                res.picked.append(p)
                if len(res.picked) >= want:
                    break
            if not done:
                break
        if len(res.picked) < want:
            # 到点了或者前面的都失败：后面已经下好的也收进来
            for k in sorted(done_in_order):
                p = done_in_order[k]
                if k < nxt or p is None or len(res.picked) >= want:
                    continue
                if not any(hamming(getattr(p, "dhash", 0), getattr(q, "dhash", 1 << 70)) <= 6 for q in res.picked):
                    res.picked.append(p)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return res
