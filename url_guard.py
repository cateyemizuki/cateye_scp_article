"""cateye 出站 URL/下载统一安全护栏（参考实现，随插件复制分发）。

来源：cateye_common（2026-09-29 跨插件安全审查引入）。
约定见 cateye_common/README.md：scheme 白名单 + IP 黑名单（含 CGNAT）+
重定向逐跳复验 + 流式大小上限 + 错误脱敏。

仅依赖标准库，transport 无关：httpx / aiohttp / urllib 均可配合使用。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "UrlGuardError",
    "ForbiddenAddressError",
    "SchemeNotAllowedError",
    "InvalidUrlError",
    "SafeUrl",
    "check_url",
    "check_redirect",
    "is_forbidden_ip",
    "resolve_host_ips",
    "sanitize_error",
    "cap_stream",
]

DEFAULT_SCHEMES = ("https",)
# 元数据/内网/保留段黑名单（IPv4 + IPv6，含 v4-mapped）
_FORBIDDEN_NETS_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT（3.12 的 .private 不覆盖，单列）
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local，含 169.254.169.254 元数据
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)
_FORBIDDEN_NETS_V6 = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::ffff:0:0/96"),   # v4-mapped，成员按 v4 规则再判
    ipaddress.ip_network("64:ff9b:1::/48"),  # NAT64 本地化段
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


class UrlGuardError(Exception):
    """对外安全的护栏异常：str(e) 可直接回显聊天，不含 URL/IP/状态码细节。"""


class SchemeNotAllowedError(UrlGuardError):
    def __init__(self, scheme: str) -> None:
        super().__init__(f"不支持的地址协议（仅允许 {'/'.join(DEFAULT_SCHEMES)}）")
        self.scheme = scheme


class InvalidUrlError(UrlGuardError):
    def __init__(self, reason: str = "地址格式无效") -> None:
        super().__init__(reason)


class ForbiddenAddressError(UrlGuardError):
    def __init__(self) -> None:
        super().__init__("目标地址不被允许（内网/保留地址已拦截）")


@dataclass(frozen=True)
class SafeUrl:
    scheme: str
    host: str
    port: int
    path_query: str

    def reconstruct(self) -> str:
        """重建不含 userinfo 的 URL（凭据一律不落 URL）。"""
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        if self.port not in (None, 443) and self.scheme == "https":
            netloc = f"{netloc}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path_query}"


def is_forbidden_ip(ip: str) -> bool:
    """单个 IP 是否命中黑名单。未知格式一律视为禁止（fail-closed）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    nets = _FORBIDDEN_NETS_V6 if addr.version == 6 else _FORBIDDEN_NETS_V4
    return any(addr in net for net in nets)


async def resolve_host_ips(host: str) -> list[str]:
    """解析主机名（线程池内执行，不阻塞事件循环），返回全部地址。"""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise InvalidUrlError("域名解析失败") from exc
    return [info[4][0] for info in infos]


async def check_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = DEFAULT_SCHEMES,
    allowed_hosts: tuple[str, ...] | None = None,
    check_dns: bool = True,
) -> SafeUrl:
    """请求前校验。失败抛 UrlGuardError 子类（str 可安全回显）。"""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError as exc:
        raise InvalidUrlError() from exc
    if parts.scheme not in allowed_schemes:
        raise SchemeNotAllowedError(parts.scheme or "(empty)")
    if parts.username or parts.password:
        raise InvalidUrlError("地址中不允许携带凭据")
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise InvalidUrlError("缺少主机名")
    if allowed_hosts is not None:
        if host.lower() not in {h.lower().rstrip(".") for h in allowed_hosts}:
            raise InvalidUrlError("目标主机不在允许列表内")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError("端口无效") from exc
    path_query = parts.path or "/"
    if parts.query:
        path_query += "?" + parts.query
    safe = SafeUrl(parts.scheme, host, port, path_query)
    if check_dns:
        # 主机名本身是 IP 时直接判；域名则解析后对全部结果判（尽力收敛 DNS rebinding，
        # httpx 无法钉住连接 IP 的残留窗口在插件文档中注明）。
        try:
            ipaddress.ip_address(host)
            ips = [host]
        except ValueError:
            ips = await resolve_host_ips(host)
        if any(is_forbidden_ip(ip) for ip in ips):
            raise ForbiddenAddressError()
    return safe


async def check_redirect(previous: SafeUrl, location: str, *, allowed_schemes: tuple[str, ...] = DEFAULT_SCHEMES) -> SafeUrl:
    """重定向逐跳复验：location 允许相对路径，host 变化时重新走完整校验。"""
    if location.startswith("/"):
        return SafeUrl(previous.scheme, previous.host, previous.port, location)
    return await check_url(location, allowed_schemes=allowed_schemes, check_dns=True)


def cap_stream(chunks, max_bytes: int):
    """流式读取并限制总大小；超限抛 UrlGuardError。

    用法（httpx）：`data = b"".join(cap_stream(resp.aiter_bytes(), 8 << 20))`
    （aiohttp）：`data = b"".join(cap_stream(resp.content.iter_chunked(65536), 8 << 20))`
    """

    async def _gen():
        total = 0
        async for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise UrlGuardError(f"响应内容超过大小上限（{max_bytes >> 10}KB）")
            yield chunk

    return _gen()


def sanitize_error(exc: BaseException, *, default: str = "请求失败，详情见日志") -> str:
    """把异常转为可回显聊天的简短文案；绝不携带 URL/IP/状态码/内网细节。"""
    return default
