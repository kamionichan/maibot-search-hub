"""发布准备：从开发仓库（Gitea）生成干净的 GitHub 发布仓库内容。不联网、不推送。

用法（在仓库根目录）：
  .venv/bin/python tools/release.py check            发布前检查：版本号、更新日志、白名单、隐私扫描、安装包
  .venv/bin/python tools/release.py stage <目录>      把发布文件复制到 <目录>（发布仓库的工作副本），
                                                     删除白名单以外的文件（保留 .git），再做一次隐私扫描
  .venv/bin/python tools/release.py notes [版本]      打印 CHANGELOG.md 里该版本的那一段（Release 说明用）

推送到 GitHub、打 Tag、建 Release 由人工完成（或以后的工作流），步骤见 docs/RELEASING.md。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_zip  # noqa: E402

ROOT = build_zip.ROOT

# 发布内容里不能出现的东西：内网地址、服务器路径、私人邮箱、测试实例目录、key 形态的字符串。
# 具体的私人域名、IP 段不写进公开仓库：放在开发仓库的 tools/private_patterns.txt（不在白名单里，不会发布），
# 每行一个正则；文件不存在时只用下面这些通用规则。
# 规则本身用字符类拆开写（如 [/]root），免得扫描这个文件时匹配到自己
GENERIC = [
    (r"[/]root[/]|[/]data[/]stacks[/]", "服务器路径"),
    (r"[.]local[/](testbed|research)", "测试实例或研究资料路径"),
    (r"\b(sk-[A-Za-z0-9]{20,}|tvly-[A-Za-z0-9]{10,}|keen_[A-Za-z0-9]{16,}|gh[op]_[A-Za-z0-9]{20,})", "key"),
]


def _patterns() -> list[tuple[re.Pattern, str]]:
    pats = [(re.compile(p, re.I), what) for p, what in GENERIC]
    extra = ROOT / "tools" / "private_patterns.txt"
    if extra.is_file():
        for line in extra.read_text(encoding="utf-8").splitlines():
            line = line.split("  #", 1)[0].strip()
            if line and not line.startswith("#"):
                pats.append((re.compile(line, re.I), "私人信息"))
    return pats
GENERATED = {".git", ".gitignore"}   # 发布仓库里由 stage 维护、不来自白名单的文件
PUBLIC_GITIGNORE = """.venv*/
__pycache__/
.pytest_cache/
*.py[cod]
dist/
config.toml
.env
.env.*
*.sqlite3
"""


def release_files() -> list[str]:
    s = build_zip.read_list(ROOT)
    return sorted(dict.fromkeys(s["zip"] + s["repo"]))


def scan(paths: dict[str, bytes]) -> list[str]:
    problems = []
    pats = _patterns()
    for name, data in paths.items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pat, what in pats:
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{name}:{line} 含{what}（{m.group(0)[:12]}…）")
    return problems


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        raise ValueError(f"CHANGELOG.md 里没有 {version} 这一段")
    return m.group(1).strip()


def check() -> list[str]:
    """返回问题列表；为空表示可以发布。"""
    problems: list[str] = []
    v = build_zip.version()
    m = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    if f"版本 {v}" not in (ROOT / "README.md").read_text(encoding="utf-8"):
        problems.append(f"README.md 顶部的版本号不是 {v}")
    try:
        changelog_section(v)
    except ValueError as e:
        problems.append(str(e))
    repo = (m.get("urls") or {}).get("repository", "")
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", repo):
        problems.append(f"manifest urls.repository 必须是公开 GitHub 地址（不带 .git）：{repo}")
    files = release_files()
    missing = [f for f in files if not (ROOT / f).is_file()]
    problems += [f"白名单里的 {f} 不存在" for f in missing]
    payload = {f: (ROOT / f).read_bytes() for f in files if f not in missing}
    problems += scan(payload)
    try:
        build_zip.check()
    except ValueError as e:
        problems.append(f"安装包检查：{e}")
    return problems


def stage(dest: Path) -> None:
    problems = check()
    if problems:
        raise SystemExit("发布检查没通过，未复制任何文件：\n  " + "\n  ".join(problems))
    dest.mkdir(parents=True, exist_ok=True)
    if dest.resolve() == ROOT.resolve() or ROOT.resolve() in dest.resolve().parents:
        raise SystemExit("目标目录不能是开发仓库本身或它的子目录")
    files = set(release_files())
    # 先删掉发布仓库里不在白名单的文件（.git 和 .gitignore 除外），保证“只有白名单里的东西”
    for p in sorted(dest.rglob("*"), reverse=True):
        rel = p.relative_to(dest).as_posix()
        if rel.split("/", 1)[0] in GENERATED:
            continue
        if p.is_file() and rel not in files:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    for f in sorted(files):
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, dest / f)
    (dest / ".gitignore").write_text(PUBLIC_GITIGNORE, encoding="utf-8")
    leftover = scan({p.relative_to(dest).as_posix(): p.read_bytes()
                     for p in dest.rglob("*") if p.is_file() and ".git" not in p.relative_to(dest).parts})
    if leftover:
        raise SystemExit("复制后的发布目录仍有私人信息：\n  " + "\n  ".join(leftover))
    print(f"已准备 {len(files)} 个文件到 {dest}（版本 {build_zip.version()}）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        ps = check()
        if ps:
            print("发布检查没通过：\n  " + "\n  ".join(ps))
            raise SystemExit(1)
        print(f"OK：版本 {build_zip.version()}，发布文件 {len(release_files())} 个")
    elif cmd == "stage" and len(sys.argv) == 3:
        stage(Path(sys.argv[2]).resolve())
    elif cmd == "notes":
        print(changelog_section(sys.argv[2] if len(sys.argv) > 2 else build_zip.version()))
    else:
        raise SystemExit(__doc__)
