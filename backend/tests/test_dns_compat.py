"""#583: musl fails a dual-stack lookup when AAAA is NXDOMAIN even though A answered."""
import socket
import unittest
from unittest.mock import patch

from core import dns_compat

V4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]
V6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::2", 443, 0, 0))]


def _fake(by_family):
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        result = by_family.get(family)
        if result is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name does not resolve")
        return result
    return getaddrinfo


class DnsCompatTests(unittest.TestCase):
    def _call(self, by_family, *args):
        with patch.object(dns_compat, "_original", _fake(by_family)):
            return dns_compat._getaddrinfo("radarr.example.com", 443, *args)

    def test_dual_stack_success_is_untouched(self):
        self.assertEqual(self._call({0: V4 + V6}), V4 + V6)

    def test_ipv4_only_name_resolves_when_unspec_lookup_fails(self):
        self.assertEqual(self._call({socket.AF_INET: V4}), V4)

    def test_ipv6_only_name_resolves_when_unspec_lookup_fails(self):
        self.assertEqual(self._call({socket.AF_INET6: V6}), V6)

    def test_name_that_resolves_nowhere_still_fails(self):
        with self.assertRaises(socket.gaierror):
            self._call({})

    def test_explicit_family_failure_is_not_retried(self):
        with self.assertRaises(socket.gaierror):
            self._call({socket.AF_INET: V4}, socket.AF_INET6)

    def test_temporary_failures_are_not_masked(self):
        def flaky(*a, **k):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        with patch.object(dns_compat, "_original", flaky):
            with self.assertRaises(socket.gaierror) as ctx:
                dns_compat._getaddrinfo("h", 443)
        self.assertEqual(ctx.exception.errno, socket.EAI_AGAIN)

    def test_install_is_idempotent_and_replaces_socket_getaddrinfo(self):
        saved = socket.getaddrinfo, dns_compat._installed
        try:
            dns_compat._installed = False
            dns_compat.install()
            dns_compat.install()
            self.assertIs(socket.getaddrinfo, dns_compat._getaddrinfo)
        finally:
            socket.getaddrinfo, dns_compat._installed = saved
