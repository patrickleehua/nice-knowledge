"""网页正文抓取单测:SSRF 校验 + 流式抓取 + trafilatura 提取 + 批量降级。

全程不碰真实网络:DNS 由 autouse fixture 把 socket.getaddrinfo 换成查表实现(表外域名
一律 gaierror,等于物理隔绝真实解析),HTTP 由 httpx.MockTransport 接管——用真 httpx
客户端 + 假传输层,重定向/流式/content-type 这些行为才和线上一致,不会被手搓 stub 骗过。
"""

import socket

import httpx
import pytest

from nicekit.capabilities.websearch.fetch import check_url_safety, fetch_page, fetch_pages

# 假 DNS 表:公网 IP 用真实公网段(避开 203.0.113.0/24 这类 Python 判定为 private 的文档段)
_FAKE_DNS = {
    "example.com": "93.184.216.34",
    "www.example.com": "93.184.216.34",
    "news.example.com": "104.18.32.7",
    "cdn.example.com": "104.18.32.7",
    "intranet.example.com": "10.1.2.3",  # 名字像公网,记录指内网——典型 SSRF 伪装
    "roundrobin.example.com": ["93.184.216.34", "192.168.7.7"],  # 多 A 记录轮询绕过
    "v6.example.com": "::ffff:127.0.0.1",  # IPv4-mapped 包装环回
}


def _fake_getaddrinfo(host, port=0, *_args, **_kwargs):
    """查表 DNS;表外主机直接 gaierror,保证任何用例都不会真正发起解析。"""
    raw = _FAKE_DNS.get(host)
    if raw is None:
        raise socket.gaierror(f"[Errno 11001] getaddrinfo failed: {host}")
    addresses = raw if isinstance(raw, list) else [raw]
    infos = []
    for addr in addresses:
        if ":" in addr:
            infos.append(
                (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port, 0, 0))
            )
        else:
            infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port)))
    return infos


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


ARTICLE_HTML = """<html lang="zh"><head>
<title>2026 年泰国落地签最新材料清单 - 示例旅游网</title>
<meta property="article:published_time" content="2026-03-05T10:00:00+08:00">
</head><body>
<nav id="topnav"><a href="/">首页</a><a href="/visa">签证</a><a href="/about">关于我们</a></nav>
<aside class="sidebar"><h3>热门推荐</h3><ul><li>广告位一</li><li>广告位二</li></ul></aside>
<article>
<h1>2026 年泰国落地签最新材料清单</h1>
<p>自 2026 年 1 月起,泰国移民局对落地签所需材料做了一次调整,出行前请按下列清单逐项
准备,避免在机场被要求补件而耽误行程。</p>
<p>第一,护照原件,有效期需自入境之日起不少于六个月,且至少留有两页连续空白签证页。</p>
<p>第二,近六个月内拍摄的白底彩色照片两张,规格为 4x6 厘米,不得佩戴眼镜与帽子。</p>
<p>第三,已确认的返程或续程机票行程单,离境日期须在入境之日起十五天之内。</p>
<table><tr><th>项目</th><th>标准</th></tr>
<tr><td>签证费</td><td>2000 泰铢</td></tr>
<tr><td>停留期</td><td>不超过 15 天</td></tr></table>
</article>
<script>var _ad = {track: 1}; console.log("广告脚本噪声");</script>
<footer>版权所有 示例旅游网</footer>
</body></html>"""

# 只有导航壳、没有任何正文段落的页面
EMPTY_HTML = (
    "<html><head><title>空页</title></head><body>"
    '<div class="gallery"><img src="a.jpg"></div></body></html>'
)


def _html_response(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=body.encode("utf-8"),
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


# --------------------------------------------------------------------------- #
# check_url_safety
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com:70/_x",
        "data:text/html,<h1>x</h1>",
        "javascript:alert(1)",
    ],
)
def test_check_url_safety_rejects_non_http_schemes(url: str) -> None:
    reason = check_url_safety(url)
    assert reason is not None
    assert "协议" in reason


