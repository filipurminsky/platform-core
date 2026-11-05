"""Caller identity: bearer API-key auth and proxy-aware client IP resolution.

Pure functions (keys / trusted CIDRs are passed in) so they are trivially unit
testable and the route code decides which configuration applies.
"""

import hashlib
import hmac
import ipaddress
from collections.abc import Iterable, Sequence

from starlette.requests import Request

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header.

    The scheme is case-insensitive (RFC 7235 §2.1). Returns None when the header
    is absent, uses another scheme, or carries an empty token.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def match_api_key(token: str, valid_keys: Iterable[str]) -> str | None:
    """Return the matching configured key, or None.

    Every configured key is compared with ``hmac.compare_digest`` and the loop
    never short-circuits, so response timing reveals neither the key contents
    nor which (or how many) keys exist.
    """
    candidate = token.encode()
    matched: str | None = None
    for key in valid_keys:
        if hmac.compare_digest(candidate, key.encode()):
            matched = key
    return matched


def key_id(key: str) -> str:
    """Short, non-reversible identifier for a key — safe for logs and limiter keys."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Client IP
# ---------------------------------------------------------------------------
def _parse_ip(value: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    # Normalise ::ffff:a.b.c.d so it matches IPv4 CIDRs.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_trusted(ip: IPAddress, trusted: Sequence[IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in trusted)


def client_ip(request: Request, trusted_proxies: Sequence[IPNetwork]) -> str:
    """Resolve the originating client IP without trusting spoofable headers.

    - No trusted proxies configured, or the direct peer is not inside one of
      them → the direct peer address (X-Forwarded-For is ignored entirely, so
      a caller can't pick its own rate-limit bucket).
    - Peer is a trusted proxy → walk X-Forwarded-For right-to-left, skipping
      trusted hops, and return the first untrusted address. Entries further
      left were supplied by the client and are never used. If a malformed entry
      is reached first, fall back to the peer (fail towards a shared bucket,
      never towards a client-chosen one). If every hop is trusted, return the
      left-most one.
    """
    peer_raw = request.client.host if request.client and request.client.host else None
    if peer_raw is None:
        return "unknown"
    peer = _parse_ip(peer_raw)
    if peer is None or not trusted_proxies or not _is_trusted(peer, trusted_proxies):
        return peer_raw

    # Multiple XFF headers are equivalent to one comma-joined list (RFC 7230 §3.2.2).
    hops = [
        hop.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for hop in header.split(",")
        if hop.strip()
    ]
    leftmost_trusted: IPAddress = peer
    for hop in reversed(hops):
        ip = _parse_ip(hop)
        if ip is None:
            return str(peer)
        if not _is_trusted(ip, trusted_proxies):
            return str(ip)
        leftmost_trusted = ip
    return str(leftmost_trusted)
