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
    (tmp_path / "r" / "hub" / "stray.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="白名单"):
        build_zip.check(tmp_path / "r")
