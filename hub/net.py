"""网络层：共享 httpx 客户端 + 错误四分类（D24、D36）+ 受限收包与脱敏（0.5.2）。

分类规则（D36）：
- not_sent：请求肯定没有到达服务端（DNS 失败、连接被拒、连接超时、TLS 握手失败、本地预检失败）。允许同一来源重发 1 次。
- rejected：服务端明确拒绝（4xx、厂商额度/限流错误）。不重发同一来源，可以换源。
- unknown：已发出但结果不明（读取超时、连接中断、5xx、响应无效）。**不重发同一来源**（D1），可以换源。

0.5.2（INDEPENDENT_REVIEW F01/F02/F03，作者复核见 docs/REVIEW_AUTHOR_CHECK.md）：
- 所有响应体经 read_limited()：读原始字节、自己逐步解压（0.5.6 起每一步的输出都有上限，并限制线缆字节），
  只接受一层 gzip/deflate；超限立即停止，不会先解开整个压缩炸弹再检查。
  错误响应体不读，或只读一小段（以图搜接口需要业务码时）。
- proxy 留空 = 直连，不继承环境变量里的代理（trust_env=False），与配置页说明一致。
- 上游返回的文字（错误信息等）在进入模型、账本、trace 前经 redact() 脱敏：已配置的 key 与 URL 里的凭据参数。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Iterator

import httpx

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ERROR_BODY_BYTES = 16 * 1024
USER_AGENT = "maibot-search-hub/0.5 (+https://github.com/kamionichan/maibot-search-hub)"


class Outcome(str, Enum):
    NOT_SENT = "not_sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class SourceError(Exception):
    outcome: Outcome
    message: str
    status: int | None = None
    retry_after: float | None = None
    quota_exhausted: bool = False
    auth_failed: bool = False
    unavailable: bool = False   # 来源本身未配置/未启用：长时间冷却，不重发
    budget: bool = False        # 0.5.3：被本次搜索的预算挡下（没有发出请求，也不是来源故障）
    key_gen: int = 0            # 0.5.6（S5）：发出请求时 key 的代次；换 key 后旧代次的失败不影响新 key 的冷却

    def __post_init__(self) -> None:
        # 0.5.6（B2）：先脱敏再截断，截断后的 key 残段也认不出来了
        self.message = clip(redact(str(self.message or "")), MESSAGE_MAX)
        super().__init__(self.message)   # args 也只保留脱敏后的文字

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.outcome.value}: {self.message}"


MESSAGE_MAX = 80


def clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class TooLarge(Exception):
    """解压后的响应体超过上限。"""


# ---------------- 脱敏（F03；0.5.6 B2 补全） ----------------
_SECRET_PARAM = re.compile(r"((?<![A-Za-z0-9])(?:api[_-]?key|apikey|access[_-]?token|token|secret|signature|sig|key)=)[^&\s#\"'<>]+", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=\-]+", re.I)
SECRET_GRACE = 600.0          # 换 key 后旧 key 继续脱敏的时长：覆盖仍在途的旧请求（任务时限最长 300 秒）
_current: frozenset[str] = frozenset()
_retired: dict[str, float] = {}    # 旧 key → 退役时间
_secrets: tuple[str, ...] = ()


def set_secrets(secrets: Iterable[str], *, now: float | None = None) -> None:
    """插件按当前配置登记所有 key（plugin._apply_config）。短于 6 个字符的不登记，避免误伤普通文字。

    0.5.6（B2）：不再整体覆盖。被换掉的旧 key 继续脱敏 SECRET_GRACE 秒，在途的旧请求回显旧 key 时也会被遮掉。"""
    global _current, _secrets
    t = time.monotonic() if now is None else now
    new = frozenset(s.strip() for s in secrets if s and len(s.strip()) >= 6)
    for old in _current - new:
        _retired[old] = t
    for k in [k for k, since in _retired.items() if t - since > SECRET_GRACE or k in new]:
        _retired.pop(k, None)
    _current = new
    _secrets = tuple(sorted(new | set(_retired), key=len, reverse=True))


def redact(text: str) -> str:
    if not text:
        return text
    for s in _secrets:
        text = text.replace(s, "***")
    text = _SECRET_PARAM.sub(r"\1***", text)
    return _BEARER.sub(r"\1***", text)


def redact_url(url: str) -> str:
    """展示用：去掉网址里的凭据参数（token=、key= 等）和已登记的 key。读取时仍用原网址。"""
    return redact(url)


class RedactingFilter(logging.Filter):
    """挂在 httpx/httpcore 日志上：管理员打开这些库的日志时，请求 URL 里的 key 也会被遮掉（F03）。
    SauceNAO 只接受 URL 参数里的 key（官方库 PicImageSearch 同样如此），所以从日志这一侧堵住。
    0.5.6（B2）：异常堆栈文字（exc_info / exc_text）也一并脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_FILTER = RedactingFilter()


