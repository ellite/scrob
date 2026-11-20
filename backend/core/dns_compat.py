"""Work around musl's all-or-nothing dual-stack lookup (#583).

The Docker image is Alpine, whose musl resolver fails an AF_UNSPEC
getaddrinfo() with EAI_NONAME when the AAAA query gets NXDOMAIN, even though
the A query answered. Some internal DNS setups (conditional forwarders) do
exactly that for IPv4-only names, and glibc tolerated it. httpx and asyncio
both resolve through socket.getaddrinfo, so wrapping it fixes every outbound
connection: when the dual-stack lookup fails, retry per address family and
use whatever answers."""
import socket

_installed = False
_original = socket.getaddrinfo


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _original(host, port, family, type, proto, flags)
    except socket.gaierror as exc:
        if family != socket.AF_UNSPEC or exc.errno != socket.EAI_NONAME:
            raise
        results: list = []
        for single_family in (socket.AF_INET, socket.AF_INET6):
            try:
                results.extend(_original(host, port, single_family, type, proto, flags))
            except socket.gaierror:
                continue
        if results:
            return results
        raise


def install() -> None:
    """Idempotently replace socket.getaddrinfo with the per-family fallback."""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _getaddrinfo
    _installed = True
