from __future__ import annotations

from ipaddress import ip_address

from fastapi import HTTPException, Request


def is_loopback_request(request: Request) -> bool:
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
    return address.is_loopback


def require_local_request(request: Request) -> None:
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Local endpoint requires a loopback host")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-origin local operation rejected")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(status_code=403, detail="Cross-origin local operation rejected")
