"""Host/address safety checks (SSRF protection).

A crawler follows arbitrary links, which means without guard rails it can be
steered into fetching internal services: localhost, the LAN, or cloud metadata
endpoints like 169.254.169.254. We resolve each host and refuse private,
loopback, link-local, reserved and multicast addresses by default.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # not a parseable address -> be safe and block
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def resolve_addresses(host: str) -> list[str]:
    """Resolve a hostname to all of its IP addresses (v4 and v6)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return list({info[4][0] for info in infos})


def is_public_host(host: str) -> bool:
    """True if *every* address ``host`` resolves to is publicly routable.

    Resolving here (rather than trusting the literal hostname) also defends
    against DNS names that point at private space. If any resolved address is
    blocked we reject the host outright.
    """
    if not host:
        return False
    addrs = resolve_addresses(host)
    if not addrs:
        return False
    return all(not _is_blocked_ip(ip) for ip in addrs)


def url_is_safe(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return is_public_host(host)
