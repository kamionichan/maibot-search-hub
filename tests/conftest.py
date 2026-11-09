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


# ---------------- 0.6.1 审查：单测一律不联网 ----------------
# 审查者用禁网保护跑单测，发现有 124 次域名解析尝试（被拦下，没有真正联网）。这里把解析和连接都挡住：
# 只允许回环地址和测试里用的 .example/.test/.local/.invalid 域名（它们由 MockTransport 或假解析器处理，不会到这里）。
import socket as _socket

import pytest as _pytest

_real_getaddrinfo = _socket.getaddrinfo
_real_connect = _socket.socket.connect
_OK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _blocked_getaddrinfo(host, *a, **k):
    h = host.decode() if isinstance(host, bytes) else str(host or "")
    if h in _OK_HOSTS or h.startswith("127."):
        return _real_getaddrinfo(host, *a, **k)
    raise _socket.gaierror(f"单测禁止联网：解析 {h}")


def _blocked_connect(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if isinstance(host, str) and (host in _OK_HOSTS or host.startswith("127.")) or self.family == _socket.AF_UNIX:
        return _real_connect(self, addr, *a, **k)
    raise OSError(f"单测禁止联网：连接 {addr}")


@_pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(_socket, "getaddrinfo", _blocked_getaddrinfo)
    monkeypatch.setattr(_socket.socket, "connect", _blocked_connect)
    yield



# ---------------- D84：单测里不真的等限速间隔 ----------------
@_pytest.fixture(autouse=True)
def _instant_pacing(monkeypatch):
    """各引擎默认的限速（间隔几秒、随机停顿）在单测里改成 0；专门测限速的用例自己建 Pacer。"""
    import hub.pace as hp
    zero = dict(min_gap=0.0, jitter=0.0, per_hour=0, max_wait=5.0, step_range=(0.0, 0.0))
    monkeypatch.setattr(hp, "BROWSER_DEFAULT", dict(zero))
    monkeypatch.setattr(hp, "WEB_DEFAULT", dict(zero))
    hp.reset_for_tests()
    for mod in ("hub.image_web", "hub.image_more", "hubplugin.hub.image_web", "hubplugin.hub.image_more"):
        m = sys.modules.get(mod)
        if m is not None:
            for k in ("BROWSER_DEFAULT", "WEB_DEFAULT"):
                if hasattr(m, k):
                    monkeypatch.setattr(m, k, dict(zero))
    for m in (hp, sys.modules.get("hubplugin.hub.pace")):
        if m is not None:
            monkeypatch.setattr(m, "DOWNLOADS", m.SitePacer(gap=0.0, jitter=0.0))
    ph = sys.modules.get("hubplugin.hub.pace")
    if ph is not None:
        monkeypatch.setattr(ph, "BROWSER_DEFAULT", dict(zero))
        monkeypatch.setattr(ph, "WEB_DEFAULT", dict(zero))
        ph.reset_for_tests()
    yield
