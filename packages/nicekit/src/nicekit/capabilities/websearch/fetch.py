"""网页正文抓取(web_fetch 工具的底座):URL 安全校验 → 流式拉取 → 正文转 markdown。

## 威胁模型

待抓取的 URL 由模型给出,而模型上下文里混着用户输入和第三方网页文本,所以 URL 必须
当**完全不可信的输入**处理——否则 agent 就成了打内网的跳板(SSRF)。本模块的防护面:

1. **scheme 白名单**:只放 http/https。挡掉 `file://` 读本地文件、`ftp://`/`gopher://`
   做协议走私、`data:` 塞伪造正文。
2. **地址族封禁**:把主机名解析出的**全部** A/AAAA 记录逐条判定,任一条命中私网/环回/
   链路本地/保留/组播/未指定即整体拒绝(只看第一条会被多 A 记录轮询绕过)。IPv4-mapped、
   6to4、Teredo 三种 IPv6 包装形态先还原成 IPv4 再判,防止 `::ffff:127.0.0.1` 这类写法绕过。
   另外补一条 CGNAT 段(100.64.0.0/10)——Python 的 `is_private` 不含它,但阿里云元数据
   100.100.100.200 正落在里面。
3. **端口封禁**:SSH/SMTP/数据库/缓存等高危端口即使在公网 IP 上也拒,避免 agent 被当成
   端口探测器或被用来对内网服务做协议走私。
4. **域名黑名单**:云厂商元数据入口(metadata.google.internal 等)与本机别名,在 IP 段
   之外再显式挡一层,防的是 DNS 记录被改成公网 IP 后再改回来的时间差。
5. **逐跳复核重定向**:**不开** httpx 的 follow_redirects,自己跟且每一跳都重跑
   `check_url_safety`。这才挡得住"公网 URL 302 到 127.0.0.1",也把 DNS rebinding
   (校验时解析到公网、连接时解析到内网)的时间窗压到最小。
6. **响应体限流**:流式读、超过 max_bytes 立刻断开连接,不给超大响应打爆内存的机会。

## 降级约定

同 websearch provider:任何网络/超时/解析/编码异常都就地兜住,返回 status=unavailable
的 FetchedPage 并如实透传 error,**绝不抛异常阻塞 agent loop**。

`source_tier` / `ref` 两个治理字段一律留默认值,由 service 层统一填充——本模块刻意不引用
域名分级策略,保持"抓取"与"来源治理"解耦。
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from nicekit.capabilities.websearch.base import FetchedPage

_ALLOWED_SCHEMES = {"http", "https"}

# 高危端口:即使解析到公网 IP 也拒,避免 agent 沦为端口探测/协议走私的跳板
_BLOCKED_PORTS = {22, 23, 25, 3306, 5432, 6379, 9200, 11211, 27017}

# 本机别名与云元数据入口的域名形态(IP 段检查已覆盖大部分,这里再挡一层防 DNS 漂移)
_BLOCKED_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "169.254.169.254",
}

# 私有用途顶级域:.internal 已由 ICANN 保留给内网、.local 是 mDNS、.localhost 是本机
_BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local")

# Python 的 is_private 不含 CGNAT 段,但阿里云元数据 100.100.100.200 就在其中
_EXTRA_BLOCKED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)

# 重定向最多跟 3 跳,超了就放弃——正常站点不会绕这么多,绕得多的多半是在兜圈子
_MAX_REDIRECTS = 3
_REDIRECT_CODES = {301, 302, 303, 307, 308}

_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain",)

# 伪装成常见桌面 Chrome:不少站点对无 UA 或脚本 UA 直接返回 403/验证页
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TRUNCATED_MARK = "\n\n…(正文已截断)"
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --------------------------------------------------------------------------- #
# SSRF 校验
# --------------------------------------------------------------------------- #
def _is_ip_literal(host: str) -> bool:
    """主机名是否已经是 IP 字面量(是的话不必也不该再过 DNS)。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _reject_ip(raw: str) -> str | None:
    """判定单个 IP 是否落在禁止访问的地址段;放行返回 None,否则返回中文原因。"""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        # 解析器给了个认不出来的地址,宁可错杀:未知形态没法证明它指向公网
        return f"目标地址 {raw} 无法识别,已按 SSRF 策略拒绝"

    # IPv6 对 IPv4 的三种包装形态先还原,否则 ::ffff:127.0.0.1 / 2002:7f00:1:: 能绕过判定
    if isinstance(ip, ipaddress.IPv6Address):
        unwrapped = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if unwrapped is not None:
            ip = unwrapped

    # 先判具体类别再判 is_private,报错信息才说得清到底踩了哪条
    checks = (
        (ip.is_unspecified, "未指定地址"),
        (ip.is_loopback, "环回地址"),
        (ip.is_link_local, "链路本地地址(云元数据入口)"),
        (ip.is_multicast, "组播地址"),
        (ip.is_reserved, "保留地址"),
        (ip.is_private, "内网地址"),
    )
    for hit, label in checks:
        if hit:
            return f"目标解析到{label} {ip},已按 SSRF 策略拒绝"
    if any(ip in net for net in _EXTRA_BLOCKED_NETS):
        return f"目标解析到运营商共享地址段 {ip}(云元数据入口),已按 SSRF 策略拒绝"
    return None


