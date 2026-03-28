"""Tests for shop schema commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from shop.cli import app

runner = CliRunner()


class TestSchemaCommands:
    def test_exit_code_0(self):
        result = runner.invoke(app, ["schema", "commands"])
        assert result.exit_code == 0

    def test_output_is_valid_json(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        assert isinstance(data, dict)

    def test_has_commands_key(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        assert "commands" in data
        assert isinstance(data["commands"], list)

    def test_commands_list_not_empty(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        assert len(data["commands"]) > 0

    def test_each_command_has_required_fields(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        required = {"noun", "verb", "description", "flags", "exit_codes", "mutates"}
        for cmd in data["commands"]:
            missing = required - set(cmd.keys())
            assert not missing, f"Command {cmd.get('noun')} missing fields: {missing}"

    def test_search_products_present(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        search = next(
            (c for c in data["commands"] if c["noun"] == "search" and c["verb"] == "products"),
            None,
        )
        assert search is not None
        assert search["mutates"] is False
        assert 0 in search["exit_codes"]
        assert 5 in search["exit_codes"]  # low confidence

    def test_merchant_add_present(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        merchant_add = next(
            (c for c in data["commands"] if c["noun"] == "merchant" and c["verb"] == "add"),
            None,
        )
        assert merchant_add is not None
        assert merchant_add["mutates"] is True
        assert 1 in merchant_add["exit_codes"]  # SSRF / invalid URL
        assert 4 in merchant_add["exit_codes"]  # not discoverable

    def test_schema_commands_describes_itself(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        schema_cmd = next(
            (c for c in data["commands"] if c["noun"] == "schema" and c["verb"] == "commands"),
            None,
        )
        assert schema_cmd is not None
        assert schema_cmd["exit_codes"] == [0]
        assert schema_cmd["mutates"] is False

    def test_history_has_null_verb(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        history = next((c for c in data["commands"] if c["noun"] == "history"), None)
        assert history is not None
        assert history["verb"] is None

    def test_mutating_commands_flagged(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        mutating = {(c["noun"], c["verb"]) for c in data["commands"] if c["mutates"]}
        assert ("cart", "add") in mutating
        assert ("order", "create") in mutating
        assert ("mandate", "create") in mutating
        assert ("merchant", "add") in mutating

    def test_each_flag_has_required_fields(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        for cmd in data["commands"]:
            for flag in cmd["flags"]:
                assert "name" in flag, f"Flag in {cmd['noun']} missing 'name'"
                assert "type" in flag, f"Flag in {cmd['noun']} missing 'type'"
                assert "required" in flag, f"Flag in {cmd['noun']} missing 'required'"
                assert "description" in flag, f"Flag in {cmd['noun']} missing 'description'"

    def test_order_create_requires_idempotency_key(self):
        result = runner.invoke(app, ["schema", "commands"])
        data = json.loads(result.output)
        order_create = next(
            (c for c in data["commands"] if c["noun"] == "order" and c["verb"] == "create"), None
        )
        assert order_create is not None
        idem = next((f for f in order_create["flags"] if f["name"] == "idempotency-key"), None)
        assert idem is not None
        assert idem["required"] is True