def install_log_filter() -> None:
    # logger 上的过滤器不作用于子 logger：httpx 记在 "httpx" 本身，httpcore 记在各个子 logger 上
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                 "httpcore.proxy", "httpcore.socks"):
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)


# ---------------- 受限收包（F01；0.5.6 B1 重写） ----------------
# 0.5.2 的做法是把压缩数据分片喂给 httpx 的解码器，但 decode() 一次就会把一片完整解开，
# 解压峰值不受限（第二轮审查 B1：8 KB 的 gzip 峰值 21 MB，两层 gzip 峰值 53 MB）。
# 现在自己用 zlib 解压：每一步用 max_length 限定输出，只接受一层 gzip/deflate；请求也只声明这两种。
ACCEPT_ENCODING = "gzip, deflate"
WIRE_FACTOR = 4          # 线缆字节上限 = max_bytes × WIRE_FACTOR（压缩率很差的正常内容也够用）
_STEP = 64 * 1024        # 每一步最多解压出的字节数


class _Inflater:
    """一层 gzip/deflate，按步限定输出。deflate 按首字节判断是 zlib 封装还是裸 deflate（与 httpx 的处理一致）。
    gzip 只解第一个成员，之后的字节忽略（正常服务不会发多成员 gzip）。"""

    def __init__(self, encoding: str) -> None:
        self._gzip = encoding == "gzip"
        self._d: Any = zlib.decompressobj(zlib.MAX_WBITS | 16) if self._gzip else None

    def feed(self, data: bytes, room: int) -> Iterator[bytes]:
        """解压 data，每步最多 _STEP 字节；累计输出超过 room 时，多产出至多 1 字节后停止（调用方据此判断超限）。"""
        if self._d is None:
            # zlib 封装：CMF 低 4 位为 8（deflate），且 (CMF*256+FLG) % 31 == 0
            zlib_wrapped = len(data) >= 2 and (data[0] & 0x0F) == 8 and ((data[0] << 8) | data[1]) % 31 == 0
            self._d = zlib.decompressobj() if zlib_wrapped else zlib.decompressobj(-zlib.MAX_WBITS)
        d = self._d
        buf = data
        while buf and not d.eof:
            out = d.decompress(buf, max(1, min(_STEP, room + 1)))
            buf = d.unconsumed_tail
            if out:
                room -= len(out)
                yield out
                if room < 0:
                    return
            elif buf and not d.eof:
                continue   # 没有输出但还有输入：继续（例如 gzip 头部）
            else:
                break

    def flush(self, room: int) -> bytes:
        if self._d is None or self._d.eof:
            return b""
        tail = self._d.flush()[: max(0, room + 1)]
        if not self._d.eof:
            # 流结束了但压缩数据没有结束（被截断或根本不是有效压缩数据）
            raise zlib.error("incomplete compressed stream")
        return tail


async def read_limited(resp: httpx.Response, max_bytes: int, *, truncate: bool = False) -> bytes:
    """读响应体，解压后最多 max_bytes。超过时：truncate=True 截断返回，否则抛 TooLarge。

    - 只接受 identity、gzip、deflate 一层；其他或多层编码视为无效响应（请求里只声明了 gzip, deflate）。
    - 解压每一步的输出都有上限，峰值内存约为 max_bytes + 64 KiB。
    - 线缆字节最多 max_bytes × 4：压缩数据几乎不膨胀的异常流量也会被拦下。
    """
    if hasattr(resp, "_content"):
        # 响应已经被完整读入（非流式构造，例如测试里的 Response(json=...)）：内容已解压，只做长度检查
        body = resp.content
        if len(body) > max_bytes:
            if not truncate:
                raise TooLarge
            return body[:max_bytes]
        return body

    encs = [e.strip().lower() for e in resp.headers.get("content-encoding", "").split(",") if e.strip()]
    encs = [e for e in encs if e != "identity"]
    if len(encs) > 1 or (encs and encs[0] not in ("gzip", "x-gzip", "deflate")):
        raise SourceError(Outcome.UNKNOWN, f"不支持的内容编码：{', '.join(encs)}")
    inflater = _Inflater("gzip" if encs and encs[0] in ("gzip", "x-gzip") else "deflate") if encs else None

    out = bytearray()
    wire = 0
    wire_cap = max_bytes * WIRE_FACTOR + 64 * 1024

    def full() -> bytes:
        if not truncate:
            raise TooLarge
        return bytes(out[:max_bytes])

    async for raw in resp.aiter_raw():
        wire += len(raw)
        if wire > wire_cap:
            return full()
        if inflater is None:
            out.extend(raw[: max_bytes + 1 - len(out)])
        else:
            try:
                for piece in inflater.feed(raw, max_bytes - len(out)):
                    out.extend(piece)
            except zlib.error:
                raise SourceError(Outcome.UNKNOWN, "响应解压失败") from None
        if len(out) > max_bytes:
            return full()
    if inflater is not None:
        try:
            out.extend(inflater.flush(max_bytes - len(out)))
        except zlib.error:
            raise SourceError(Outcome.UNKNOWN, "响应解压失败") from None
        if len(out) > max_bytes:
            return full()
    return bytes(out)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def classify_status(resp: httpx.Response, quota_codes: frozenset[int] = frozenset()) -> SourceError:
    """quota_codes：厂商把"额度用尽"放在非标准状态码上（例如博查用 403 表示余额不足）。"""
    code = resp.status_code
    if 400 <= code < 500:
        quota = code in (402, 432, 433) or code in quota_codes
        return SourceError(
            Outcome.REJECTED, "额度用尽或余额不足" if quota else f"HTTP {code}", status=code,
            retry_after=_retry_after(resp), quota_exhausted=quota,
            auth_failed=code in (401, 403) and not quota,
        )
    return SourceError(Outcome.UNKNOWN, f"HTTP {code}", status=code)