def test_check_url_safety_requires_hostname() -> None:
    assert check_url_safety("http:///no-host") is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://172.16.0.5/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/x",
        "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped 包装
        "http://100.100.100.200/latest/meta-data/",  # 阿里云元数据(CGNAT 段)
    ],
)
def test_check_url_safety_rejects_internal_ip_literals(url: str) -> None:
    reason = check_url_safety(url)
    # 169.254.169.254 会先命中域名黑名单,其余走 IP 段判定——两条路径都算挡住
    assert reason is not None
    assert "SSRF" in reason or "禁止访问名单" in reason


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/",
        "http://api.localhost/",
        "http://metadata.google.internal/",
        "http://foo.internal/",
        "http://printer.local/",
    ],
)
def test_check_url_safety_rejects_blacklisted_hosts(url: str) -> None:
    reason = check_url_safety(url)
    assert reason is not None
    assert "禁止访问名单" in reason


def test_check_url_safety_rejects_host_resolving_to_private_ip() -> None:
    """域名看着像公网,A 记录指内网——这是 SSRF 最常见的伪装。"""
    reason = check_url_safety("https://intranet.example.com/admin")
    assert reason is not None
    assert "内网地址" in reason


def test_check_url_safety_rejects_when_any_record_is_private() -> None:
    """多 A 记录必须逐条判定:只看第一条会被轮询记录绕过。"""
    reason = check_url_safety("https://roundrobin.example.com/x")
    assert reason is not None
    assert "192.168.7.7" in reason


def test_check_url_safety_rejects_ipv4_mapped_from_dns() -> None:
    reason = check_url_safety("https://v6.example.com/x")
    assert reason is not None
    assert "环回地址" in reason


@pytest.mark.parametrize("port", [22, 23, 25, 3306, 5432, 6379, 9200, 11211, 27017])
def test_check_url_safety_rejects_sensitive_ports(port: int) -> None:
    reason = check_url_safety(f"http://example.com:{port}/")
    assert reason is not None
    assert "高危服务端口" in reason


def test_check_url_safety_allows_public_https() -> None:
    assert check_url_safety("https://example.com/a") is None
    assert check_url_safety("http://example.com:8443/a?b=1#c") is None


def test_check_url_safety_returns_reason_on_dns_failure() -> None:
    """解析失败必须返回原因而不是抛异常,否则会把 agent loop 打断。"""
    reason = check_url_safety("https://not-in-dns-table.test/x")
    assert reason is not None
    assert "解析失败" in reason


# --------------------------------------------------------------------------- #
# fetch_page:安全拦截
# --------------------------------------------------------------------------- #
async def test_fetch_page_blocks_unsafe_url_without_network() -> None:
    page = await fetch_page("file:///etc/passwd")
    assert page.status == "blocked"
    assert page.content == ""


async def test_fetch_page_blocks_redirect_to_loopback() -> None:
    """公网 URL 302 到 127.0.0.1 是绕过首跳校验的经典手法,必须逐跳复核挡住。"""
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/start", client=client)

    assert page.status == "blocked"
    assert "重定向目标被拒绝" in (page.error or "")
    assert hops == ["https://example.com/start"]  # 内网那跳压根没发出去


async def test_fetch_page_follows_safe_redirect_and_records_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://news.example.com/new"})
        return _html_response(ARTICLE_HTML)

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/old", client=client)

    assert page.status == "ok"
    assert page.url == "https://example.com/old"
    assert page.final_url == "https://news.example.com/new"
    assert page.domain == "news.example.com"


async def test_fetch_page_stops_after_max_redirect_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/loop", client=client)

    assert page.status == "unavailable"
    assert "重定向超过" in (page.error or "")


