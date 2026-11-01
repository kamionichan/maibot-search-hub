import httpx
import pytest

from hub.net import Outcome, SourceError
from hub.reader import BuiltinReader, excerpt, html_to_text, validate_public_url


def resolver(table):
    async def r(host, port):
        return table[host]
    return r


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "100.64.0.1", "0.0.0.0"])
async def test_private_addresses_rejected(ip):
    with pytest.raises(SourceError) as ei:
        await validate_public_url("http://evil.test/x", resolver({"evil.test": [ip]}))
    assert "公网" in ei.value.message


async def test_mixed_public_private_rejected_and_literal_ip_rejected():
    with pytest.raises(SourceError):
        await validate_public_url("https://a.test/", resolver({"a.test": ["93.184.216.34", "10.0.0.1"]}))
    with pytest.raises(SourceError):
        await validate_public_url("http://127.0.0.1:8001/api", resolver({}))
    with pytest.raises(SourceError):
        await validate_public_url("ftp://a.test/", resolver({}))
    with pytest.raises(SourceError):
        await validate_public_url("http://user:pw@a.test/", resolver({}))


async def test_connects_to_validated_ip_with_host_and_sni():
    urls, host, sni = await validate_public_url("https://a.test:8443/p?q=1",
                                                resolver({"a.test": ["2606:2800::1", "93.184.216.34"]}))
    assert urls == ["https://93.184.216.34:8443/p?q=1", "https://[2606:2800::1]:8443/p?q=1"]
    assert host == "a.test:8443" and sni == "a.test"


async def test_falls_back_to_next_address_when_connect_fails():
    def handler(req):
        if req.url.host == "93.184.216.34":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, headers={"content-type": "text/plain"}, content="ok".encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = BuiltinReader(client, resolver({"a.test": ["93.184.216.34", "93.184.216.35"]}))
    assert (await r.fetch("http://a.test/")).text == "ok"
    await client.aclose()


async def test_redirect_to_private_is_rejected():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(302, headers={"location": "http://internal.test/admin"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = BuiltinReader(client, resolver({"pub.test": ["93.184.216.34"], "internal.test": ["192.168.0.2"]}))
    with pytest.raises(SourceError) as ei:
        await r.fetch("http://pub.test/start")
    assert "公网" in ei.value.message and seen == ["93.184.216.34"]
    await client.aclose()


async def test_reads_html_main_text_and_rejects_binary():
    page = ("<html><head><title>原神 7.1 公告</title><script>evil()</script></head><body><nav>菜单</nav>"
            "<article><h1>7.1 版本</h1><p>" + "新角色 薇丝娜 上线。" * 60 + "</p></article><footer>版权</footer></body></html>")

    def handler(req):
        if req.url.path == "/bin":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=page.encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = BuiltinReader(client, resolver({"pub.test": ["93.184.216.34"]}))
    p = await r.fetch("http://pub.test/a")
    assert p.title == "原神 7.1 公告" and "薇丝娜" in p.text and "evil" not in p.text and "菜单" not in p.text
    with pytest.raises(SourceError) as ei:
        await r.fetch("http://pub.test/bin")
    assert ei.value.outcome is Outcome.REJECTED
    await client.aclose()


def test_excerpt_focus_and_limit():
    text = "\n".join(f"第{i}段 普通内容" * 5 for i in range(200)) + "\n关键：上线日期是 9 月 23 日"
    out, cut = excerpt(text, "上线日期", 600)
    assert cut and "上线日期" in out and len(out) <= 700
    out2, cut2 = excerpt("短文本", "", 600)
    assert out2 == "短文本" and not cut2
    t, body = html_to_text("<p>a&amp;b</p><p>c</p>")
    assert body == "a&b\nc"
