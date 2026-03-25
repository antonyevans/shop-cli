"""shop merchant add — discover and register UCP-compatible merchants."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer
import yaml

from shop.config import MERCHANTS_PATH, SHOP_DIR

_TIMEOUT = 3.0

app = typer.Typer()


def _emit(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(exit_code)


def _error(error_code: str, detail: str, exit_code: int) -> None:
    _emit({"error_code": error_code, "detail": detail, "exit_code": exit_code}, exit_code)


def _check_ssrf(url: str) -> None:
    """Validate URL is safe to fetch. Exits 1 if invalid or private."""
    parsed = urlparse(url)

    if parsed.scheme != "https":
        _error("invalid_url", "HTTPS required — only https:// URLs are accepted", 1)

    hostname = parsed.hostname
    if not hostname:
        _error("invalid_url", "Invalid URL: missing hostname", 1)

    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        _error("invalid_url", f"Cannot resolve hostname: {hostname}", 1)

    for _af, _socktype, _proto, _canonname, addr in results:
        ip_str = addr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        # For IPv4-mapped IPv6, check the underlying IPv4 address
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            _error("invalid_url", f"Private/loopback addresses not allowed (resolved to {ip_str})", 1)


async def _discover_ucp_endpoint(base_url: str) -> tuple[str | None, str | None]:
    """Return (ucp_endpoint, name) from well-known discovery, or (None, None)."""
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        # 1. Try commerce.txt
        try:
            r = await client.get(f"{base}/.well-known/commerce.txt")
            if r.status_code == 200:
                endpoint = None
                name = None
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.startswith("UCP-Endpoint:"):
                        endpoint = line.split(":", 1)[1].strip()
                    elif line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                if endpoint:
                    return endpoint, name
        except (httpx.TimeoutException, httpx.RequestError):
            pass

        # 2. Try ucp.json
        try:
            r = await client.get(f"{base}/.well-known/ucp.json")
            if r.status_code == 200:
                data = r.json()
                endpoint = data.get("ucp_endpoint")
                name = data.get("name")
                if endpoint:
                    return endpoint, name
        except (httpx.TimeoutException, httpx.RequestError, ValueError):
            pass

    return None, None


async def _health_check(ucp_endpoint: str) -> str | None:
    """Return warning string if health check fails, None on success."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{ucp_endpoint.rstrip('/')}/capabilities")
            if r.status_code == 200:
                return None
    except Exception:
        pass
    return "health check failed — endpoint may be unreliable"


def _save_merchant(merchant_data: dict, merchants_path: Path) -> None:
    """Write or update merchant entry in merchants.yaml."""
    merchants_path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if merchants_path.exists():
        with merchants_path.open() as f:
            data = yaml.safe_load(f) or {}
            existing = data.get("merchants", [])

    # Upsert by slug
    slug = merchant_data["slug"]
    existing = [m for m in existing if m.get("slug") != slug]
    existing.append(merchant_data)

    with merchants_path.open("w") as f:
        yaml.dump({"merchants": existing}, f, default_flow_style=False, allow_unicode=True)


def _slug_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or "unknown"
    # Strip www. prefix, replace dots/underscores with dashes
    slug = hostname.removeprefix("www.")
    slug = slug.replace(".", "-").replace("_", "-")
    return slug


async def _run_merchant_add(url: str, merchants_path: Path) -> None:
    _check_ssrf(url)

    ucp_endpoint, name = await _discover_ucp_endpoint(url)
    if not ucp_endpoint:
        _error(
            "merchant_not_discoverable",
            f"No UCP endpoint found at {url}/.well-known/commerce.txt or /.well-known/ucp.json",
            4,
        )

    warning = await _health_check(ucp_endpoint)

    slug = _slug_from_url(url)
    if name is None:
        name = slug.replace("-", " ").title()

    merchant_data = {
        "slug": slug,
        "name": name,
        "adapter": "ucp",
        "url": url,
        "ucp_endpoint": ucp_endpoint,
    }

    _save_merchant(merchant_data, merchants_path)

    result: dict = {"status": "added", "merchant": merchant_data}
    if warning:
        result["warning"] = warning

    _emit(result, 0)


def run_merchant_add_command(url: str, merchants_path: Path = MERCHANTS_PATH) -> None:
    """Core merchant-add logic — separated from CLI wiring for testability."""
    asyncio.run(_run_merchant_add(url, merchants_path))


@app.command("add")
def merchant_add(
    url: str = typer.Argument(..., help="Merchant HTTPS URL"),
) -> None:
    """Discover and register a UCP-compatible merchant."""
    run_merchant_add_command(url)