async def test_fetch_page_forces_manual_redirect_even_if_client_auto_follows() -> None:
    """调用方传入的 client 开了 follow_redirects 也不能绕过逐跳校验。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://192.168.0.1/x"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        page = await fetch_page("https://example.com/start", client=client)

    assert page.status == "blocked"


# --------------------------------------------------------------------------- #
# fetch_page:内容处理
# --------------------------------------------------------------------------- #
async def test_fetch_page_rejects_non_text_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 binary"
        )

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/doc.pdf", client=client)

    assert page.status == "blocked"
    assert "application/pdf" in (page.error or "")
    assert page.content == ""


async def test_fetch_page_aborts_oversized_body() -> None:
    """超限必须当场断流,而不是读完再截——否则内存已经被打爆了。"""
    yielded: list[int] = []

    async def _body():
        for i in range(200):
            yielded.append(i)
            yield b"x" * (64 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain; charset=utf-8"}, content=_body()
        )

    async with _client(handler) as client:
        page = await fetch_page(
            "https://example.com/huge.txt", client=client, max_bytes=128 * 1024, max_chars=500
        )

    assert page.status == "ok"
    assert len(yielded) <= 3  # 只读了刚过阈值的那几块就断开
    assert page.truncated is True


async def test_fetch_page_extracts_main_content_and_drops_noise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _html_response(ARTICLE_HTML)

    async with _client(handler) as client:
        page = await fetch_page("https://www.example.com/visa/thailand", client=client)

    assert page.status == "ok"
    assert page.error is None
    assert "护照原件" in page.content
    assert "2000 泰铢" in page.content  # include_tables 生效
    assert "广告位" not in page.content  # 侧栏被剔除
    assert "console.log" not in page.content  # script 被剔除
    assert "关于我们" not in page.content  # 导航被剔除
    assert "版权所有" not in page.content  # 页脚被剔除
    assert page.title
    assert page.published_at == "2026-03-05"
    assert page.domain == "example.com"  # www. 前缀已去掉
    assert page.truncated is False
    assert page.source_tier == "unknown"  # 由 service 层填,本模块不碰
    assert page.ref is None


async def test_fetch_page_returns_empty_when_no_main_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _html_response(EMPTY_HTML)

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/gallery", client=client)

    assert page.status == "empty"
    assert page.content == ""


async def test_fetch_page_truncates_at_max_chars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _html_response(ARTICLE_HTML)

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/visa", client=client, max_chars=60)

    assert page.status == "ok"
    assert page.truncated is True
    assert page.content.endswith("…(正文已截断)")
    assert len(page.content) == 60 + len("\n\n…(正文已截断)")


async def test_fetch_page_handles_plain_text_without_extraction() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content="签证材料清单纯文本".encode(),
        )

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/a.txt", client=client)

    assert page.status == "ok"
    assert page.content == "签证材料清单纯文本"


async def test_fetch_page_returns_unavailable_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope")

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/missing", client=client)

    assert page.status == "unavailable"
    assert "404" in (page.error or "")


async def test_fetch_page_swallows_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝")

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/x", client=client)

    assert page.status == "unavailable"
    assert "连接被拒绝" in (page.error or "")


async def test_fetch_page_swallows_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    async with _client(handler) as client:
        page = await fetch_page("https://example.com/slow", client=client)

    assert page.status == "unavailable"
    assert page.error


# --------------------------------------------------------------------------- #
# fetch_pages
# --------------------------------------------------------------------------- #
async def test_fetch_pages_dedups_keeps_order_and_isolates_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/boom":
            raise httpx.ConnectError("连接被拒绝")
        if path == "/empty":
            return _html_response(EMPTY_HTML)
        return _html_response(ARTICLE_HTML)

    urls = [
        "https://example.com/ok",
        "https://example.com/boom",
        "https://example.com/ok",  # 重复
        "http://127.0.0.1/admin",  # 安全拦截
        "https://example.com/empty",
        "   ",  # 空白项丢弃
    ]
    async with _client(handler) as client:
        pages = await fetch_pages(urls, client=client, concurrency=2)

    assert [p.url for p in pages] == [
        "https://example.com/ok",
        "https://example.com/boom",
        "http://127.0.0.1/admin",
        "https://example.com/empty",
    ]
    assert [p.status for p in pages] == ["ok", "unavailable", "blocked", "empty"]
    assert "护照原件" in pages[0].content  # 同批里的失败项没影响成功项


async def test_fetch_pages_empty_input() -> None:
    assert await fetch_pages([]) == []
    assert await fetch_pages(["", "  "]) == []
