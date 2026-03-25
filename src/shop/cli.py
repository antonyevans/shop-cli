"""Main typer app — shop <noun> <verb> [flags]

JSON is always the output format. No --output flag needed.
"""

import typer

from shop.commands.merchant import app as merchant_app
from shop.commands.schema import app as schema_app
from shop.commands.search import app as search_app

app = typer.Typer(
    name="shop",
    help="Agent-native commerce CLI. All commands return JSON.",
    add_completion=False,
    no_args_is_help=True,
)

app.add_typer(search_app, name="search", help="Product discovery")
app.add_typer(merchant_app, name="merchant", help="Merchant registry")
app.add_typer(schema_app, name="schema", help="Runtime self-description for agents")
