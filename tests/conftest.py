"""测试按 Host 的方式加载插件：plugin.py 作为包模块，子模块搜索路径为插件目录（plugin_loader.py）。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hubplugin", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules["hubplugin"] = module
spec.loader.exec_module(module)
sys.modules["plugin"] = module
# 核心测试直接 import hub.*：与插件内的 hubplugin.hub.* 是两份模块对象，互不影响
sys.path.insert(0, str(ROOT))
