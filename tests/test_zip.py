"""安装 ZIP：白名单完整、能在全新目录里作为独立包导入并创建插件。"""

import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_zip  # noqa: E402


def test_zip_allowlist_and_import(tmp_path):
    data = build_zip.build()
    assert data == build_zip.build()                                  # 可复现
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == sorted(build_zip.FILES)
        assert not any(n.startswith((".local", "tests", "probes", "docs", "dist")) for n in z.namelist())
        z.extractall(tmp_path / "pkg")
    spec = importlib.util.spec_from_file_location("installed_hub", tmp_path / "pkg" / "__init__.py",
                                                  submodule_search_locations=[str(tmp_path / "pkg")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["installed_hub"] = mod
    try:
        spec.loader.exec_module(mod)
        from installed_hub.plugin import create_plugin, PLUGIN_VERSION
        p = create_plugin()
        assert PLUGIN_VERSION == build_zip.version() and p.config_widgets
        names = {c["name"] for c in p.get_components()}
        assert {"search", "read_source", "hub_context", "hub_grant", "search_status"} <= names
    finally:
        for k in [k for k in sys.modules if k == "installed_hub" or k.startswith("installed_hub.")]:
            del sys.modules[k]


def test_check_rejects_unlisted_module(tmp_path):
    import shutil
    shutil.copytree(ROOT, tmp_path / "r", ignore=shutil.ignore_patterns(".venv*", ".git", "__pycache__", "dist"))
    # 0.5.5：hub/*.py 通配会自动收进新模块；改为检查“白名单里的文件缺失”和“声明的图标不在包里”
    (tmp_path / "r" / "KEYS.md").unlink()
    with pytest.raises(ValueError, match="不是仓库里的普通文件"):
        build_zip.check(tmp_path / "r")


def test_zip_contains_icon_and_changelog():
    import json
    m = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert m["display"]["icon"]["value"] in build_zip.FILES
    assert m["changelog"] in build_zip.FILES
    assert not any(f.startswith(("docs/", "probes/", ".gitea/")) or f in ("AGENTS.md", "tools/private_patterns.txt")
                   for f in build_zip.read_list(ROOT)["zip"] + build_zip.read_list(ROOT)["repo"])


def test_release_check_and_stage(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    assert release.check() == []
    dest = tmp_path / "public"
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / "keep").write_text("x", encoding="utf-8")
    (dest / "old_file.txt").write_text("stale", encoding="utf-8")
    release.stage(dest)
    names = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert "old_file.txt" not in names and ".git/keep" in names and ".gitignore" in names
    assert set(release.release_files()) <= set(names)
    assert release.changelog_section(build_zip.version())


def test_release_scan_catches_private_info():
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    sample = ("see " + "/ro" + "ot/workspace/x and " + ".lo" + "cal/testbed/y").encode()
    found = release.scan({"a.md": sample})
    assert len(found) == 2


def test_r61_22_stage_does_not_follow_gitignore_symlink(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    dest = tmp_path / "public"
    (dest / ".git").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    (dest / ".gitignore").symlink_to(outside)
    release.stage(dest)
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not (dest / ".gitignore").is_symlink() and (dest / ".gitignore").read_text(encoding="utf-8").startswith(".venv")
