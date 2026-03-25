"""Main typer app — shop <noun> <verb> [flags]

JSON is always the output format. No --output flag needed.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer

from shop.commands.merchant import app as merchant_app
from shop.commands.schema import app as schema_app
from shop.commands.search import app as search_app

try:
    from importlib.metadata import version as _pkg_version
    _VERSION = _pkg_version("shop-cli")
except Exception:
    _VERSION = "0.1.0"

app = typer.Typer(
    name="shop",
    help="Agent-native commerce CLI. All commands return JSON.",
    add_completion=False,
    no_args_is_help=True,
)

app.add_typer(search_app, name="search", help="Product discovery")
app.add_typer(merchant_app, name="merchant", help="Merchant registry")
app.add_typer(schema_app, name="schema", help="Runtime self-description for agents")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(None, "--version", is_eager=True, help="Show version"),
) -> None:
    if version:
        print(json.dumps({"version": _VERSION}))
        sys.exit(0)
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())
