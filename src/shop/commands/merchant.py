"""shop merchant — discover/register UCP merchants and connect Shopify Catalog."""

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
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            _error("invalid_url", f"Private/loopback addresses not allowed (resolved to {ip_str})", 1)


async def _discover_ucp_endpoint(base_url: str) -> tuple[str | None, str | None]:
    """Return (ucp_endpoint, name) from /.well-known/ucp Business Profile, or (None, None)."""
    base = base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(f"{base}/.well-known/ucp")
            if r.status_code == 200:
                profile = r.json()
                ucp = profile.get("ucp", {})

                # Extract first REST transport endpoint
                endpoint = None
                for svc in ucp.get("services", []):
                    for transport in svc.get("transports", []):
                        if transport.get("type") == "rest" and transport.get("endpoint"):
                            endpoint = transport["endpoint"].rstrip("/")
                            break
                    if endpoint:
                        break

                # Fall back to top-level ucp_endpoint for non-spec implementations
                if not endpoint:
                    endpoint = ucp.get("endpoint") or profile.get("ucp_endpoint")
                    if endpoint:
                        endpoint = endpoint.rstrip("/")

                name = profile.get("name") or ucp.get("name")
                if endpoint:
                    return endpoint, name
        except (httpx.TimeoutException, httpx.RequestError, ValueError):
            pass

    return None, None


def _save_merchant(merchant_data: dict, merchants_path: Path) -> None:
    """Write or update merchant entry in merchants.yaml."""
    merchants_path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if merchants_path.exists():
        with merchants_path.open() as f:
            data = yaml.safe_load(f) or {}
            existing = data.get("merchants", [])

    slug = merchant_data["slug"]
    existing = [m for m in existing if m.get("slug") != slug]
    existing.append(merchant_data)

    with merchants_path.open("w") as f:
        yaml.dump({"merchants": existing}, f, default_flow_style=False, allow_unicode=True)


def _slug_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or "unknown"
    slug = hostname.removeprefix("www.")
    slug = slug.replace(".", "-").replace("_", "-")
    return slug


async def _run_merchant_add(url: str, merchants_path: Path) -> None:
    _check_ssrf(url)

    ucp_endpoint, name = await _discover_ucp_endpoint(url)
    if not ucp_endpoint:
        _error(
            "merchant_not_discoverable",
            f"No UCP endpoint found at {url}/.well-known/ucp — merchant must publish a UCP Business Profile",
            4,
        )

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
    _emit({"status": "added", "merchant": merchant_data}, 0)


def run_merchant_add_command(url: str, merchants_path: Path = MERCHANTS_PATH) -> None:
    """Core merchant-add logic — separated from CLI wiring for testability."""
    asyncio.run(_run_merchant_add(url, merchants_path))


async def _run_merchant_connect_shopify(
    client_id: str, client_secret: str, ships_to: str, merchants_path: Path
) -> None:
    """Validate Shopify Catalog credentials and save to merchants.yaml."""
    # Verify credentials by obtaining a token
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                "https://api.shopify.com/auth/access_token",
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
            )
            if r.status_code == 401:
                _error("auth_failed", "Invalid Shopify client_id or client_secret", 2)
            r.raise_for_status()
            token_data = r.json()
    except SystemExit:
        raise
    except httpx.TimeoutException:
        _error("network_error", "Timed out contacting Shopify auth endpoint", 6)
    except Exception as e:
        _error("network_error", f"Failed to validate Shopify credentials: {e}", 6)

    scope = token_data.get("scope", "")
    merchant_data = {
        "slug": "shopify",
        "name": "Shopify Global Catalog",
        "adapter": "shopify_catalog",
        "client_id": client_id,
        "client_secret": client_secret,
        "ships_to": ships_to,
    }

    _save_merchant(merchant_data, merchants_path)
    _emit({
        "status": "connected",
        "merchant": {
            "slug": "shopify",
            "name": "Shopify Global Catalog",
            "adapter": "shopify_catalog",
            "ships_to": ships_to,
            "scope": scope,
        },
    }, 0)


def run_merchant_connect_shopify_command(
    client_id: str,
    client_secret: str,
    ships_to: str,
    merchants_path: Path = MERCHANTS_PATH,
) -> None:
    asyncio.run(_run_merchant_connect_shopify(client_id, client_secret, ships_to, merchants_path))


@app.command("add")
def merchant_add(
    url: str = typer.Argument(..., help="Merchant HTTPS URL"),
) -> None:
    """Discover and register a UCP-compatible merchant via /.well-known/ucp."""
    run_merchant_add_command(url)


@app.command("connect-shopify")
def merchant_connect_shopify(
    client_id: str = typer.Option(..., "--client-id", help="Shopify Dev Dashboard app client ID"),
    client_secret: str = typer.Option(..., "--client-secret", help="Shopify Dev Dashboard app client secret"),
    ships_to: str = typer.Option("US", "--ships-to", help="ISO 3166-1 alpha-2 country code for search results"),
) -> None:
    """Connect Shopify Global Catalog — one credential searches all Shopify merchants."""
    run_merchant_connect_shopify_command(client_id, client_secret, ships_to)
