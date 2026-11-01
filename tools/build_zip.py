"""可复现的安装 ZIP：只打包 tools/release_files.txt 的 [zip] 段列出的文件。

用法：.venv/bin/python tools/build_zip.py          → dist/maibot-search-hub-<版本>.zip 和 .sha256
     .venv/bin/python tools/build_zip.py --check  → 只校验，不写文件
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST_FILE = "tools/release_files.txt"
FORBIDDEN = re.compile(r"(api[_-]?key|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.I)


def read_list(root: Path = ROOT) -> dict[str, list[str]]:
    """解析白名单：返回 {"zip": [...], "repo": [...]}，通配已展开、按字母排序。"""
    sections: dict[str, list[str]] = {"zip": [], "repo": []}
    cur = None
    for raw in (root / LIST_FILE).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            cur = m.group(1)
            if cur not in sections:
                raise ValueError(f"{LIST_FILE}：未知的段 [{cur}]")
            continue
        if cur is None:
            raise ValueError(f"{LIST_FILE}：{line} 不在任何段里")
        if ".." in Path(line).parts or line.startswith("/"):
            raise ValueError(f"{LIST_FILE}：{line} 不能指向仓库外")
        if line.endswith("/*.py"):
            d = line[: -len("/*.py")]
            sections[cur] += sorted(f"{d}/{p.name}" for p in (root / d).glob("*.py"))
        else:
            sections[cur].append(line)
    for k in sections:
        sections[k] = sorted(dict.fromkeys(sections[k]))
    return sections


def git_tracked(root: Path = ROOT) -> set[str] | None:
    """git 跟踪的文件；不是 git 仓库（例如从发布 ZIP 解出来的目录）时返回 None，不做这项检查。"""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return {x for x in out.stdout.decode("utf-8").split("\0") if x}


def check_file(root: Path, name: str, tracked: set[str] | None) -> None:
    """0.5.6（B3）：[zip] 和 [repo] 两段都用这一项检查：必须是仓库里的普通文件、不是符号链接、
    父目录也不是符号链接，并且已被 git 跟踪（工作区里随手放的文件不会被 *.py 通配带进发布）。"""
    p = root / name
    rp = root.resolve()
    if p.is_symlink() or any(q.is_symlink() for q in p.parents if q != rp and rp in q.resolve().parents):
        raise ValueError(f"{name} 是符号链接或在符号链接目录里")
    if not p.is_file() or not p.resolve().is_relative_to(rp):
        raise ValueError(f"{name} 不是仓库里的普通文件")
    if tracked is not None and name not in tracked:
        raise ValueError(f"{name} 没有被 git 跟踪（先提交，或从白名单/目录里移走）")


def _files(root: Path) -> tuple[str, ...]:
    return tuple(read_list(root)["zip"])


FILES = _files(ROOT)


def version(root: Path = ROOT) -> str:
    v = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(v, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", v):
        raise ValueError("manifest version 必须是严格的 X.Y.Z")
    return v


def check(root: Path = ROOT) -> dict[str, bytes]:
    files = _files(root)
    tracked = git_tracked(root)
    payload: dict[str, bytes] = {}
    for name in files:
        check_file(root, name, tracked)
        data = (root / name).read_bytes()
        if name.endswith(".py") and FORBIDDEN.search(data.decode("utf-8")):
            raise ValueError(f"{name} 里疑似写死了密钥")
        payload[name] = data
    if "hub/__init__.py" not in files:
        raise ValueError(f"白名单缺少 hub/*.py（{LIST_FILE} 的 [zip] 段）")
    m = json.loads(payload["_manifest.json"])
    plugin_src = payload["plugin.py"].decode("utf-8")
    pv = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', plugin_src)
    if not pv or pv.group(1) != m["version"]:
        raise ValueError(f"plugin.py 的 PLUGIN_VERSION（{pv and pv.group(1)}）与 manifest（{m['version']}）不一致")
    icon = ((m.get("display") or {}).get("icon") or {})
    if icon.get("type") == "local" and icon.get("value") not in payload:
        raise ValueError(f"manifest 声明的图标 {icon.get('value')} 不在安装包里")
    changelog = m.get("changelog")
    if changelog and not changelog.startswith(("http://", "https://")) and changelog not in payload:
        raise ValueError(f"manifest 声明的更新日志 {changelog} 不在安装包里")
    return payload


def build(root: Path = ROOT) -> bytes:
    payload = check(root)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(payload):
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
