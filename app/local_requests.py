from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    if host is None:
        return False
    try:
        address = ip_address(host)
        return address.is_loopback
    except ValueError:
        return False


def require_local_request(request: Request) -> None:
    denied = HTTPException(status_code=403, detail="Yerel dosya işlemleri yalnızca bu bilgisayarda kullanılabilir.")
    try:
        host = urlsplit(f"//{request.headers.get('host', '')}")
        if host.username or host.path or host.query or host.fragment or not _is_loopback(host.hostname):
            raise denied
        if request.client is not None and not _is_loopback(request.client.host):
            raise denied
        request_port = host.port or (443 if request.url.scheme == "https" else 80)
        raw_origin = request.headers.get("origin")
        if raw_origin is None:
            return
        origin = urlsplit(raw_origin)
        if (
            origin.scheme not in {"http", "https"} or not _is_loopback(origin.hostname)
            or origin.username or origin.path or origin.query or origin.fragment
        ):
            raise denied
        origin_port = origin.port or (443 if origin.scheme == "https" else 80)
        same_origin = (
            origin.scheme == request.url.scheme
            and origin.hostname == host.hostname
            and origin_port == request_port
        )
        if not same_origin and (origin.scheme != "http" or origin_port not in {8000, 5173}):
            raise denied
    except ValueError as exc:
        raise denied from exc
