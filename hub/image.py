"""图片输入：取图（只用授权会话）、清洗重编码、dHash（第 4 阶段，IMPLEMENTATION_PLAN 第 7、8 条）。

- 取图：`ctx.maisaka.context.resolve_image(stream_id=授权里的会话, source_id=消息编号, index)`；
  模型给的是消息编号或短别名，Host 负责展开（HOST_FACTS「第 4 阶段：图片相关」）。不接受模型给的会话 ID（D21）。
  也接受公开 http(s) 图片 URL：经过与 read_source 相同的 SSRF 校验后下载。
- 清洗：移植自旧插件 maibot-picimagesearch `image_prepare.py`（MIT，本项目作者自己的前身项目）：
  只接受 JPEG/PNG/WebP/GIF，取第一帧，应用 EXIF 方向，透明合成到白底，重新编码为不含元数据的 JPEG（最长边 1280，≤1MB）。
  外发给第三方的永远是清洗后的字节，不转发 QQ CDN 链接（可能带令牌）。
- dHash：64 位差值哈希，用于"同一张图"判断与账本里只存哈希不存图片（ARCHITECTURE L3）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import warnings
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from .net import Outcome, SourceError

MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 16_000_000
# 0.6.1（用户要求）：上传前统一压缩。以图搜服务自己会缩到 1000 像素左右再比对，原图大只浪费上传和等待时间
UPLOAD_SIDE = 1280
MAX_OUTPUT_BYTES = 1_000_000


class ImageInputError(Exception):
    """图片没取到或不能用：不是搜索失败，不应计入来源健康。"""


@dataclass
class PreparedImage:
    jpeg: bytes
    width: int
    height: int
    dhash: int
    sha1: str          # 清洗后字节的 sha1：用于缓存键（同一张图重复搜索）

    @property
    def b64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")


def prepare_image(data: bytes) -> PreparedImage:
    from PIL import Image, ImageOps  # Host 自带 Pillow

    if not isinstance(data, bytes) or not data:
        raise ImageInputError("图片数据为空")
    if len(data) > MAX_INPUT_BYTES:
        raise ImageInputError("图片超过 15 MB")
    clean = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            formats = ("JPEG", "PNG", "WEBP", "GIF")
            with BytesIO(data) as src, closing(Image.open(src, formats=formats)) as probe:
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageInputError("图片像素超过 1600 万")
                probe.verify()
            with BytesIO(data) as src, closing(Image.open(src, formats=formats)) as img:
                img.seek(0)
                ImageOps.exif_transpose(img, in_place=True)
                clean = Image.new("RGB", img.size, "white")
                with closing(img.convert("RGBA")) as rgba, closing(rgba.getchannel("A")) as alpha:
                    clean.paste(rgba, mask=alpha)
            dh = dhash_image(clean)
            if max(clean.size) > UPLOAD_SIDE:
                scale = UPLOAD_SIDE / max(clean.size)
                resized = clean.resize((max(1, round(clean.width * scale)), max(1, round(clean.height * scale))),
                                       Image.Resampling.LANCZOS)
                clean.close()
                clean = resized
            for step in range(7):
                for quality in (85, 75, 65, 50, 35):
                    with BytesIO() as out:
                        clean.save(out, format="JPEG", quality=quality, optimize=True)
                        result = out.getvalue()
                    if len(result) <= MAX_OUTPUT_BYTES:
                        return PreparedImage(result, clean.width, clean.height, dh, hashlib.sha1(result).hexdigest())
                size = (max(1, clean.width * 3 // 4), max(1, clean.height * 3 // 4))
                if step == 6 or size == clean.size:
                    break
                resized = clean.resize(size, Image.Resampling.LANCZOS)
                clean.close()
                clean = resized
            raise ImageInputError("图片无法压缩到 1 MB 以内")
    except ImageInputError:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageInputError("图片像素超过 1600 万") from None
    except (OSError, ValueError, SyntaxError, MemoryError):
        raise ImageInputError("图片无效或无法解码（只支持 JPEG/PNG/WebP/GIF）") from None
    finally:
        if clean is not None:
            clean.close()


def dhash_image(img: Any, size: int = 8) -> int:
    from PIL import Image

    with closing(img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)) as g:
        px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


CANDIDATE_MAX_PIXELS = 4_000_000   # 候选缩略图：远小于输入图上限（F12）


def dhash_bytes(data: bytes) -> int | None:
    """候选图的 dHash。F12（0.5.2）：与输入清洗同样只接受常见格式、拒绝解压炸弹，并限制像素；
    调用方应放到线程里执行（解码是 CPU 活，不能卡住事件循环）。"""
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with BytesIO(data) as src, closing(Image.open(src, formats=("JPEG", "PNG", "WEBP", "GIF"))) as img:
                if img.width * img.height > CANDIDATE_MAX_PIXELS:
                    return None
                img.seek(0)
                return dhash_image(img)
    except Exception:  # noqa: BLE001
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------- 取图 ----------------
async def fetch_message_image(ctx: Any, session_id: str, source_id: str, index: int) -> bytes:
    """只用授权里的会话 ID 读图（D21）。错误信息来自 Host，原样转述（不含敏感内容）。"""
    try:
        envelope = await asyncio.wait_for(
            ctx.maisaka.context.resolve_image(stream_id=session_id, source_id=source_id, index=index), timeout=10)
    except asyncio.TimeoutError:
        raise ImageInputError("读取消息图片超时") from None
    except Exception as exc:  # noqa: BLE001
        # 能力未在 _manifest.json 声明时也会走到这里（第 4 阶段实测踩过）：把原因带上，便于排查
        msg = str(exc)
        hint = "（插件未声明 maisaka.context.resolve_image 能力）" if "能力" in msg or "capabilit" in msg.lower() else ""
        raise ImageInputError(f"无法读取消息图片{hint}") from None
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        err = envelope.get("error") if isinstance(envelope, dict) else ""
        raise ImageInputError(str(err or "无法读取消息图片")[:120])
    encoded = (envelope.get("image") or {}).get("binary_data_base64") if isinstance(envelope.get("image"), dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise ImageInputError("消息图片没有数据")
    if len(encoded) > MAX_INPUT_BYTES * 4 // 3 + 16:
        raise ImageInputError("图片超过 15 MB")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ImageInputError("消息图片数据无效") from None


async def fetch_url_image(reader: Any, url: str, *, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    """公开图片 URL：复用内置读取器的 SSRF 校验（只连公网 IP、每跳重定向重新校验）。"""
    try:
        return await reader.fetch_bytes(url, max_bytes=max_bytes, accept=("image/",))
    except SourceError as err:
        raise ImageInputError(f"图片下载失败：{err.message}") from None


def outcome_is_input_error(err: Exception) -> bool:
    return isinstance(err, ImageInputError) or (isinstance(err, SourceError) and err.outcome is Outcome.NOT_SENT
                                                 and "图片" in err.message)
