"""Where a request came from.

Used to decide two things that must not be decided loosely:

* whether a visitor is on the home network, which is what an access link is
  scoped to, and
* whether a visitor is on this machine, which is the only place the
  single-user auto-login is safe.

Proxy headers are deliberately **not** trusted by default. `X-Forwarded-For`
is attacker-controlled unless something you run sets it, so honouring it would
let anyone on the internet claim a LAN address and walk in. Set
TRUST_PROXY_HEADERS=true only when this really is behind a reverse proxy you
control.
"""
from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)


def client_ip(request, trust_proxy: bool = False) -> str:
    """The requester's IP address as a string, or "" if it can't be determined."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""


def _parse(ip):
    """Parse an address, or None. Never raises — callers use this to decide
    access, and an unparseable address must fail closed rather than blow up."""
    if not isinstance(ip, str):
        return None
    try:
        return ipaddress.ip_address(ip.strip())
    except ValueError:
        return None


def is_loopback(ip) -> bool:
    """This machine — 127.0.0.0/8 or ::1."""
    addr = _parse(ip)
    return bool(addr and addr.is_loopback)


# Written out rather than using ipaddress.is_private, which is broader than
# "someone in my house": it also covers the documentation ranges (192.0.2/24,
# 198.51.100/24, 203.0.113/24), carrier-grade NAT (100.64/10), and 0.0.0.0/8.
# For an access-control decision the allowed set should be explicit enough to
# read and argue with.
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8",       # loopback
    "10.0.0.0/8",        # RFC1918
    "172.16.0.0/12",     # RFC1918
    "192.168.0.0/16",    # RFC1918
    "169.254.0.0/16",    # link-local (APIPA)
    "::1/128",           # IPv6 loopback
    "fc00::/7",          # IPv6 unique-local
    "fe80::/10",         # IPv6 link-local
))


def is_private(ip) -> bool:
    """True when the address belongs to a home/local network.

    Anything public, unrecognised, empty or malformed is False, so a failure
    to identify the caller is never mistaken for a friendly one.
    """
    addr = _parse(ip)
    if addr is None:
        return False
    # IPv4-mapped IPv6 (::ffff:192.168.1.5) — judge the embedded address.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped:
        addr = mapped
    return any(addr in net for net in _LOCAL_NETWORKS
               if net.version == addr.version)


def local_hostnames() -> list[str]:
    """Every name this machine answers to on the home network.

    Typing an IP address on a phone is nobody's idea of a good time, so the
    machine can be given a short alias (tools/setup_lan_name.ps1 registers one
    as a Windows OptionalName). The alias only helps if the app will also
    *accept* it as a Host header, and hard-coding it in config would mean two
    places to keep in sync. Reading the names the OS actually publishes keeps
    the trusted-host list exactly as wide as reality and no wider.
    """
    names: list[str] = []

    def add(name):
        name = str(name or "").strip().lower()
        # A Host header is a hostname, not a URL. Reject anything carrying a
        # port, path, or wildcard so a stray registry value can't widen the
        # trusted-host list into a match-everything rule.
        if (name and name not in names
                and all(c.isalnum() or c in "-." for c in name)):
            names.append(name)
            names.append(f"{name}.local")

    try:
        add(socket.gethostname())
    except OSError:
        pass

    try:                                    # Windows NetBIOS aliases
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "OptionalNames")
            for name in ([value] if isinstance(value, str) else value or []):
                add(name)
    except (ImportError, OSError, FileNotFoundError):
        pass

    return names


def lan_addresses() -> list[str]:
    """This machine's own LAN addresses, for building a shareable link.

    Opening a UDP socket to an off-machine address makes the OS pick the
    interface it would actually route through, which is more reliable than
    resolving the hostname — that often returns 127.0.0.1.
    """
    found: list[str] = []
    for probe in ("8.8.8.8", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 80))   # UDP: no packets are actually sent
            ip = sock.getsockname()[0]
            if ip and ip not in found and not is_loopback(ip):
                found.append(ip)
        except OSError:
            continue
        finally:
            sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in found and is_private(ip) and not is_loopback(ip):
                found.append(ip)
    except OSError:
        pass

    return found
