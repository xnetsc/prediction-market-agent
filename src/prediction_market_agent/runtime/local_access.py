from __future__ import annotations

from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request


LOCAL_NETWORKS = tuple(map(ip_network, (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
)))


def is_local_request(request: Request) -> bool:
    """Classify the requested host, never a forwarded client-address header.

    Container port forwarding changes the socket peer to a bridge address, so the
    browser's target host defines local access. Public proxies must preserve Host.
    """
    host = (request.url.hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return any(address in network for network in LOCAL_NETWORKS)


def require_local_request(request: Request) -> None:
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="Local endpoint requires a local host")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-origin local operation rejected")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(status_code=403, detail="Cross-origin local operation rejected")
