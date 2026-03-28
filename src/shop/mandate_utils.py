"""Mandate file management — Ed25519-signed YAML files."""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml
from canonicaljson import encode_canonical_json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)


class MandateNotFoundError(Exception):
    pass


def get_or_create_device_key(keys_dir: Path) -> Ed25519PrivateKey:
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_path = keys_dir / "device.pem"
    if key_path.exists():
        pem = key_path.read_bytes()
        return load_pem_private_key(pem, password=None)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_path.write_bytes(pem)
    key_path.chmod(0o600)
    return key


def sign_mandate(mandate_data: dict, private_key: Ed25519PrivateKey) -> tuple[str, str]:
    to_sign = {k: v for k, v in mandate_data.items() if k not in ("signature", "public_key")}
    canonical = encode_canonical_json(to_sign)
    sig_bytes = private_key.sign(canonical)
    sig_b64 = base64.b64encode(sig_bytes).decode()
    pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pub_b64 = base64.b64encode(pub_bytes).decode()
    return sig_b64, pub_b64


def verify_mandate(mandate_data: dict) -> bool:
    try:
        sig_b64 = mandate_data.get("signature")
        pub_b64 = mandate_data.get("public_key")
        if not sig_b64 or not pub_b64:
            return False
        sig_bytes = base64.b64decode(sig_b64)
        pub_bytes = base64.b64decode(pub_b64)
        # Load raw public key bytes
        pub_key = _load_raw_ed25519_public_key(pub_bytes)
        to_verify = {k: v for k, v in mandate_data.items() if k not in ("signature", "public_key")}
        canonical = encode_canonical_json(to_verify)
        pub_key.verify(sig_bytes, canonical)
        return True
    except Exception:
        return False


def _load_raw_ed25519_public_key(raw_bytes: bytes) -> Ed25519PublicKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(raw_bytes)


def save_mandate(mandate: dict, mandates_dir: Path) -> Path:
    mandates_dir.mkdir(parents=True, exist_ok=True)
    path = mandates_dir / f"{mandate['mandate_id']}.yaml"
    with path.open("w") as f:
        yaml.dump(mandate, f, default_flow_style=False, allow_unicode=True)
    return path


def load_mandate(mandate_id: str, mandates_dir: Path) -> dict:
    path = mandates_dir / f"{mandate_id}.yaml"
    if not path.exists():
        raise MandateNotFoundError(f"Mandate not found: {mandate_id}")
    with path.open() as f:
        return yaml.safe_load(f)


def list_mandates(mandates_dir: Path) -> list[dict]:
    if not mandates_dir.exists():
        return []
    mandates = []
    for path in sorted(mandates_dir.glob("*.yaml")):
        with path.open() as f:
            mandates.append(yaml.safe_load(f))
    return mandates


def compute_period_start(period: str) -> int:
    now = datetime.now(timezone.utc)
    if period == "monthly":
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(first.timestamp())
    elif period == "weekly":
        # Last Monday
        days_since_monday = now.weekday()
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta

        monday = monday - timedelta(days=days_since_monday)
        return int(monday.timestamp())
    else:  # one-time
        return 0


def get_period_spend(conn: sqlite3.Connection, mandate_id: str, period_start: int) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_usd), 0.0) as total
        FROM mandate_spend
        WHERE mandate_id = ?
          AND recorded_at >= ?
          AND status IN ('confirmed', 'pending')
        """,
        (mandate_id, period_start),
    ).fetchone()
    return float(row["total"]) if row else 0.0


def is_mandate_expired(mandate: dict) -> bool:
    expires_at = mandate.get("expires_at")
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > expiry
    except Exception:
        return False


def check_mandate_policy(
    mandate: dict,
    merchant_slug: str,
    category: str | None,
    price: float,
) -> str | None:
    # (a) expired
    if is_mandate_expired(mandate):
        return "mandate_expired"

    merchants = mandate.get("merchants", {}) or {}
    allow_merchants = merchants.get("allow") or []
    deny_merchants = merchants.get("deny") or []

    categories = mandate.get("categories", {}) or {}
    allow_cats = categories.get("allow") or []
    deny_cats = categories.get("deny") or []

    budget = mandate.get("budget", {}) or {}
    per_order_max = budget.get("per_order_max_usd")

    # (b) merchant_not_allowed
    if allow_merchants and merchant_slug not in allow_merchants:
        return "merchant_not_allowed"

    # (c) category_not_allowed
    if allow_cats and category is not None and category not in allow_cats:
        return "category_not_allowed"

    # (d) merchant_denied
    if merchant_slug in deny_merchants:
        return "merchant_denied"

    # (e) category_denied
    if category is not None and category in deny_cats:
        return "category_denied"

    # (f) per_order_limit
    if per_order_max is not None and price > per_order_max:
        return "per_order_limit_exceeded"

    return None