def classify_exception(exc: BaseException) -> SourceError:
    """把 httpx 异常归类。无法识别的一律视为 unknown（宁可少重发）。只用异常类型名，不带 URL。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)):
        return SourceError(Outcome.NOT_SENT, type(exc).__name__)
    if isinstance(exc, httpx.PoolTimeout):
        return SourceError(Outcome.NOT_SENT, "PoolTimeout")
    return SourceError(Outcome.UNKNOWN, type(exc).__name__)


class Http:
    """每个插件实例一个共享客户端；配置热更新时整体替换（旧任务继续用旧实例）。"""

    def __init__(self, proxy: str = "", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.proxy = proxy
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
            "follow_redirects": False,
            "trust_env": False,          # F02：留空就是直连
            "headers": {"User-Agent": USER_AGENT, "Accept-Encoding": ACCEPT_ENCODING},
        }
        if transport is not None:
            kwargs["transport"] = transport
        elif proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_json(self, url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None,
                        read_timeout: float = 20.0, quota_codes: frozenset[int] = frozenset()) -> Any:
        return await self._request("POST", url, json_body=body, headers=headers, read_timeout=read_timeout,
                                   quota_codes=quota_codes)

    async def get_json(self, url: str, params: dict[str, Any], *, headers: dict[str, str] | None = None,
                       read_timeout: float = 20.0, quota_codes: frozenset[int] = frozenset()) -> Any:
        return await self._request("GET", url, params=params, headers=headers, read_timeout=read_timeout,
                                   quota_codes=quota_codes)

    async def _request(self, method: str, url: str, *, json_body: dict[str, Any] | None = None,
                       params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                       read_timeout: float = 20.0, quota_codes: frozenset[int] = frozenset()) -> Any:
        try:
            async with self._client.stream(
                method, url, json=json_body, params=params, headers=headers or {},
                timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=10.0, pool=5.0),
            ) as resp:
                if not resp.is_success:
                    raise classify_status(resp, quota_codes)   # 错误体不读：状态码足够分类
                data = await read_limited(resp, MAX_RESPONSE_BYTES)
        except SourceError:
            raise
        except TooLarge:
            raise SourceError(Outcome.UNKNOWN, "响应超过大小上限") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise SourceError(Outcome.UNKNOWN, "无效 JSON") from None

    async def post_raw(self, url: str, *, content: bytes | None = None, json_body: Any = None, files: Any = None,
                       params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                       read_timeout: float = 30.0, max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[int, Any]:
        """以图搜接口：各家错误语义不同（AnimeTrace 用 HTTP 500 + 业务码），返回 (状态码, 解析后的 JSON 或 None)。
        成功体按 max_bytes 受限读取；错误体最多读 ERROR_BODY_BYTES。"""
        try:
            async with self._client.stream(
                "POST", url, content=content, json=json_body, files=files, params=params, headers=headers or {},
                timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=20.0, pool=5.0),
            ) as resp:
                ok = resp.is_success
                body = await read_limited(resp, max_bytes if ok else ERROR_BODY_BYTES, truncate=not ok)
                status = resp.status_code
        except TooLarge:
            raise SourceError(Outcome.UNKNOWN, "响应超过大小上限") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc) from None
        try:
            return status, (json.loads(body) if body else None)
        except (ValueError, UnicodeError):
            return status, None
