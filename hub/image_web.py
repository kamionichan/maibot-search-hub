"""抓网页的以图搜引擎，移植进插件本体（D54）。

参考 PicImageSearch（MIT）的 Yandex / Bing 实现，以及 picimagesearch-api（MIT，同一作者）的防护做法；
只用 httpx + 标准库，不引入 PicImageSearch、lxml、pyquery、httpx2。

防护（沿用 picimagesearch-api 的 _GuardedTransport）：
- 每次搜索一个独立的 httpx 客户端（cookie 不串）；手动跟随跳转，只允许跳到该引擎自己的域名；
- 单次搜索最多 8 个请求、响应最多 8MB、不接受压缩（Accept-Encoding: identity，避免解压炸弹）；
- 遇到验证码 / 访问限制直接报 access_limited 并冷却，不尝试绕过、不自动重试；
- httpx 的请求日志在这里被遮掉 URL（URL 里有引擎给这张图分配的 ID）。

2026-10-29 实测：
- Yandex：`POST /images/search?rpt=imageview&cbir_page=sites`（multipart upfile）→ 跳到带 cbir_id 的结果页，
  页面里 `id="ImagesApp-…"` 的 data-state 是 JSON：initialState.cbirSites.sites[]（title/url/domain/description/
  thumb.url/originalImage{url,width,height}），initialState.cbirTags.tags[].text 是识别关键词。
- Bing：上传后 302 到 `insightsToken=bcid_…`，再 302 到 `/search?q=<猜测词>&bcid=…`。上游 PicImageSearch 用的
  `/images/api/custom/knowledge` 现在返回空 tags（两张不同的图都一样），所以本体只取“猜测词”：
  番剧截图猜成 “Gochuumon Wa Usagi Desu Ka? Rize”（对），风景照只给通用的 “Bing Visual Search”（丢弃）。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .image_sources import ImageHit
from .net import Outcome, SourceError, classify_exception
from .providers import ProviderInfo, _clip, _valid_url

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/139.0.0.0 Safari/537.36")
MAX_BODY = 8 * 1024 * 1024
MAX_REQUESTS = 8
CAPTCHA_MARKERS = ('action="/checkcaptcha', "action='/checkcaptcha", 'class="g-recaptcha', "class='g-recaptcha",
                   "our systems have detected unusual traffic", "cf-chl-", "verify you are human", 'id="captcha',
                   "smartcaptcha")
BING_GENERIC = {"", "bing visual search", "visual search", "bing"}


# ---------------- 日志遮挡（移植自 picimagesearch-api） ----------------
_PRIVATE = ContextVar("search_hub_private_http", default=False)
_LOG_LOCK = threading.Lock()
_LOG_USERS = 0
_LOG_NAMES = ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _PRIVATE.get():
            record.msg, record.args, record.exc_info, record.exc_text = "以图搜 HTTP 事件（已省略 URL）", (), None, None
        return True


_FILTER = _MaskFilter()


@contextmanager
def _private_logs():
    global _LOG_USERS
    with _LOG_LOCK:
        if _LOG_USERS == 0:
            for n in _LOG_NAMES:
                logging.getLogger(n).addFilter(_FILTER)
        _LOG_USERS += 1
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)
        with _LOG_LOCK:
            _LOG_USERS -= 1
            if _LOG_USERS == 0:
                for n in _LOG_NAMES:
                    logging.getLogger(n).removeFilter(_FILTER)


# ---------------- 受限会话 ----------------
class _Session:
    """一次搜索的受限 HTTP 会话。"""

    def __init__(self, engine: str, hosts: frozenset[str], *, proxy: str = "", timeout: float = 40.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.engine, self.hosts, self.requests = engine, hosts, 0
        kw: dict[str, Any] = {"timeout": httpx.Timeout(connect=8.0, read=timeout, write=20.0, pool=5.0),
                              "follow_redirects": False, "trust_env": False,
                              "headers": {"User-Agent": BROWSER_UA, "Accept-Encoding": "identity",
                                          "Accept-Language": "en-US,en;q=0.9"}}
        if transport is not None:
            kw["transport"] = transport
        elif proxy:
            kw["proxy"] = proxy
        self.client = httpx.AsyncClient(**kw)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _check_url(self, url: httpx.URL) -> None:
        if url.scheme != "https" or url.host not in self.hosts or url.port not in (None, 443) or url.userinfo:
            raise SourceError(Outcome.UNKNOWN, f"{self.engine} 跳到了非预期地址，已停止")
        path = url.path.lower()
        if any(p in path for p in ("captcha", "/sorry", "challenge")):
            raise _limited(self.engine)

    async def send(self, method: str, url: str, **kw: Any) -> tuple[int, httpx.URL, str, httpx.Headers]:
        """发一个请求（不跟随跳转），返回 (状态码, URL, 文本, 响应头)。"""
        req = self.client.build_request(method, url, **kw)
        self._check_url(req.url)
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise SourceError(Outcome.UNKNOWN, f"{self.engine} 请求步骤超过上限")
        try:
            resp = await self.client.send(req, stream=True)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        try:
            if resp.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise SourceError(Outcome.UNKNOWN, f"{self.engine} 返回了未预期的压缩响应")
            if resp.status_code in (401, 403, 429):
                raise _limited(self.engine, resp.status_code)
            if resp.status_code >= 400:
                raise SourceError(Outcome.UNKNOWN if resp.status_code >= 500 else Outcome.REJECTED,
                                  f"{self.engine} 返回 HTTP {resp.status_code}", status=resp.status_code)
            body = bytearray()
            async for chunk in resp.aiter_bytes(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise SourceError(Outcome.UNKNOWN, f"{self.engine} 返回内容过大")
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        finally:
            await resp.aclose()
        text = body.decode("utf-8", "replace")
        low = text.lower()
        if any(m in low for m in CAPTCHA_MARKERS):
            raise _limited(self.engine)
        return resp.status_code, req.url, text, resp.headers

    async def follow(self, method: str, url: str, *, max_hops: int = 4, **kw: Any) -> tuple[httpx.URL, str]:
        """发请求并在本引擎域名内跟随跳转，返回最终 URL 和正文。"""
        status, cur, text, headers = await self.send(method, url, **kw)
        for _ in range(max_hops):
            if status not in (301, 302, 303, 307, 308):
                return cur, text
            loc = headers.get("location", "")
            if not loc:
                break
            status, cur, text, headers = await self.send("GET", urljoin(str(cur), loc))
        if status in (301, 302, 303, 307, 308):
            raise SourceError(Outcome.UNKNOWN, f"{self.engine} 跳转次数过多")
        return cur, text


def _limited(engine: str, status: int | None = None) -> SourceError:
    return SourceError(Outcome.REJECTED, f"{engine} 要求验证或限制了访问（未尝试绕过）", status=status,
                       retry_after=1800)


def _clean_text(s: Any, n: int) -> str:
    if not isinstance(s, str):
        return ""
    s = re.sub(r"<[^>]*>", "", html.unescape(s))
    s = "".join(c for c in s if c.isprintable() or c == " ")
    return _clip(s.strip(), n)


def _strip_tracking(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def _https(u: Any) -> str:
    if not isinstance(u, str) or not u:
        return ""
    return "https:" + u if u.startswith("//") else u


class _WebBase:
    hosts: frozenset[str] = frozenset()
    engine = ""
    label = ""

    def __init__(self, *, proxy: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 40.0) -> None:
        self._proxy, self._transport, self._timeout = proxy, transport, timeout
        self.info = ProviderInfo(f"img_{self.engine}", f"img_{self.engine}", "slow", paid=False,
                                 zero_marginal_cost=True, has_dates=False, label=self.label,
                                 extra={"image": True, "web": True, "kinds": ["visual", "guess"]})

    async def search_image(self, jpeg: bytes, *, max_results: int = 5) -> list[ImageHit]:
        s = _Session(self.label, self.hosts, proxy=self._proxy, transport=self._transport, timeout=self._timeout)
        try:
            with _private_logs():
                return await asyncio.wait_for(self._search(s, jpeg, max_results), timeout=self._timeout + 20)
        except asyncio.TimeoutError:
            raise SourceError(Outcome.UNKNOWN, f"{self.label} 超时") from None
        finally:
            await s.aclose()

    async def _search(self, s: _Session, jpeg: bytes, max_results: int) -> list[ImageHit]:  # pragma: no cover
        raise NotImplementedError


# ---------------- Yandex ----------------
_YANDEX_STATE = re.compile(r'<div\b[^>]*\bid="ImagesApp-[^"]*"[^>]*>', re.S)
_DATA_STATE = re.compile(r'\bdata-state="([^"]*)"', re.S)


def parse_yandex(page: str) -> tuple[list[dict], list[str]]:
    """从结果页取 (sites, tags)。结构变了就抛 SourceError（不伪造结果）。"""
    m = None
    for tag in _YANDEX_STATE.finditer(page):
        m = _DATA_STATE.search(tag.group(0))
        if m:
            break
    if not m:
        raise SourceError(Outcome.UNKNOWN, "Yandex 页面结构已变化（找不到 data-state）")
    try:
        state = json.loads(html.unescape(m.group(1)))
    except ValueError:
        raise SourceError(Outcome.UNKNOWN, "Yandex 页面结构已变化（data-state 不是 JSON）") from None
    init = state.get("initialState") if isinstance(state, dict) else None
    if not isinstance(init, dict) or "cbirSites" not in init:
        raise SourceError(Outcome.UNKNOWN, "Yandex 页面结构已变化（缺 cbirSites）")
    sites = (init.get("cbirSites") or {}).get("sites") or []
    tags = [t.get("text") for t in ((init.get("cbirTags") or {}).get("tags") or []) if isinstance(t, dict)]
    return [x for x in sites if isinstance(x, dict)], [t for t in tags if isinstance(t, str) and t.strip()]


class YandexImageProvider(_WebBase):
    engine, label = "yandex", "Yandex"
    hosts = frozenset({"yandex.com", "yandex.ru"})

    async def _search(self, s: _Session, jpeg: bytes, max_results: int) -> list[ImageHit]:
        url, page = await s.follow("POST", "https://yandex.com/images/search",
                                   params={"rpt": "imageview", "cbir_page": "sites"}, data={"prg": "1"},
                                   files={"upfile": ("blob", jpeg, "image/jpeg")})
        sites, tags = parse_yandex(page)
        out: list[ImageHit] = []
        seen: set[str] = set()
        for x in sites:
            u = _strip_tracking(x.get("url") or "")
            if not _valid_url(u) or u in seen:
                continue
            seen.add(u)
            thumb = _https((x.get("thumb") or {}).get("url"))
            orig = x.get("originalImage") or {}
            size = f"{orig.get('width')}×{orig.get('height')}" if orig.get("width") and orig.get("height") else ""
            snippet = "；".join(p for p in (x.get("domain") or "", "页面里有相似图片" + (f"（{size}）" if size else ""),
                                            _clean_text(x.get("description"), 120)) if p)
            out.append(ImageHit(_clean_text(x.get("title"), 80) or (x.get("domain") or u), u, snippet, "visual", None,
                                "weak", self.info.name, extra={"thumbnail": thumb if _valid_url(thumb) else "",
                                                               "original": orig.get("url") or ""}))
            if len(out) >= max_results:
                break
        if tags:
            words = "、".join(_clip(t, 30) for t in tags[:5])
            out.append(ImageHit(f"Yandex 识别关键词：{words}", "https://yandex.com/images/search?" + urlencode(
                {"text": tags[0]}), "Yandex 对整张图给出的关键词，只是线索，需要再用文字搜索核实", "guess", None,
                "weak", self.info.name, extra={"tags": tags[:5]}))
        return out


# ---------------- Bing（只取猜测词） ----------------
def parse_bing_guess(location: str) -> str:
    try:
        q = parse_qs(urlsplit(location).query).get("q", [""])[0]
    except ValueError:
        return ""
    q = _clean_text(q, 80)
    return "" if q.lower() in BING_GENERIC else q


class BingImageProvider(_WebBase):
    engine, label = "bing", "Bing"
    hosts = frozenset({"www.bing.com"})

    async def _search(self, s: _Session, jpeg: bytes, max_results: int) -> list[ImageHit]:
        import base64

        status, _, _, headers = await s.send(
            "POST", "https://www.bing.com/images/search?view=detailv2&iss=sbiupload&FORM=SBIHMP",
            files={"cbir": (None, "sbi"), "imageBin": (None, base64.b64encode(jpeg).decode("ascii"))})
        loc = headers.get("location", "") if status in (301, 302, 303) else ""
        if "insightsToken=" not in loc:
            raise SourceError(Outcome.UNKNOWN, "Bing 上传流程已变化（没有拿到 insightsToken）")
        status, _, _, headers = await s.send("GET", urljoin("https://www.bing.com/", loc))
        nxt = headers.get("location", "") if status in (301, 302, 303) else ""
        guess = parse_bing_guess(nxt) if "/search" in nxt else ""
        if not guess:
            return []
        return [ImageHit(f"Bing 猜测：{guess}", "https://www.bing.com/search?q=" + quote(guess),
                         "Bing 对整张图的猜测词，只是线索，需要再用文字搜索核实", "guess", None, "weak",
                         self.info.name, extra={"guess": guess})]


WEB_ENGINES = {"yandex": YandexImageProvider, "bing": BingImageProvider}
