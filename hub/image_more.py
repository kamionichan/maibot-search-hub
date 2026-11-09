"""以图搜第二批引擎（0.6.1，D76；实测记录见 OPEN_QUESTIONS Q53）。

- 百度识图：插件本体，纯 httpx。`POST https://graph.baidu.com/upload`（multipart image，from=pc）→ JSON data.url →
  结果页里 `window.cardData = [...]` 是 JSON。`same` 卡片 = “图片来源”（url/title/image_src/releaseTime），
  沿用 image_web 的受限会话（只允许百度自己的域名、请求数/响应大小上限、遇验证即冷却不绕过）。
- Google Lens（SerpApi）：官方接口。先 `POST https://serpapi.com/image`（≤500KB）拿 image_id（10 分钟有效），
  再 `GET https://serpapi.com/search?engine=google_lens&image_id=…`；只有成功的搜索计次（每月免费 250 次）。
  上传一次 + 搜索一次，额度按 1 次搜索计（Image API 本身不计入搜索次数，以官网为准）。
- Google Lens（camofox 浏览器）：外部可选服务（jo-inc/camofox-browser，自部署）。插件只调它的 REST 接口，
  固定执行：开标签页 → 打开 lens.google.com → 在页面里用表单把图片提交到 /v3/upload → 读结果链接 → 关会话。
  2026-11-08 实测：约 2 秒/张，连续第 12 张左右被重定向到 /sorry。所以本地限频（默认每小时 6 次），
  遇到验证页冷却 2 小时，不尝试绕过。模型不能直接操作这个浏览器。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .image_sources import ImageHit
from .image_web import _clean_text, _private_logs, _Session, _strip_tracking, _WebBase
from .pace import BROWSER_DEFAULT, Pacer
from .net import Http, Outcome, SourceError, classify_exception, TooLarge, ACCEPT_ENCODING
from .providers import ProviderInfo, _clip, _valid_url


def _host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


# ---------------- 百度识图 ----------------
_CARD_DATA = re.compile(r"window\.cardData\s*=\s*(\[.*?\]);\s*(?:window\.|</script>)", re.S)


def parse_baidu(page: str) -> tuple[list[dict], str]:
    """从结果页取 (“图片来源”列表, 识别出的类别词)。结构变了就抛 SourceError（不伪造结果）。"""
    m = _CARD_DATA.search(page)
    if not m:
        raise SourceError(Outcome.UNKNOWN, "百度识图页面结构已变化（找不到 cardData）")
    try:
        cards = json.loads(m.group(1))
    except (ValueError, RecursionError):
        raise SourceError(Outcome.UNKNOWN, "百度识图页面结构已变化（cardData 不是 JSON）") from None
    if not isinstance(cards, list):
        raise SourceError(Outcome.UNKNOWN, "百度识图页面结构已变化（cardData 不是列表）")
    by = {c.get("cardName"): c for c in cards if isinstance(c, dict)}
    same = ((by.get("same") or {}).get("tplData") or {}).get("list") or []
    guess = ""
    pano = (((by.get("cardHeader") or {}).get("tplData") or {}).get("pano") or {}).get("list") or []
    if pano and isinstance(pano[0], dict):
        guess = _clean_text(pano[0].get("subTitle"), 30)
    return [x for x in same if isinstance(x, dict)], guess


def _ms_date(v: Any) -> str | None:
    try:
        ms = int(v)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


class BaiduImageProvider(_WebBase):
    engine, label = "baidu", "百度识图"
    hosts = frozenset({"graph.baidu.com"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.info.extra["kinds"] = ["source", "visual"]

    async def _search(self, s: _Session, jpeg: bytes, max_results: int) -> list[ImageHit]:
        s.client.headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        status, _, text, _ = await s.send("POST", "https://graph.baidu.com/upload", data={"from": "pc"},
                                          files={"image": ("q.jpg", jpeg, "image/jpeg")},
                                          headers={"Acs-Token": ""})
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise SourceError(Outcome.UNKNOWN, "百度识图上传返回不是 JSON") from None
        url = ((data or {}).get("data") or {}).get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://graph.baidu.com/"):
            # R62-01：原文交给 SourceError 统一“先脱敏、再截断”，这里只清掉控制字符，不预先截断
            msg = _clean_text((data or {}).get("msg"), 2000) if isinstance(data, dict) else ""
            raise SourceError(Outcome.REJECTED, f"百度识图没有接受这张图{('（' + msg + '）') if msg else ''}")
        _, page = await s.follow("GET", url)
        same, guess = parse_baidu(page)
        out: list[ImageHit] = []
        seen: set[str] = set()
        for x in same:
            u = _strip_tracking(str(x.get("url") or ""))
            if not _valid_url(u) or u in seen or x.get("isDeadlink"):
                continue
            seen.add(u)
            title = x.get("title")
            title = title[0] if isinstance(title, list) and title else title
            thumb = str(x.get("image_src") or "")
            snippet = "；".join(p for p in (_host(u), "页面里有这张图", _clean_text(x.get("abstract"), 100)) if p)
            out.append(ImageHit(_clean_text(title, 80) or _host(u), u, snippet, "source", None, "weak", self.info.name,
                                extra={"thumbnail": thumb if _valid_url(thumb) else ""},
                                published_at=_ms_date(x.get("releaseTime"))))
            if len(out) >= max_results:
                break
        if guess and guess not in ("通用", "其他"):
            out.append(ImageHit(f"百度识图：{guess}", "https://graph.baidu.com/", "百度对整张图给出的类别，只是线索",
                                "guess", None, "weak", self.info.name, extra={"guess": guess}))
        return out


# ---------------- 本地限频（Lens 两条通道共用） ----------------
def _lens_hits(items: list[tuple[str, str, str, str, str | None]], source: str, kind: str,
               max_results: int) -> list[ImageHit]:
    """items: (标题, 网址, 网站名, 缩略图, 日期)。"""
    out: list[ImageHit] = []
    seen: set[str] = set()
    for title, url, site, thumb, date in items:
        url = _strip_tracking(url)
        if not _valid_url(url) or url in seen or _host(url).endswith(("google.com", "gstatic.com", "googleusercontent.com")):
            continue
        seen.add(url)
        note = "Google 完全匹配：页面里有同一张图" if kind == "source" else "Google 视觉匹配：页面里有相似的图"
        out.append(ImageHit(_clean_text(title, 80) or _host(url), url, "；".join(p for p in (site or _host(url), note) if p),
                            kind, None, "weak", source, extra={"thumbnail": thumb if _valid_url(thumb) else ""},
                            published_at=date))
        if len(out) >= max_results:
            break
    return out


# ---------------- Google Lens（SerpApi） ----------------
def shrink_jpeg(jpeg: bytes, limit: int) -> bytes:
    """把（已清洗的）JPEG 压到 limit 字节以内：先缩到 1024 再逐步降画质，最多 4 轮。输入只来自 prepare_image。"""
    if len(jpeg) <= limit:
        return jpeg
    import io

    from PIL import Image
    with Image.open(io.BytesIO(jpeg), formats=("JPEG",)) as src:
        im = src.convert("RGB")
    q, side = 85, max(im.size)
    while True:
        if side > 1024:
            im.thumbnail((1024, 1024))
            side = 1024
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        data = buf.getvalue()
        if len(data) <= limit or q <= 40:
            return data
        q -= 15


class SerpApiLensProvider:
    """Google Lens 官方 JSON（经 SerpApi）。填 key 才启用；按成功次数计费，计入每日上限。"""

    IMAGE_URL = "https://serpapi.com/image"
    SEARCH_URL = "https://serpapi.com/search"
    MAX_UPLOAD = 500 * 1024

    def __init__(self, http: Http, api_key: str) -> None:
        self._http = http
        self._key = api_key.strip()
        self.info = ProviderInfo("serpapi_lens", "google_lens", "medium", paid=True, zero_marginal_cost=False,
                                 has_dates=False, label="Google Lens（SerpApi）",
                                 extra={"image": True, "lens": True, "kinds": ["source", "visual"]})

    @staticmethod
    def _shrink(jpeg: bytes, limit: int) -> bytes:
        return shrink_jpeg(jpeg, limit)

    def _check(self, status: int, data: Any, what: str) -> dict:
        err = data.get("error") if isinstance(data, dict) else None
        if status in (401, 403) or (isinstance(err, str) and "invalid api key" in err.lower()):
            raise SourceError(Outcome.REJECTED, "SerpApi key 无效", status=status or 401, auth_failed=True)
        if status == 429 or (isinstance(err, str) and ("run out" in err.lower() or "limit" in err.lower())):
            raise SourceError(Outcome.REJECTED, "SerpApi 额度用尽", status=429, quota_exhausted=True)
        if status >= 500:
            raise SourceError(Outcome.UNKNOWN, f"SerpApi {what} HTTP {status}", status=status)
        if status != 200 or not isinstance(data, dict) or err:
            # R61-01：报错文字可能回显 key——交给 SourceError 统一“先脱敏、再截断”，这里不预先截断
            raise SourceError(Outcome.REJECTED, f"SerpApi {what}失败：{err or status}", status=status)
        return data

    async def search_image(self, jpeg: bytes, *, max_results: int = 5) -> list[ImageHit]:
        from .image_exec import run_image_cpu
        data = await run_image_cpu(self._shrink, jpeg, self.MAX_UPLOAD)
        status, up = await self._http.post_raw(self.IMAGE_URL, files={"image": ("q.jpg", data, "image/jpeg")},
                                               params={"api_key": self._key}, read_timeout=30)
        image_id = self._check(status, up, "上传").get("image_id")
        if not isinstance(image_id, str) or not image_id:
            raise SourceError(Outcome.UNKNOWN, "SerpApi 上传没有返回 image_id")
        try:
            res = await self._http.get_json(self.SEARCH_URL, {"engine": "google_lens", "image_id": image_id,
                                                              "type": "all", "hl": "zh-cn", "api_key": self._key},
                                            read_timeout=60)
        except SourceError as err:
            if err.status in (401, 403):
                err.auth_failed = True
            raise
        res = self._check(200, res, "搜索")
        items: list[tuple[str, str, str, str, str | None]] = []
        for x in res.get("exact_matches") or []:
            if isinstance(x, dict):
                items.append((x.get("title") or "", x.get("link") or "", x.get("source") or "", x.get("thumbnail") or "",
                              None))
        exact = _lens_hits(items, self.info.name, "source", max_results)
        items = [((x.get("title") or ""), (x.get("link") or ""), (x.get("source") or ""), (x.get("thumbnail") or ""), None)
                 for x in res.get("visual_matches") or [] if isinstance(x, dict)]
        visual = _lens_hits(items, self.info.name, "visual", max(0, max_results - len(exact)))
        return exact + visual


# ---------------- Google Lens（camofox 浏览器） ----------------
_UPLOAD_JS = """(() => {
 const bin = Uint8Array.from(atob('%s'), c => c.charCodeAt(0));
 const f = new File([bin], 'q.jpg', {type: 'image/jpeg'});
 const form = document.createElement('form'); form.method = 'POST'; form.enctype = 'multipart/form-data';
 form.action = 'https://lens.google.com/v3/upload?hl=zh-CN&re=df&stcs=' + Date.now() + '&ep=gsbubb';
 const inp = document.createElement('input'); inp.type = 'file'; inp.name = 'encoded_image';
 const dt = new DataTransfer(); dt.items.add(f); inp.files = dt.files; form.appendChild(inp);
 document.body.appendChild(form); form.submit(); return 'submitted'; })()"""

# 结果页：每条结果是 a[href^="/goto?url="]，真实网址在 data-sb 里没有明文；先取标题和跳转链接，
# 跳转链接再由浏览器解析一次（只解析前几条，避免多次访问 Google）。
_STATE_JS = """(() => { const u = new URL(location.href); const t = document.body ? (document.body.innerText || '') : '';
 const blocked = u.pathname.startsWith('/sorry') || !!document.querySelector('#captcha-form, form[action*="sorry"]');
 const res = Array.from(document.querySelectorAll('a[href*="/goto?url="]')).map(a => {
   const lines = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
   const img = a.querySelector('img'); return [lines.slice(1).join(' ') || lines[0] || '', a.href, lines[0] || '',
   img && /^https?:/.test(img.src) ? img.src : ''];
 }).filter(x => x[0]);
 return {blocked, res, path: u.pathname, host: u.hostname}; })()"""


class CamofoxLensProvider:
    """Google Lens 网页版，经外部 camofox 浏览器服务。
    D84：节流由 hub.pace 负责——同一时间只跑一个；两次之间至少隔 60 秒再加 0~30 秒随机；每小时最多 6 次；
    一次查询内部各操作（开页、上传、轮询、打开结果链接）之间随机停 0.8~2.5 秒。遇验证页冷却 2 小时（health 负责）。"""

    POLL, MAX_WAIT, RESOLVE = 1.5, 25.0, 3
    OPEN_WAIT, POLL_JITTER = 1.0, 0.8      # 审查建议：做成类属性，测试里可以置零
    MAX_UPLOAD = 700 * 1024        # R61-17：camofox 默认 evaluate 请求体 1 MiB，base64 再加 1/3，留出余量
    MAX_BODY = 4 * 1024 * 1024     # R61-16：响应上限，边收边数
    CLEANUP_TIMEOUT = 3.0          # R61-19：清理会话的总时限

    def __init__(self, base_url: str, api_key: str = "", *, rate_per_hour: int = 6,
                 transport: httpx.AsyncBaseTransport | None = None, pacer: Pacer | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key.strip()
        self._transport = transport
        self.pacer = pacer or Pacer("Google Lens（浏览器）", **{**BROWSER_DEFAULT, "per_hour": rate_per_hour})
        self.info = ProviderInfo("camofox_lens", "google_lens", "slow", paid=False, zero_marginal_cost=True,
                                 has_dates=False, label="Google Lens（浏览器）",
                                 extra={"image": True, "lens": True, "kinds": ["source", "visual"]})

    def _client(self) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json", "Accept-Encoding": ACCEPT_ENCODING}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        kw: dict[str, Any] = {"timeout": httpx.Timeout(connect=5.0, read=45.0, write=20.0, pool=5.0),
                              "trust_env": False, "headers": headers, "follow_redirects": False}
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.AsyncClient(**kw)

    async def _call(self, c: httpx.AsyncClient, method: str, path: str, body: Any = None) -> Any:
        from .net import read_limited
        try:
            async with c.stream(method, self._base + path, json=body) as r:
                if r.status_code in (401, 403):
                    raise SourceError(Outcome.REJECTED, "camofox 认证失败", status=r.status_code, auth_failed=True)
                if r.status_code >= 400:
                    raise SourceError(Outcome.UNKNOWN if r.status_code >= 500 else Outcome.REJECTED,
                                      f"camofox 返回 HTTP {r.status_code}", status=r.status_code)
                # R61-16：边收边数、边解压边数（与其他来源同一套 read_limited），超限立即停止
                raw = await read_limited(r, self.MAX_BODY)
        except SourceError:
            raise
        except TooLarge:
            raise SourceError(Outcome.UNKNOWN, "camofox 返回内容过大") from None
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise SourceError(Outcome.UNKNOWN, "camofox 返回不是 JSON") from None

    async def _eval(self, c: httpx.AsyncClient, tid: str, uid: str, js: str) -> Any:
        res = await self._call(c, "POST", f"/tabs/{tid}/evaluate", {"userId": uid, "expression": js})
        return res.get("result") if isinstance(res, dict) else None

    async def search_image(self, jpeg: bytes, *, max_results: int = 5, ready: Any = None) -> list[ImageHit]:
        from .image_exec import run_image_cpu
        if len(jpeg) > self.MAX_UPLOAD:
            jpeg = await run_image_cpu(shrink_jpeg, jpeg, self.MAX_UPLOAD)
        # D84：排队、守间隔、守每小时上限；等不到就跳过（不计失败）。R62-08：放行前再查冷却
        async with self.pacer.slot(ready):
            return await self._search_paced(jpeg, max_results)

    async def _search_paced(self, jpeg: bytes, max_results: int) -> list[ImageHit]:
        uid = f"search-hub-{uuid.uuid4().hex[:16]}"      # R61-26：多 Bot 共用一个 camofox 时不会撞号
        async with self._client() as c:
            try:
                with _private_logs():
                    return await asyncio.wait_for(self._run(c, uid, jpeg, max_results), timeout=self.MAX_WAIT + 30)
            except asyncio.TimeoutError:
                raise SourceError(Outcome.UNKNOWN, "Google Lens（浏览器）超时") from None
            finally:
                # R61-19：清理单独限时，不读正文；超时就交给 camofox 自己的空闲回收。
                # R62-13：清理期间（或之前）收到的取消必须在清理后照样抛出——只吞清理自己的超时和 HTTP 错误。
                # 用 shield + 自己的超时：外层取消时清理仍有界完成，再把取消传下去
                # R63-09：清理总共最多 CLEANUP_TIMEOUT 秒——取消发生在清理中途时只用剩下的时间，不再重新给 3 秒
                cleanup = asyncio.ensure_future(self._cleanup(c, uid))
                cleanup_deadline = time.monotonic() + self.CLEANUP_TIMEOUT
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), timeout=self.CLEANUP_TIMEOUT)
                except asyncio.CancelledError:
                    try:
                        await asyncio.wait_for(cleanup, timeout=max(0.01, cleanup_deadline - time.monotonic()))
                    except BaseException:  # noqa: BLE001 - 清理本身的失败不重要；下面重新抛出取消
                        pass
                    raise
                except Exception:  # noqa: BLE001 - 清理超时或 HTTP 错误：交给 camofox 空闲回收
                    cleanup.cancel()

    async def _cleanup(self, c: httpx.AsyncClient, uid: str) -> None:
        async with c.stream("DELETE", f"{self._base}/sessions/{uid}"):
            pass

    async def _run(self, c: httpx.AsyncClient, uid: str, jpeg: bytes, max_results: int) -> list[ImageHit]:
        tab = await self._call(c, "POST", "/tabs", {"userId": uid, "listItemId": "lens",
                                                    "url": "https://lens.google.com/?hl=zh-CN"})
        tid = tab.get("tabId") if isinstance(tab, dict) else None
        if not isinstance(tid, str) or not tid:
            raise SourceError(Outcome.UNKNOWN, "camofox 没有返回标签页")
        await asyncio.sleep(self.OPEN_WAIT)
        await self.pacer.step()                # 打开页面后像人一样停一下再上传
        await self._eval(c, tid, uid, _UPLOAD_JS % base64.b64encode(jpeg).decode("ascii"))
        t0 = time.monotonic()
        st: dict = {}
        while time.monotonic() - t0 < self.MAX_WAIT:
            await asyncio.sleep(self.POLL + random.uniform(0.0, self.POLL_JITTER))
            st = await self._eval(c, tid, uid, _STATE_JS) or {}
            if not isinstance(st, dict):
                st = {}
            if st.get("blocked"):
                raise SourceError(Outcome.REJECTED, "Google Lens 要求验证（异常流量），未尝试绕过", retry_after=7200)
            if st.get("res") or (st.get("path") == "/search" and time.monotonic() - t0 > 6):
                break
        res = [x for x in (st.get("res") or []) if isinstance(x, list) and len(x) >= 4]
        if not res:
            return []
        # 只解析前几条跳转链接：在同一标签页打开，读最终地址
        items: list[tuple[str, str, str, str, str | None]] = []
        for title, goto, site, thumb in res[: max(self.RESOLVE, 0)]:
            await self.pacer.step()            # 打开每个结果链接之间随机停顿
            try:
                nav = await self._call(c, "POST", f"/tabs/{tid}/navigate", {"userId": uid, "url": goto})
            except SourceError:
                break
            real = nav.get("url") if isinstance(nav, dict) else ""
            if isinstance(real, str) and _valid_url(real) and not _host(real).endswith("google.com"):
                items.append((title, real, site, thumb, None))
            if len(items) >= max_results:
                break
        return _lens_hits(items, self.info.name, "visual", max_results)
