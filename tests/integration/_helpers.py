"""Shared helpers for integration tests — importable by test modules."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def shop(shop_home: Path, *args: str, expect_exit: int = 0) -> dict:
    """Run `shop <args>` in the given SHOP_HOME and return parsed JSON."""
    env = {**os.environ, "SHOP_HOME": str(shop_home)}
    result = subprocess.run(
        ["shop", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    stdout = result.stdout.strip()
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {"raw": stdout}

    assert result.returncode == expect_exit, (
        f"`shop {' '.join(args)}` exited {result.returncode}, expected {expect_exit}.\n"
        f"stdout: {stdout}\nstderr: {result.stderr.strip()}"
    )
    return data


def skip_unless(*env_vars: str):
    """pytest.mark.skipif wrapper — skips if any credential env var is missing."""
    missing = [v for v in env_vars if not os.environ.get(v, "").strip()]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"Missing credentials: {', '.join(missing)}",
    )
