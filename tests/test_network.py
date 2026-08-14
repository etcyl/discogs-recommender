"""Tests for services/network.py.

These decide who counts as "on my network" and who counts as "on this
machine", so a wrong answer is an access-control bug.
"""
from types import SimpleNamespace

import pytest

from services import network


def req(host="", **headers):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


class TestIsPrivate:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "::1",
        "192.168.1.10", "10.0.0.5", "172.16.0.1", "172.31.255.254",
        "169.254.1.1",              # link-local
        "fd00::1",                  # unique-local IPv6
        "::ffff:192.168.1.5",       # IPv4-mapped IPv6
    ])
    def test_local_addresses(self, ip):
        assert network.is_private(ip)

    @pytest.mark.parametrize("ip", [
        "8.8.8.8", "1.1.1.1", "203.0.113.5",
        "172.32.0.1",               # just outside 172.16/12
        "2606:4700::1111",
        "", "not-an-ip", "999.999.999.999", None,
    ])
    def test_public_or_unparseable(self, ip):
        """An address we cannot identify is not treated as friendly."""
        assert not network.is_private(ip)


class TestIsLoopback:
    @pytest.mark.parametrize("ip", ["127.0.0.1", "127.1.2.3", "::1"])
    def test_loopback(self, ip):
        assert network.is_loopback(ip)

    @pytest.mark.parametrize("ip", ["192.168.1.5", "8.8.8.8", "", "garbage"])
    def test_not_loopback(self, ip):
        assert not network.is_loopback(ip)


class TestClientIp:
    def test_uses_the_connection_address(self):
        assert network.client_ip(req("192.168.1.9")) == "192.168.1.9"

    def test_ignores_forwarded_headers_by_default(self):
        """X-Forwarded-For is caller-supplied; trusting it grants entry."""
        r = req("203.0.113.9", **{"x-forwarded-for": "127.0.0.1"})
        assert network.client_ip(r) == "203.0.113.9"

    def test_honours_forwarded_header_when_told_to(self):
        r = req("10.0.0.2", **{"x-forwarded-for": "192.168.1.44, 10.0.0.2"})
        assert network.client_ip(r, trust_proxy=True) == "192.168.1.44"

    def test_falls_back_to_real_ip_header(self):
        r = req("10.0.0.2", **{"x-real-ip": "192.168.1.44"})
        assert network.client_ip(r, trust_proxy=True) == "192.168.1.44"

    def test_missing_client_is_empty(self):
        assert network.client_ip(SimpleNamespace(client=None, headers={})) == ""


class TestLanAddresses:
    def test_returns_only_non_loopback_addresses(self):
        for ip in network.lan_addresses():
            assert not network.is_loopback(ip)


class TestLocalHostnames:
    """Names the machine answers to, so a short alias can be typed instead
    of an IP address. These end up in the trusted-host list, so a bad value
    must be dropped rather than widened into a wildcard."""

    def test_includes_the_machine_name(self):
        import socket
        assert socket.gethostname().lower() in network.local_hostnames()

    def test_each_name_gets_an_mdns_form(self):
        import socket
        assert f"{socket.gethostname().lower()}.local" in network.local_hostnames()

    def test_names_are_lowercased(self):
        assert all(n == n.lower() for n in network.local_hostnames())

    def test_no_duplicates(self):
        names = network.local_hostnames()
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("bad", [
        "*", "*.com", "evil.com:80", "a/b", "", "   ", None,
    ])
    def test_a_junk_alias_is_ignored(self, bad, monkeypatch):
        """A registry value is not a URL. Anything with a wildcard, port or
        path would turn the trusted-host list into a match-everything rule."""
        monkeypatch.setattr(network.socket, "gethostname", lambda: bad)
        assert all("*" not in n and "/" not in n and ":" not in n
                   for n in network.local_hostnames())

    def test_a_missing_hostname_does_not_raise(self, monkeypatch):
        def boom():
            raise OSError("no hostname")
        monkeypatch.setattr(network.socket, "gethostname", boom)
        assert isinstance(network.local_hostnames(), list)
