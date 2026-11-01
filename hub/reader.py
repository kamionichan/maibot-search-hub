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

    async def fetch_bytes(self, url: str, *, max_bytes: int, accept: tuple[str, ...]) -> bytes:
        """按内容类型下载二进制（第 4 阶段：图片 URL 输入、dHash 候选图）。同样的 SSRF 防护。"""
        _, data, _ = await self._download(url, max_bytes, accept, "image/*;q=0.9,*/*;q=0.1", truncate=False)
        return data

    async def _download(self, url: str, max_bytes: int, accept: tuple[str, ...], accept_header: str,
                        truncate: bool = True) -> tuple[str, bytes, str]:
        current = url
        for hop in range(6):
            req_urls, host_header, sni = await validate_public_url(current, self._resolver)
            if not self._pin_ip:
                # 走代理：已确认目标是公网地址；用原域名发出，TLS 证书按域名校验
                req_urls, sni = [current.split("#", 1)[0]], None
            result = await self._get_once(req_urls, host_header, sni, current, hop, max_bytes, accept,
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


class ReadChain:
    """D3：Host web.fetch → fetch-url API → 内置读取器。前一级不可用（未配置/未安装）时才往下走。"""

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
                errors.append("Host 读取不可用")
        if self._use_fetch_url and now >= self._fetch_url_off_until:
            try:
                res = await self._ctx.api.call(self.FETCH_URL_API, url=url, on_exceed="truncate", return_image=False)
                if isinstance(res, dict) and res.get("success") and isinstance(res.get("result"), dict):
                    r = res["result"]
                    content = str(r.get("content") or "")
                    if r.get("success", True) and content.strip() and not content.startswith("抓取失败"):
                        return Page(url, str(r.get("title") or ""), content, "fetch_url")
                    errors.append("fetch-url 未取到正文")
                else:
                    err = str((res or {}).get("error") if isinstance(res, dict) else res)
                    if "未找到" in err or "不存在" in err or "解析失败" in err or "not found" in err.lower():
                        self._fetch_url_off_until = now + 1800
                    errors.append("fetch-url 不可用")
            except Exception:  # noqa: BLE001
                self._fetch_url_off_until = now + 1800
                errors.append("fetch-url 不可用")
        return await self._builtin.fetch(url)


# ---------------- 摘录 ----------------
def excerpt(text: str, focus: str, limit: int = 2200) -> tuple[str, bool]:
    """按 focus 关键词挑段落；没有 focus 时取开头。返回 (摘录, 是否截断)。"""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    terms = [t for t in re.split(r"[\s,，、；;]+", focus.lower()) if t] if focus else []
    if not terms:
        out, n = [], 0
        for p in paras:
            if n + len(p) > limit:
                break
            out.append(p)
            n += len(p) + 1
        return "\n".join(out) or text[:limit], True
    scored = sorted(range(len(paras)), key=lambda i: -sum(paras[i].lower().count(t) for t in terms))
    chosen, n = set(), 0
    for i in scored:
        if sum(paras[i].lower().count(t) for t in terms) == 0 and chosen:
            break
        for j in (i - 1, i, i + 1):  # 带上前后文
            if 0 <= j < len(paras) and j not in chosen and n + len(paras[j]) <= limit:
                chosen.add(j)
                n += len(paras[j]) + 1
        if n >= limit * 0.9:
            break
    out = []
    last = -2
    for j in sorted(chosen):
        if j != last + 1 and out:
            out.append("……")
        out.append(paras[j])
        last = j
    return "\n".join(out), True
