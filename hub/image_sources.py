"""以图搜引擎（第 4 阶段，D25、D26、D28、D29）。

插件本体接官方 JSON API：trace.moe、AnimeTrace、SauceNAO；抓网页的 Yandex、Bing 见 image_web.py（D54）。
其余抓网页引擎（Lens/百度/TinEye…）走外部可选容器 picimagesearch-api（D26），不部署就不可用。

每个引擎返回 ImageHit：除了标题/URL，还带"匹配性质"（kind）和引擎自己的原始分数（不跨引擎归一化，
ARCHITECTURE「以图搜用引擎自带的相似度」），以及三级判定 level：
- hit      命中：可以表述为"出处 / 就是"
- likely   疑似：只能说"相似线索 / 可能是"
- weak     弱：只作参考
字段依据见 HOST_FACTS「以图搜引擎」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .net import Http, Outcome, SourceError
from .providers import ProviderInfo, _clip, _valid_url


@dataclass
class ImageHit:
    title: str
    url: str
    snippet: str
    kind: str                  # scene / character / source / visual
    score: float | None        # 引擎原始分数：trace.moe 0–1、SauceNAO 0–100；AnimeTrace / 容器没有
    level: str                 # hit / likely / weak
    source: str = ""
    nsfw: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    published_at: str | None = None
    rank: int = 0


def _fmt_ts(sec: Any) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


async def _post_bytes(http: Http, url: str, *, content: bytes | None = None, json_body: Any = None,
                      headers: dict[str, str] | None = None, read_timeout: float = 30.0) -> tuple[int, Any]:
    """以图搜引擎的错误语义各不相同（AnimeTrace 用 HTTP 500 + 业务码），所以这里返回 (status, json) 由适配器自己判断。
    0.5.2（F01）：经 Http.post_raw 受限读取，不再直接 resp.json()。"""
    return await http.post_raw(url, content=content, json_body=json_body, headers=headers, read_timeout=read_timeout)


# ---------------- trace.moe ----------------
class TraceMoeProvider:
    """番剧截图 → 作品、集数、时间点。免 key：100 次/滚动 24 小时、并发 1（D33）。"""

    URL = "https://api.trace.moe/search"
    HIT, LIKELY = 0.90, 0.80   # 官方：similarity < 0.9 多半不准

    def __init__(self, http: Http, api_key: str = "", *, allow_adult: bool = False) -> None:
        self._http = http
        self._key = api_key.strip()
        self._allow_adult = allow_adult
        self.info = ProviderInfo("tracemoe", "tracemoe", "medium", paid=False, zero_marginal_cost=True,
                                 has_dates=False, label="trace.moe", extra={"image": True, "kinds": ["scene"]})

    async def search_image(self, jpeg: bytes, *, max_results: int = 3) -> list[ImageHit]:
        headers = {"Content-Type": "image/jpeg"}
        if self._key:
            headers["x-trace-key"] = self._key
        status, data = await _post_bytes(self._http, self.URL + "?anilistInfo", content=jpeg, headers=headers)
        if status != 200 or not isinstance(data, dict):
            msg = data.get("error") if isinstance(data, dict) else ""
            if status == 402:
                quota = "quota" in str(msg).lower()
                raise SourceError(Outcome.REJECTED, "额度用尽" if quota else "并发超限（同时只能搜一张）",
                                  status=402, quota_exhausted=quota, retry_after=None if quota else 5)
            if status == 400:
                raise SourceError(Outcome.REJECTED, "图片无法处理", status=400)
            if status == 403:
                raise SourceError(Outcome.REJECTED, "key 无效", status=403, auth_failed=True)
            if status == 429:
                raise SourceError(Outcome.REJECTED, "请求过于频繁", status=429, retry_after=60)
            raise SourceError(Outcome.UNKNOWN, f"HTTP {status}", status=status)
        out: list[ImageHit] = []
        seen: set[tuple] = set()
        for r in data.get("result") or []:
            if not isinstance(r, dict):
                continue
            sim = float(r.get("similarity") or 0)
            a = r.get("anilist") if isinstance(r.get("anilist"), dict) else {}
            key = (a.get("id"), r.get("episode"))
            if key in seen:
                continue
            seen.add(key)
            t = a.get("title") or {}
            name = t.get("chinese") or t.get("native") or t.get("romaji") or t.get("english") or r.get("filename") or "未知作品"
            alt = [x for x in (t.get("native"), t.get("romaji"), t.get("english")) if x and x != name]
            ep = r.get("episode")
            ep_s = f"第 {ep} 集" if isinstance(ep, (int, float)) else ("" if ep in (None, "") else f"第 {ep} 集")
            at = _fmt_ts(r.get("at") if r.get("at") is not None else r.get("from"))
            year = a.get("seasonYear") or (a.get("startDate") or {}).get("year")
            adult = bool(a.get("isAdult"))
            if adult and not self._allow_adult:
                continue
            level = "hit" if sim >= self.HIT else "likely" if sim >= self.LIKELY else "weak"
            url = a.get("siteUrl") or (f"https://anilist.co/anime/{a['id']}" if a.get("id") else "")
            if not _valid_url(url):
                continue
            snippet = "；".join(x for x in (
                " · ".join(x for x in (ep_s, f"约 {at} 处" if at else "") if x),
                f"{year} 年" if year else "", f"其他名称：{' / '.join(alt[:2])}" if alt else "",
                f"相似度 {sim:.0%}") if x)
            out.append(ImageHit(_clip(name, 80), url, snippet, "scene", round(sim, 4), level, self.info.name,
                                nsfw=adult, extra={"episode": ep, "at": at, "anilist_id": a.get("id")}))
            if len(out) >= max_results:
                break
        return out


# ---------------- AnimeTrace ----------------
class AnimeTraceProvider:
    """动漫 / Galgame 角色识别（"这是谁"）。免 key；额度与条款未查到（HOST_FACTS）。"""

    URL = "https://api.animetrace.com/v1/search"

    def __init__(self, http: Http, *, model: str = "") -> None:
        self._http = http
        self._model = model.strip()
        self.info = ProviderInfo("animetrace", "animetrace", "medium", paid=False, zero_marginal_cost=True,
                                 has_dates=False, label="AnimeTrace", extra={"image": True, "kinds": ["character"]})

    async def search_image(self, jpeg: bytes, *, max_results: int = 3) -> list[ImageHit]:
        import base64

        body: dict[str, Any] = {"base64": base64.b64encode(jpeg).decode("ascii"), "is_multi": 1, "ai_detect": 1}
        if self._model:
            body["model"] = self._model
        status, data = await _post_bytes(self._http, self.URL, json_body=body)
        if not isinstance(data, dict):
            raise SourceError(Outcome.UNKNOWN if status >= 500 else Outcome.REJECTED, f"HTTP {status}", status=status)
        code = data.get("code")
        if code not in (0, "0"):
            msg = str(data.get("zh_message") or data.get("message") or f"错误码 {code}")
            raise SourceError(Outcome.REJECTED, msg, status=status)
        out: list[ImageHit] = []
        boxes = [b for b in (data.get("data") or []) if isinstance(b, dict)]
        for bi, box in enumerate(boxes):
            chars = [c for c in (box.get("character") or []) if isinstance(c, dict) and c.get("character")]
            if not chars:
                continue
            unsure = bool(box.get("not_confident"))
            for ci, c in enumerate(chars[:max_results]):
                level = "likely" if (ci == 0 and not unsure) else "weak"
                who = str(c.get("character"))
                work = str(c.get("work") or "")
                where = f"（图中第 {bi + 1} 个人物）" if len(boxes) > 1 else ""
                snippet = f"作品：{work}{where}" + ("；模型自己标注了“不确定”" if unsure else "")
                url = "https://www.animetrace.com/"
                out.append(ImageHit(_clip(f"{who}（{work}）" if work else who, 80), url, snippet, "character",
                                    None, level, self.info.name,
                                    extra={"character": who, "work": work, "box": box.get("box"), "rank": ci,
                                           "ai_generated": bool(data.get("ai"))}))
        return out[: max_results * 2]


# ---------------- SauceNAO ----------------
_SAUCE_TITLE = ("title", "material", "jp_name", "eng_name", "source")
_SAUCE_AUTHOR = ("author", "member_name", "creator", "twitter_user_handle", "author_name", "user_name", "artist",
                 "company")
class SauceNaoProvider:
    URL = "https://saucenao.com/search.php"
    # 2026-10-29 实测：风景照被 SauceNAO 以 85.5% 匹配到 DeviantArt 上的 Minecraft 像素画山 → 80% 不够当“命中”。
    # ≥90 命中；80–90 疑似，由执行器下载缩略图做 dHash，同图才升为命中；70–80 弱线索；更低的丢弃。
    HIT, LIKELY, FLOOR = 90.0, 80.0, 70.0

    def __init__(self, http: Http, api_key: str, *, allow_adult: bool = False) -> None:
        self._http = http
        self._key = api_key.strip()
        self._allow_adult = allow_adult
        self.info = ProviderInfo("saucenao", "saucenao", "medium", paid=False, zero_marginal_cost=True,
                                 has_dates=False, label="SauceNAO",
                                 extra={"image": True, "kinds": ["source"], "verify_thumbs": True})

    async def search_image(self, jpeg: bytes, *, max_results: int = 3) -> list[ImageHit]:
        # SauceNAO 只接受 URL 参数里的 key；日志侧由 net.install_log_filter() 遮掉，错误文字经 SourceError 脱敏（F03）
        params = {"output_type": 2, "api_key": self._key, "numres": max(3, max_results), "db": 999,
                  "hide": 0 if self._allow_adult else 2}
        status, data = await self._http.post_raw(self.URL, params=params,
                                                 files={"file": ("image.jpg", jpeg, "image/jpeg")})
        if status == 429:
            raise SourceError(Outcome.REJECTED, "额度或频率超限", status=429, retry_after=30)
        if status == 403:
            raise SourceError(Outcome.REJECTED, "key 无效", status=403, auth_failed=True)
        if status >= 400:
            raise SourceError(Outcome.REJECTED if status < 500 else Outcome.UNKNOWN, f"HTTP {status}", status=status,
                              auth_failed=status == 401)
        if data is None:
            raise SourceError(Outcome.UNKNOWN, "返回不是 JSON（可能被 Cloudflare 拦截）")
        header = data.get("header") if isinstance(data, dict) else None
        if not isinstance(header, dict):
            raise SourceError(Outcome.UNKNOWN, "返回格式不符")
        st = header.get("status", 0)
        if isinstance(st, int) and st != 0:
            raw_msg = str(header.get("message") or "")
            msg = raw_msg.replace(self._key, "***") if len(self._key) >= 6 else raw_msg   # F03：与 set_secrets 同样的长度下限
            if st < 0:
                raise SourceError(Outcome.REJECTED, msg or "请求被拒绝", status=st,
                                  auth_failed="api key" in raw_msg.lower())
            raise SourceError(Outcome.UNKNOWN, msg or "服务端错误", status=st)
        out: list[ImageHit] = []
        for r in data.get("results") or []:
            h, d = r.get("header") or {}, r.get("data") or {}
            try:
                sim = float(h.get("similarity"))
            except (TypeError, ValueError):
                continue
            if h.get("hidden") and not self._allow_adult:
                continue
            if sim < self.FLOOR:
                continue
            url = _sauce_url(d)
            if not _valid_url(url):
                continue
            title = next((str(d[k]) for k in _SAUCE_TITLE if d.get(k)), "") or str(h.get("index_name") or "")
            author = next(((", ".join(d[k]) if isinstance(d[k], list) else str(d[k])) for k in _SAUCE_AUTHOR if d.get(k)), "")
            level = "hit" if sim >= self.HIT else "likely" if sim >= self.LIKELY else "weak"
            idx = str(h.get("index_name") or "").split(":")[0].replace("Index #", "库 #")
            snippet = "；".join(x for x in (f"作者：{author}" if author else "", idx, f"相似度 {sim:.1f}%",
                                            f"第 {d['part']} 集" if d.get("part") else "",
                                            f"约 {d['est_time']} 处" if d.get("est_time") else "") if x)
            kind = "scene" if d.get("est_time") else "source"
            thumb = h.get("thumbnail") if level == "likely" and _valid_url(h.get("thumbnail")) else ""
            # 0.5.7：开启 allow_adult 时保留 SauceNAO 标为隐藏的结果，并像 trace.moe 一样标注“成人向”
            out.append(ImageHit(_clip(title or _host(url), 80), url, snippet, kind, sim, level, self.info.name,
                                nsfw=bool(h.get("hidden")),
                                extra={"author": author, "index": h.get("index_id"), "thumbnail": thumb,
                                       "remaining": header.get("long_remaining")}))
            if len(out) >= max_results:
                break
        return out


def _sauce_url(d: dict) -> str:
    if d.get("pixiv_id"):
        return f"https://www.pixiv.net/artworks/{d['pixiv_id']}"
    urls = d.get("ext_urls") or []
    return urls[0] if urls else ""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


# ---------------- 外部容器（picimagesearch-api，D26） ----------------
class ContainerImageProvider:
    """抓网页的以图搜引擎（Yandex/Bing/百度/Lens/TinEye…）。容器同一时间只跑一个搜索（忙时 429）。
    候选不带相似度 → 统一标为 weak，靠 dHash 验证升级（IMPLEMENTATION_PLAN 第 8 条）。"""

    def __init__(self, http: Http, base_url: str, api_key: str, engine: str) -> None:
        self._http = http
        self._base = base_url.strip().rstrip("/")
        self._key = api_key.strip()
        self._engine = engine
        label = {"yandex": "Yandex", "bing": "Bing", "baidu": "百度识图", "google_lens": "Google Lens",
                 "tineye": "TinEye", "copyseeker": "Copyseeker"}.get(engine, engine)
        self.info = ProviderInfo(f"imgc_{engine}", f"imgc_{engine}", "slow", paid=False, zero_marginal_cost=True,
                                 has_dates=False, label=label + "（容器）",
                                 extra={"image": True, "kinds": ["visual"], "container": True})

    async def search_image(self, jpeg: bytes, *, max_results: int = 5) -> list[ImageHit]:
        import base64

        body = {"image_base64": base64.b64encode(jpeg).decode("ascii"), "engine": self._engine,
                "search_type": "all", "max_results": max_results}
        status, data = await _post_bytes(self._http, self._base + "/v1/search", json_body=body,
                                         headers={"Authorization": f"Bearer {self._key}"}, read_timeout=95.0)
        if status != 200 or not isinstance(data, dict):
            err = data.get("error") if isinstance(data, dict) else ""
            reason = data.get("reason") if isinstance(data, dict) else ""
            if status == 401:
                raise SourceError(Outcome.REJECTED, "容器认证失败", status=401, auth_failed=True)
            if status == 429:
                raise SourceError(Outcome.REJECTED, "容器正忙（同一时间只跑一个搜索）", status=429, retry_after=10)
            if status in (400, 413):
                raise SourceError(Outcome.REJECTED, f"容器拒绝请求（{err}）", status=status)
            if status == 502 and reason in ("access_limited", "javascript_required"):
                raise SourceError(Outcome.REJECTED, f"{self.info.label} 被上游限制（{reason}）", status=502, retry_after=600)
            raise SourceError(Outcome.UNKNOWN, f"{err or 'HTTP ' + str(status)}{'：' + reason if reason else ''}", status=status)
        out: list[ImageHit] = []
        for c in data.get("candidates") or []:
            if not isinstance(c, dict) or not _valid_url(c.get("url")):
                continue
            mt = c.get("match_type")
            out.append(ImageHit(_clip(c.get("title") or c.get("source") or _host(c["url"]), 80), c["url"],
                                f"{c.get('source') or _host(c['url'])}" + ("；引擎标注为完全相同" if mt == "exact" else ""),
                                "visual", None, "weak", self.info.name, extra={"match_type": mt}))
        return out
