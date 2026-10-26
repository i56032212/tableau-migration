import json
from pathlib import Path

import jsonschema

from tmig.ir import ir_json_schema, ir_json_schema_text
from tmig.ir.serialize import schema_filename

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
EXAMPLE = Path(__file__).parent / "data" / "example.ir.json"


def test_committed_schema_is_up_to_date() -> None:
    committed = (SCHEMA_DIR / schema_filename()).read_text(encoding="utf-8")
    assert committed == ir_json_schema_text(), (
        "kernel/schema 已過期，請執行：uv run tmig ir schema --out kernel/schema/"
        + schema_filename()
    )


def test_schema_is_valid_json_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(ir_json_schema())


def test_example_passes_json_schema() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.validate(example, ir_json_schema(), cls=jsonschema.Draft202012Validator)


def test_json_schema_rejects_unknown_fields() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    example["worksheets"][0]["colour"] = "red"
    validator = jsonschema.Draft202012Validator(ir_json_schema())
    assert any("colour" in error.message for error in validator.iter_errors(example))
