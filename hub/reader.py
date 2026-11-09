"""网页读取（D3、D43 第 8 条）。

读取链：Host ctx.web.fetch → fetch-url 插件公开 API → 内置读取器。
内置读取器的 SSRF 防护移植自 doubao-search 插件 `image_loader.py`
（MIT，Copyright (c) 2026 LowValueTarget777，https://github.com/lowvaluetarget777/doubao-search）：
先解析域名、要求所有地址都是公网地址，再用已校验的 IP 建立连接（HTTPS 通过 sni_hostname 保留证书校验），
重定向每一跳重新校验。在此基础上改为读取 HTML/纯文本，并加了正文抽取。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
from dataclasses import dataclass
from socket import SOCK_STREAM
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .net import ACCEPT_ENCODING, Outcome, SourceError, TooLarge, read_limited

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
REDIRECTS = {301, 302, 303, 307, 308}
MAX_BYTES = 3 * 1024 * 1024
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36 maibot-search-hub")


@dataclass
class Page:
    url: str
    title: str
    text: str
    via: str          # host_web / fetch_url / builtin


class Rejected(SourceError):
    pass


def _rej(msg: str) -> Rejected:
    return Rejected(Outcome.NOT_SENT, msg)


async def _default_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=SOCK_STREAM)
    return list({r[4][0] for r in records})


# H01：is_global 对组播地址和 64:ff9b::/96（NAT64 知名前缀）也返回 True，但它们不是可直连的公网单播 HTTP 目标
_NOT_UNICAST = (ipaddress.ip_network("64:ff9b::/96"),)


def _is_public_unicast(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return False
    return not (isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _NOT_UNICAST))


async def validate_public_url(url: str, resolver: Resolver) -> tuple[list[str], str, str | None]:
    """返回 (用已校验 IP 构造的请求 URL 列表, Host 头, SNI 主机名)。任何非公网地址一律拒绝。"""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise _rej("URL 无效") from None
    if p.scheme not in ("http", "https") or not p.hostname:
        raise _rej("只允许 http/https 网址")
    if p.username is not None or p.password is not None:
        raise _rej("网址不能带账号密码")
    host = p.hostname
    try:
        literal = ipaddress.ip_address(host)
        addrs: Sequence[str] = [str(literal)]
        sni = None
        host_name = str(literal)
    except ValueError:
        try:
            host_name = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _rej("域名格式无效") from None
        try:
            addrs = await resolver(host_name, port or (443 if p.scheme == "https" else 80))
        except OSError:
            raise _rej("域名无法解析") from None
        if not addrs:
            raise _rej("域名无法解析")
        sni = host_name if p.scheme == "https" else None
    checked = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            raise _rej("域名解析出无效地址") from None
        if not _is_public_unicast(ip):
            raise _rej("目标地址不是公网地址，已拒绝访问")
        checked.append(ip)
    suffix = f":{port}" if port is not None else ""
    checked.sort(key=lambda ip: ip.version)  # IPv4 优先：很多容器没有 IPv6 出口
    urls = []
    for ip in checked[:4]:
        rh = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        urls.append(urlunsplit((p.scheme, rh + suffix, p.path or "/", p.query, "")))
    hh = (f"[{host_name}]" if ":" in host_name else host_name) + suffix
    return urls, hh, sni


# ---------------- 正文抽取（不依赖第三方库：Host 环境没有 bs4/lxml） ----------------
_DROP = re.compile(r"<(script|style|noscript|svg|template|iframe|head|nav|footer|aside|form)\b.*?</\1\s*>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|blockquote|pre)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_MAIN = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def html_to_text(raw: str) -> tuple[str, str]:
    m = _TITLE.search(raw)
    title = html.unescape(_TAG.sub("", m.group(1))).strip() if m else ""
    body = _COMMENT.sub(" ", raw)
    mains = _MAIN.findall(body)
    if mains:
        candidate = max((x[1] for x in mains), key=len)
        if len(_TAG.sub("", candidate)) > 400:
            body = candidate
    body = _DROP.sub(" ", body)
    body = _BLOCK.sub("\n", body)
    body = html.unescape(_TAG.sub(" ", body))
    lines = [" ".join(line.split()) for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


def _decode(content: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if m:
        try:
            return content.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    head = content[:2048].decode("ascii", errors="ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    if m:
        try:
            return content.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    try:
        from charset_normalizer import from_bytes  # Host 自带

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001
        pass
    return content.decode("utf-8", errors="replace")


class BuiltinReader:
    """内置读取器。

    F02（INDEPENDENT_REVIEW）：连接按 IP 建立、证书按 SNI 校验时，连接池以“IP:端口”为键复用连接，
    两个域名落在同一 IP 会共用一条已为前一个域名做过 TLS 的连接。所以读取器用**独立的、不保持连接**的客户端
    （每次请求重新握手）。配了代理时，经 CONNECT 隧道的 TLS 用的是请求 URL 里的主机名：此时仍先做公网校验，
    但请求用原域名发出（不固定 IP），证书按域名校验；DNS 由代理解析，这一点写在 README 的代理说明里。
    """

    def __init__(self, client: httpx.AsyncClient, resolver: Resolver | None = None, timeout: float = 15.0,
                 *, pin_ip: bool = True, owns_client: bool = False) -> None:
        self._client = client
        self._resolver = resolver or _default_resolver
        self._timeout = timeout
        self._pin_ip = pin_ip
        self._owns = owns_client

    @classmethod
    def create(cls, *, proxy: str = "", timeout: float = 15.0) -> "BuiltinReader":
        client = httpx.AsyncClient(
            follow_redirects=False, trust_env=False, proxy=proxy or None,
            limits=httpx.Limits(max_keepalive_connections=0, keepalive_expiry=0),
        )
        return cls(client, timeout=timeout, pin_ip=not proxy, owns_client=True)

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    @property
    def resolver(self) -> Resolver:
        return self._resolver

    async def fetch(self, url: str) -> Page:
        ctype, data, final = await self._download(url, MAX_BYTES, TEXT_TYPES,
                                                  "text/html,text/plain;q=0.9,*/*;q=0.1")
        raw = _decode(data, ctype)
        if "html" in ctype:
            title, text = html_to_text(raw)
        else:
            title, text = "", raw.strip()
        if not text:
            raise SourceError(Outcome.REJECTED, "网页没有可读正文（可能需要 JavaScript）")
        return Page(final, title, text, "builtin")

    async def fetch_bytes(self, url: str, *, max_bytes: int, accept: tuple[str, ...], hop: Any = None) -> bytes:
        """按内容类型下载二进制（第 4 阶段：图片 URL 输入、dHash 候选图）。同样的 SSRF 防护。

        hop：可选的每跳节流钩子（R63-06），hop(当前网址) 返回异步上下文、产出是否放行；每一跳按那一跳的实际网站
        取名额，只包住这一次请求。不放行时抛 SourceError（NOT_SENT）。"""
        _, data, _ = await self._download(url, max_bytes, accept, "image/*;q=0.9,*/*;q=0.1", truncate=False, hop=hop)
        return data

    async def _download(self, url: str, max_bytes: int, accept: tuple[str, ...], accept_header: str,
                        truncate: bool = True, hop: Any = None) -> tuple[str, bytes, str]:
        current = url
        for n in range(6):
            req_urls, host_header, sni = await validate_public_url(current, self._resolver)
            if not self._pin_ip:
                # 走代理：已确认目标是公网地址；用原域名发出，TLS 证书按域名校验
                req_urls, sni = [current.split("#", 1)[0]], None
            if hop is None:
                result = await self._get_once(req_urls, host_header, sni, current, n, max_bytes, accept,
                                              accept_header, truncate)
            else:
                async with hop(current) as ok:
                    if not ok:
                        raise SourceError(Outcome.NOT_SENT, "等不到下载名额（按网站限速）")
                    result = await self._get_once(req_urls, host_header, sni, current, n, max_bytes, accept,
                                                  accept_header, truncate)
            if isinstance(result, str):
                current = result
                continue
            return result[0], result[1], current
        raise _rej("重定向次数过多")

    async def _get_once(self, req_urls: list[str], host_header: str, sni: str | None, current: str, hop: int,
                        max_bytes: int, accept: tuple[str, ...], accept_header: str, truncate: bool):
        last: SourceError | None = None
        for req_url in req_urls:
            try:
                return await self._get_one(req_url, host_header, sni, current, hop, max_bytes, accept,
                                           accept_header, truncate)
            except SourceError as err:
                if err.outcome is not Outcome.NOT_SENT or err.message != "连接失败":
                    raise
                last = err  # 这个地址连不上：换下一个已校验地址
        raise last or SourceError(Outcome.NOT_SENT, "连接失败")

    async def _get_one(self, req_url: str, host_header: str, sni: str | None, current: str, hop: int,
                       max_bytes: int, accept: tuple[str, ...], accept_header: str, truncate: bool):
        ext = {"sni_hostname": sni} if sni else {}
        try:
            async with self._client.stream(
                "GET", req_url, headers={"Host": host_header, "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                                         "Accept": accept_header, "Accept-Encoding": ACCEPT_ENCODING},
                follow_redirects=False, timeout=self._timeout, extensions=ext,
            ) as resp:
                if resp.status_code in REDIRECTS:
                    loc = resp.headers.get("location")
                    if not loc or hop >= 5:
                        raise _rej("重定向异常")
                    return urljoin(current, loc)
                if resp.status_code >= 400:
                    # 错误体不读（F01）
                    raise SourceError(Outcome.REJECTED if resp.status_code < 500 else Outcome.UNKNOWN,
                                      f"网页返回 HTTP {resp.status_code}", status=resp.status_code)
                ctype = resp.headers.get("content-type", "").lower()
                if not any(t in ctype for t in accept):
                    raise SourceError(Outcome.REJECTED, f"不支持的内容类型：{ctype.split(';')[0] or '未知'}")
                # F01：边解压边计数；文字截断到上限，二进制（图片）超限即拒绝
                data = await read_limited(resp, max_bytes, truncate=truncate)
        except SourceError:
            raise
        except TooLarge:
            raise SourceError(Outcome.REJECTED, "文件过大") from None
        except asyncio.CancelledError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise SourceError(Outcome.NOT_SENT, "连接失败") from None
        except httpx.ReadTimeout:
            raise SourceError(Outcome.NOT_SENT, "连接失败") from None  # GET 可安全换下一个地址重试
        except Exception as exc:  # noqa: BLE001
            raise SourceError(Outcome.UNKNOWN, type(exc).__name__) from None
        return ctype, data


_GONE = re.compile(r"(?<!\d)(404|410)(?!\d)|not found|页面不存在|网页不存在|已删除|已被删除", re.I)
# 别的读取服务的报错只认明确的状态码或“网页不存在”，避免把“服务未找到”之类当成网页不存在
_GONE_STRICT = re.compile(r"(?<!\d)(404|410)(?!\d)|页面不存在|网页不存在")


def read_failure_reason(err: BaseException | str) -> tuple[str, bool]:
    """读取失败 → (给人看的原因, 是否“网页不存在”)。D72 补充：让模型能如实说明为什么读不到。"""
    status = getattr(err, "status", None)
    msg = getattr(err, "message", None) or str(err)
    if status in (404, 410) or (status is None and _GONE.search(msg or "")):
        return "网页不存在或已被删除（HTTP 404/410）", True
    if status in (401, 403) or re.search(r"(?<!\d)(401|403)(?!\d)|拒绝|forbidden|denied", msg or "", re.I):
        return "网站拒绝自动访问", False
    if status == 429 or re.search(r"(?<!\d)429(?!\d)|频繁|rate", msg or "", re.I):
        return "网站限制访问频率", False
    if isinstance(err, asyncio.TimeoutError) or re.search(r"超时|timeout|连接失败|连不上", msg or "", re.I):
        return "网站连不上或超时", False
    if re.search(r"JavaScript", msg or ""):
        return "网页要靠 JavaScript 显示内容，读不到正文", False
    if re.search(r"公网|内网", msg or ""):
        return "不是公网地址，已拒绝访问", False
    return (msg or "读取失败")[:60], False


class PageGone(SourceError):
    """D72 补充：网页不存在（404/410）。换读取方式也读不到同一个网址，读取链到此为止。"""


class ReadChain:
    """D3：Host web.fetch → fetch-url API → 内置读取器。前一级不可用（未配置/未安装）时才往下走。
    D72 补充：任何一级明确报告 404/410 时直接结束（换读取方式访问的还是同一个网址），并抛出 PageGone。"""

    FETCH_URL_API = "com.0-hz.fetch-url.fetch_url"

    def __init__(self, ctx: Any, builtin: BuiltinReader, *, use_host: bool = True, use_fetch_url: bool = True) -> None:
        self._ctx = ctx
        self._builtin = builtin
        self._use_host = use_host
        self._use_fetch_url = use_fetch_url
        self._host_off_until = 0.0
        self._fetch_url_off_until = 0.0

    async def read(self, url: str) -> Page:
        import time

        # 先做一次公网校验：无论走哪一级，都不让模型借插件去读内网地址
        await validate_public_url(url, self._builtin.resolver)
        errors: list[str] = []
        now = time.monotonic()
        if self._use_host and now >= self._host_off_until:
            try:
                res = await self._ctx.web.fetch(url)
                payload = res.get("result") if isinstance(res, dict) and isinstance(res.get("result"), dict) else res
                if isinstance(payload, dict) and isinstance(payload.get("content"), str) and payload["content"].strip():
                    return Page(str(payload.get("url") or url), str(payload.get("title") or ""),
                                payload["content"], "host_web")
                errors.append("Host 读取无正文")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "未启用" in msg or "未配置" in msg or "能力" in msg:
                    self._host_off_until = now + 1800
                elif _GONE_STRICT.search(msg):
                    raise PageGone(Outcome.REJECTED, "网页不存在或已被删除（HTTP 404/410）", status=404) from None
                errors.append("Host 读取不可用")
        if self._use_fetch_url and now >= self._fetch_url_off_until:
            try:
                res = await self._ctx.api.call(self.FETCH_URL_API, url=url, on_exceed="truncate", return_image=False)
                if isinstance(res, dict) and res.get("success") and isinstance(res.get("result"), dict):
                    r = res["result"]
                    content = str(r.get("content") or "")
                    if r.get("success", True) and content.strip() and not content.startswith("抓取失败"):
                        return Page(url, str(r.get("title") or ""), content, "fetch_url")
                    if _GONE_STRICT.search(str(r.get("error") or content)) and not content.strip()[200:]:
                        raise PageGone(Outcome.REJECTED, "网页不存在或已被删除（HTTP 404/410）", status=404)
                    errors.append("fetch-url 未取到正文")
                else:
                    err = str((res or {}).get("error") if isinstance(res, dict) else res)
                    if "未找到" in err or "不存在" in err or "解析失败" in err or "not found" in err.lower():
                        self._fetch_url_off_until = now + 1800
                    errors.append("fetch-url 不可用")
            except PageGone:
                raise
            except Exception:  # noqa: BLE001
                self._fetch_url_off_until = now + 1800
                errors.append("fetch-url 不可用")
        try:
            return await self._builtin.fetch(url)
        except SourceError as err:
            if err.status in (404, 410):
                raise PageGone(Outcome.REJECTED, "网页不存在或已被删除（HTTP 404/410）", status=err.status) from None
            raise


# ---------------- 摘录 ----------------
def excerpt(text: str, focus: str, limit: int = 2200) -> tuple[str, bool]:
    """按 focus 关键词取正文里最相关的部分；没有 focus 或一个词都没命中时取开头。返回 (摘录, 是否截断)。

    做法（规则尽量少，每条对应一个已知的坑）：
    1. 关注词去重只去掉“匹配起来等价”的（R66-01：用和匹配同一套规则判断，ß 和 SS 不算重复）。
    2. 每个关注词各自独立在原文上 re.IGNORECASE 匹配（R65-04：短词不吞长词；R64-03/R65-03：位置就是原文位置），
       每词最多 2000 个命中；比 limit 长的命中不参与选窗口（R65-01）。
    3. 选窗口：窗口是 [w, w+limit)，命中完整落在里面才算。取“不同词最多、其次命中最多”的窗口，同分取起点最早的命中候选。
       用扫描线精确求最优（R66-02：命中互相重叠时，双指针会漏掉更好的窗口），计算量 O(H log H)（R64-01）。
    4. 核心 = 最优窗口里第一个命中的起点到最后一个命中的终点，一定完整保留（R64-02）；前面留约三分之一给上文。
    5. 两端只在窗口头尾各 15% 的范围里对齐句子边界；只认中文句末标点、换行，或后面跟空白的英文标点（R65-02）。
    6. 剩余预算给窗口外的其他命中开小窗口；分隔符算进 limit（R64-04）。"""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    hits = _hits(text, _focus_terms(focus), limit)
    if not hits:
        return _head(text, limit), True
    core_a, core_b, _ = _select(hits, limit)
    a, b = _grow(text, core_a, core_b, limit)
    parts = [(a, b)]
    used = b - a
    sep = "\n……\n"
    for s, e, _ in hits:
        room = limit - used - len(sep)
        if room < 60:
            break
        if any(x <= s and e <= y for x, y in parts):
            continue
        x, y = _grow(text, s, e, min(room, max(120, limit // 4)))
        if any(not (y <= px or x >= py) for px, py in parts):
            continue
        parts.append((x, y))
        used += (y - x) + len(sep)
    parts.sort()
    return sep.join(text[x:y] for x, y in parts), True


_MAX_HITS_PER_TERM = 2000      # R64-01：每个关注词最多看前 2000 个命中，计算量有上限
# 句子边界：中文句末标点、换行；英文标点后面必须跟空白（URL 里的 ?、命令里的 ; 不算）
_SENT_END = re.compile(r"[。！？；\n]|[.!?;](?=\s)")
_EDGE = 0.15                   # 只在窗口两头各 15% 的范围里找边界


def _focus_terms(focus: str) -> list[str]:
    """关注词：按空白和中文标点切开，保留原样，最多 12 个。
    R66-01：只有“用 re.IGNORECASE 匹配时等价”的才算重复——ß 和 SS 在 casefold 下相同，但正则不认为相等，
    两个都要留着，否则原文里写的 SS 就找不到了。"""
    out: list[str] = []
    for t in re.split(r"[\s,，、；;]+", focus or ""):
        if t and not any(re.fullmatch(re.escape(p), t, re.IGNORECASE) for p in out):
            out.append(t)
            if len(out) >= 12:
                break
    return out


def _hits(text: str, terms: list[str], limit: int) -> list[tuple[int, int, int]]:
    """所有命中 (起点, 终点, 词序号)，按起点排序。比 limit 长的命中丢掉。"""
    hits: list[tuple[int, int, int]] = []
    for k, t in enumerate(terms):
        for n, m in enumerate(re.finditer(re.escape(t), text, re.IGNORECASE)):
            if n >= _MAX_HITS_PER_TERM:
                break
            if m.end() - m.start() <= limit:
                hits.append((m.start(), m.end(), k))
    hits.sort()
    return hits


def _select(hits: list[tuple[int, int, int]], limit: int) -> tuple[int, int, tuple[int, int]]:
    """在 [w, w+limit) 形式的窗口里，找完整包含的命中“不同词数最多、其次命中数最多”的一个。
    候选 w 只取命中起点；同分时取起点最早的那个候选（R67-02：不是“整数意义上最早的 w”，那个 w 可能在第一个命中之前，
    但包含的命中集合相同，所以核心相同）。返回 (核心起点, 核心终点, (不同词数, 命中数))，核心 = 第一个命中起点到最后一个命中终点。

    最优窗口总可以右移到左端贴住它包含的第一个命中的起点而不丢命中，所以候选 w 只需取各命中的起点。
    命中 j 被窗口 w 包含 ⇔ e_j - limit <= w <= s_j：按 w 从小到大扫，按 e_j 顺序加入、按 s_j 顺序移除。"""
    starts = sorted({s for s, _, _ in hits})
    by_end = sorted(range(len(hits)), key=lambda j: hits[j][1])
    by_start = list(range(len(hits)))          # hits 已按起点排序
    counts: dict[int, int] = {}
    inwin: set[int] = set()
    total = 0
    ai = ri = 0
    best_key, best_w = (-1, -1), starts[0]
    for w in starts:
        while ai < len(by_end) and hits[by_end[ai]][1] - limit <= w:
            j = by_end[ai]
            ai += 1
            if hits[j][0] >= w:                 # 起点已经在窗口左边的不加（它的有效区间已经过去了）
                counts[hits[j][2]] = counts.get(hits[j][2], 0) + 1
                total += 1
                inwin.add(j)
        while ri < len(by_start) and hits[by_start[ri]][0] < w:
            j = by_start[ri]
            ri += 1
            if j in inwin:
                inwin.discard(j)
                k = hits[j][2]
                counts[k] -= 1
                if not counts[k]:
                    del counts[k]
                total -= 1
        key = (len(counts), total)
        if key > best_key:
            best_key, best_w = key, w
    inside = [(s, e) for s, e, _ in hits if s >= best_w and e <= best_w + limit]
    return best_w, max(e for _, e in inside), best_key


def _grow(text: str, core_a: int, core_b: int, size: int) -> tuple[int, int]:
    """以 [core_a, core_b) 为必须保留的核心，扩成最多 size 长的窗口：前面留约三分之一给上文；
    两端只在窗口头尾各 15% 的范围内对齐到句子边界，并且从不缩进核心。"""
    if core_b - core_a >= size:
        return core_a, core_a + size
    spare = size - (core_b - core_a)
    a = max(0, core_a - spare // 3)
    b = min(len(text), a + size)
    a = max(0, min(a, b - size))
    edge = int(size * _EDGE)
    if a > 0:
        m = _SENT_END.search(text, a, min(core_a, a + edge))
        if m:
            a = m.end()
    if b < len(text):
        cut = None
        for m in _SENT_END.finditer(text, max(core_b, b - edge), b):
            cut = m.end()
        if cut:
            b = cut
    return a, b


def _head(text: str, limit: int) -> str:
    """开头 limit 字，尾部 15% 范围内有句末就停在句末。"""
    cut = None
    for m in _SENT_END.finditer(text, limit - int(limit * _EDGE), limit):
        cut = m.end()
    return text[: cut or limit].strip()
