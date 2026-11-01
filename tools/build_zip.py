"""可复现的安装 ZIP：只打包白名单里的文件（沿用前身项目 maibot-picimagesearch 的做法）。

用法：.venv/bin/python tools/build_zip.py          → dist/maibot-search-hub-<版本>.zip 和 .sha256
     .venv/bin/python tools/build_zip.py --check  → 只校验，不写文件
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = ("__init__.py", "_manifest.json", "plugin.py", "README.md", "KEYS.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
HUB = ("__init__.py", "budget.py", "cards.py", "coexist.py", "config.py", "executor.py", "grants.py", "health.py",
       "image.py", "image_exec.py", "image_sources.py", "image_web.py", "jobs.py", "ledger.py", "merge.py",
       "net.py", "providers.py", "reader.py", "sources.py")
FILES = TOP + tuple(f"hub/{n}" for n in HUB)
FORBIDDEN = re.compile(r"(api[_-]?key|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.I)


def version(root: Path = ROOT) -> str:
    v = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(v, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", v):
        raise ValueError("manifest version 必须是严格的 X.Y.Z")
    return v


def check(root: Path = ROOT) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    for name in FILES:
        p = root / name
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"{name} 不是仓库里的普通文件")
        data = p.read_bytes()
        if name.endswith(".py") and FORBIDDEN.search(data.decode("utf-8")):
            raise ValueError(f"{name} 里疑似写死了密钥")
        payload[name] = data
    extra = sorted(str(p.relative_to(root)) for p in (root / "hub").glob("*.py") if f"hub/{p.name}" not in FILES)
    if extra:
        raise ValueError(f"hub/ 里有未列入白名单的模块：{extra}（新增模块要加进 HUB）")
    m = json.loads(payload["_manifest.json"])
    plugin_src = payload["plugin.py"].decode("utf-8")
    pv = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', plugin_src)
    if not pv or pv.group(1) != m["version"]:
        raise ValueError(f"plugin.py 的 PLUGIN_VERSION（{pv and pv.group(1)}）与 manifest（{m['version']}）不一致")
    return payload


def build(root: Path = ROOT) -> bytes:
    payload = check(root)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(FILES):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, payload[name], compresslevel=9)
    return out.getvalue()


if __name__ == "__main__":
    data = build()
    if "--check" in sys.argv:
        print(f"OK {len(FILES)} 个文件，{len(data)} 字节")
        sys.exit(0)
    dest = ROOT / "dist" / f"maibot-search-hub-{version()}.zip"
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    dest.with_suffix(".zip.sha256").write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    print(dest, digest)