def check_url_safety(url: str) -> str | None:
    """SSRF 校验总入口:安全返回 None,不安全返回中文拒绝原因。

    重定向的每一跳都要重新调用本函数——单次校验只能证明"当时"安全,证明不了"连接时"安全。
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError as exc:
        return f"URL 解析失败:{exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"不支持的协议 {scheme or '(空)'},仅允许 http/https"

    # urlparse 已做小写与去方括号;末尾点是 FQDN 的合法写法,不去掉会让黑名单比对失效
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        return "URL 缺少主机名"
    if hostname in _BLOCKED_HOSTS or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        return f"主机名 {hostname} 在禁止访问名单内"

    try:
        port = parsed.port
    except ValueError:
        return "URL 端口非法"
    if port is not None and port in _BLOCKED_PORTS:
        return f"端口 {port} 属高危服务端口,已按 SSRF 策略拒绝"

    # 主机名本身就是 IP 字面量时不必过 DNS:少一次解析,也少一个能被投毒的环节
    if _is_ip_literal(hostname):
        return _reject_ip(hostname)

    try:
        infos = socket.getaddrinfo(
            hostname,
            port or (443 if scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except Exception as exc:  # gaierror / UnicodeError(超长域名)等一律拒
        return f"域名 {hostname} 解析失败:{exc}"
    if not infos:
        return f"域名 {hostname} 未解析到任何地址"

    # 必须遍历全部 A/AAAA:只看第一条会被"一条公网 + 一条内网"的轮询记录绕过
    for info in infos:
        reason = _reject_ip(str(info[4][0]))
        if reason:
            return reason
    return None


async def _check_url_safety_async(url: str) -> str | None:
    """异步路径专用:getaddrinfo 是阻塞调用,丢线程里做,别让一次慢解析卡住事件循环。"""
    return await asyncio.to_thread(check_url_safety, url)


# --------------------------------------------------------------------------- #
# 抓取与正文提取
# --------------------------------------------------------------------------- #
def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _normalize_date(value: object) -> str | None:
    """htmldate 正常给 YYYY-MM-DD,但带时间戳/异常格式时防御式只取日期段。"""
    if not value:
        return None
    matched = _DATE_RE.match(str(value).strip())
    return matched.group(1) if matched else None


def _parse_html(raw: bytes) -> tuple[str, str, str | None]:
    """同步解析(在线程里跑):返回 (正文 markdown, 标题, 发布日期)。

    直接把 bytes 喂给 trafilatura,让它按 meta charset 自己判编码,比我们先 decode 更准。
    """
    body = (
        trafilatura.extract(
            raw,
            output_format="markdown",
            include_tables=True,
            include_comments=False,
            favor_precision=True,
        )
        or ""
    )
    title, published = "", None
    try:
        meta = trafilatura.extract_metadata(raw)
    except Exception:
        meta = None  # 元数据是锦上添花,拿不到不该把整次抓取拖成失败
    if meta is not None:
        title = (meta.title or "").strip()
        published = _normalize_date(meta.date)
    return body, title, published


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """流式读并在超限处主动断流——先 aread() 全量再截的话,内存已经炸过一次了。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)[:max_bytes]


def _blocked(url: str, reason: str, *, final_url: str = "") -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=final_url,
        status="blocked",
        error=reason,
        domain=_domain_of(final_url or url),
    )


def _unavailable(url: str, reason: str, *, final_url: str = "") -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=final_url,
        status="unavailable",
        error=reason,
        domain=_domain_of(final_url or url),
    )


