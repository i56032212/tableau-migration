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


def test_save_ir_never_exposes_a_partial_file(tmp_path: Path) -> None:
    """存檔先寫暫存檔再換上：同時讀取的一方讀到的都是完整的 IR（編輯器存檔時其他請求可能正在重新載入），
    存完後資料夾中沒有留下暫存檔。"""
    import threading

    from tmig.ir.serialize import load_ir, save_ir

    ir = load_ir(EXAMPLE)
    target = tmp_path / "golden.ir.json"
    save_ir(ir, target)
    failures: list[BaseException] = []
    done = threading.Event()

    def read() -> None:
        while not done.is_set():
            try:
                assert load_ir(target) == ir
            except BaseException as exc:
                failures.append(exc)
                return

    readers = [threading.Thread(target=read) for _ in range(3)]
    for reader in readers:
        reader.start()
    try:
        for _ in range(150):
            save_ir(ir, target)
    finally:
        done.set()
        for reader in readers:
            reader.join(timeout=30)
    assert failures == []
    assert [p.name for p in tmp_path.iterdir()] == ["golden.ir.json"]
    assert target.read_text(encoding="utf-8").endswith("}\n")
