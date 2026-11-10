from __future__ import annotations

import errno
import logging
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

logger = logging.getLogger(__name__)
ADDRESS_ERRORS = {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.ENETUNREACH, errno.EHOSTUNREACH}


def address_error(error: BaseException) -> bool:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in ADDRESS_ERRORS:
            return True
        text = str(current).lower()
        if any(message in text for message in (
            "cannot assign requested address", "network is unreachable",
            "no route to host", "address family not supported",
        )):
            return True
        pending.extend(
            item for item in (current.__cause__, current.__context__, *current.args)
            if isinstance(item, BaseException)
        )
    return False


def catalog_error(error: BaseException) -> str:
    if address_error(error):
        return (
            "Cannot connect from the NAS/container (address or route unavailable). "
            "Check its network connection and try IPv4-only catalog connections in Settings. "
            "Library-only album search and manual tag editing work offline."
        )
    # musicbrainzngs otherwise appends an unhelpful 'caused by: None'.
    if error.args and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def _ipv4_get(url: str, **kwargs: Any) -> httpx.Response:
    parsed = urlsplit(url)
    proxies = getproxies()
    proxy = None
    if parsed.hostname and not proxy_bypass(parsed.hostname):
        proxy = proxies.get(parsed.scheme) or proxies.get("all")
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    transport = httpx.HTTPTransport(local_address="0.0.0.0", proxy=proxy, retries=0)
    # Supply the proxy on this transport so environment mounts cannot bypass its IPv4 binding.
    with httpx.Client(transport=transport, trust_env=False) as client:
        return client.get(url, **kwargs)


def catalog_get(url: str, *, ipv4_only: bool = False, **kwargs: Any) -> httpx.Response:
    if ipv4_only:
        return _ipv4_get(url, **kwargs)
    try:
        return httpx.get(url, **kwargs)
    except (httpx.ConnectError, OSError) as exc:
        if not address_error(exc):
            raise
        logger.warning("Catalog connection to %s failed; retrying once over IPv4", urlsplit(url).hostname)
        return _ipv4_get(url, **kwargs)