async def _fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_chars: int,
    timeout: float,
    max_bytes: int,
) -> FetchedPage:
    """已通过首跳安全校验后的实际抓取;仍可能返回 blocked(重定向/类型不支持)。"""
    current = url
    payload: tuple[str, str, bytes, str | None] | None = None

    for _ in range(_MAX_REDIRECTS + 1):
        # follow_redirects 显式关死:调用方传进来的 client 可能默认开着自动重定向,
        # 那样就绕过了逐跳校验——安全参数不能依赖外部对象的默认值
        async with client.stream(
            "GET", current, headers=_HEADERS, timeout=timeout, follow_redirects=False
        ) as resp:
            if resp.status_code in _REDIRECT_CODES:
                location = (resp.headers.get("location") or "").strip()
                if not location:
                    return _unavailable(
                        url, f"HTTP {resp.status_code} 但缺少 Location 头", final_url=current
                    )
                nxt = urljoin(current, location)
                # 每一跳都重新校验,这是防"公网 302 到内网"与 DNS rebinding 的关键
                reason = await _check_url_safety_async(nxt)
                if reason:
                    return _blocked(url, f"重定向目标被拒绝:{reason}", final_url=nxt)
                current = nxt
                continue

            if resp.status_code >= 400:
                return _unavailable(url, f"HTTP {resp.status_code}", final_url=str(resp.url))

            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            # 空 content-type 放行(不少站点不给),但给了就必须是文本类:
            # PDF/图片/视频既提不出正文,又容易变成大文件下载器
            if content_type and not content_type.startswith(_HTML_TYPES + _TEXT_TYPES):
                return _blocked(
                    url,
                    f"内容类型 {content_type} 不支持正文抓取(仅支持 HTML / 纯文本)",
                    final_url=str(resp.url),
                )
            raw = await _read_capped(resp, max_bytes)
            payload = (str(resp.url), content_type, raw, resp.charset_encoding)
        break

    if payload is None:
        return _unavailable(url, f"重定向超过 {_MAX_REDIRECTS} 跳,已停止跟随", final_url=current)

    final_url, content_type, raw, charset = payload
    if content_type.startswith(_TEXT_TYPES):
        # 纯文本本身就是正文,再走一遍 HTML 抽取只会把它当噪声清掉
        body = raw.decode(charset or "utf-8", errors="replace").strip()
        title, published = "", None
    else:
        # trafilatura 是纯同步的 CPU 密集实现,丢线程里跑,别占着事件循环
        body, title, published = await asyncio.to_thread(_parse_html, raw)

    domain = _domain_of(final_url or url)
    if not body.strip():
        return FetchedPage(
            url=url,
            final_url=final_url,
            title=title,
            status="empty",
            error="页面未提取到正文(可能是纯导航页或需要 JS 渲染)",
            domain=domain,
            published_at=published,
        )

    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars] + _TRUNCATED_MARK
    return FetchedPage(
        url=url,
        final_url=final_url,
        title=title,
        content=body,
        status="ok",
        truncated=truncated,
        domain=domain,
        published_at=published,
    )


async def fetch_page(
    url: str,
    *,
    max_chars: int = 8000,
    timeout: float = 15.0,
    max_bytes: int = 2 * 1024 * 1024,
    client: httpx.AsyncClient | None = None,
) -> FetchedPage:
    """抓取单页正文并转 markdown;任何失败都降级返回,不向上抛异常。"""
    url = (url or "").strip()
    owned: httpx.AsyncClient | None = None
    try:
        reason = await _check_url_safety_async(url)
        if reason:
            return _blocked(url, reason)
        if client is None:
            owned = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
            client = owned
        return await _fetch(client, url, max_chars=max_chars, timeout=timeout, max_bytes=max_bytes)
    except Exception as exc:  # 网络/超时/解析/编码,统一降级(部分异常 str() 为空,兜底类名)
        return _unavailable(url, str(exc) or exc.__class__.__name__)
    finally:
        if owned is not None:
            await owned.aclose()


async def fetch_pages(
    urls: list[str],
    *,
    max_chars: int = 8000,
    timeout: float = 15.0,
    concurrency: int = 4,
    client: httpx.AsyncClient | None = None,
) -> list[FetchedPage]:
    """批量抓取:URL 去重(保序)+ 限并发;单条失败不影响其余,返回顺序与去重后入参一致。"""
    seen: set[str] = set()
    unique: list[str] = []
    for raw in urls or []:
        candidate = (raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    if not unique:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(target: str) -> FetchedPage:
        async with semaphore:
            return await fetch_page(target, max_chars=max_chars, timeout=timeout, client=client)

    owned: httpx.AsyncClient | None = None
    try:
        if client is None:
            # 批量抓取共用一个 client,连接池才能复用;各协程只是并发发请求,互不干扰
            owned = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
            client = owned
        results = await asyncio.gather(*(_one(u) for u in unique), return_exceptions=True)
    finally:
        if owned is not None:
            await owned.aclose()

    pages: list[FetchedPage] = []
    for target, item in zip(unique, results, strict=True):
        if isinstance(item, BaseException):
            # fetch_page 已经兜过异常,这里是 gather 层的兜底(如取消/线程池异常)
            pages.append(_unavailable(target, str(item) or item.__class__.__name__))
        else:
            pages.append(item)
    return pages
