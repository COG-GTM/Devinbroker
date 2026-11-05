"""Guards for fetching user-supplied URLs server-side (SSRF protection).

Only absolute http(s) URLs whose hostname resolves exclusively to public
(globally routable) addresses are accepted.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 2048


class UnsafeURLError(ValueError):
    """Raised when a URL is not safe to fetch from the server."""


def is_public_ip(ip) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


def resolve_public_ips(host: str, port: int) -> list[str]:
    """Resolve *host* and return its addresses, rejecting any non-public one."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise UnsafeURLError(f"Could not resolve host {host!r}: {e}") from e
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if not is_public_ip(ip):
            raise UnsafeURLError(f"Host {host!r} resolves to non-public address {ip}")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise UnsafeURLError(f"Host {host!r} did not resolve to any address")
    return ips


def validate_public_url(url, *, resolve: bool = True) -> tuple[str, int, list[str]]:
    """Validate that *url* is an http(s) URL pointing at a public host.

    Returns ``(hostname, port, resolved_ips)``; ``resolved_ips`` is empty when
    ``resolve`` is False. Raises :class:`UnsafeURLError` otherwise.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("URL must be a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is too long")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise UnsafeURLError("URL must not contain whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError(f"Malformed URL: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"URL scheme {scheme or '(none)'!r} is not allowed (http/https only)")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URL must not include credentials")
    if port is None:
        port = 443 if scheme == "https" else 80
    ips: list[str] = []
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeURLError(f"Address {host} is not a public address")
        ips = [str(literal)]
    elif resolve:
        ips = resolve_public_ips(host, port)
    return host, port, ips
