"""IR 的讀寫與 JSON Schema 產生。

輸出格式固定（UTF-8、縮排 2、保留中文、結尾換行），golden IR 才能直接比對文字。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tmig.ir.models import IR, IR_VERSION


def load_ir(path: str | Path) -> IR:
    """讀取並驗證 IR JSON 檔；不合法時丟出 pydantic.ValidationError。"""
    return IR.model_validate_json(Path(path).read_bytes())


def dumps_ir(ir: IR) -> str:
    return json.dumps(ir.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def save_ir(ir: IR, path: str | Path) -> None:
    Path(path).write_text(dumps_ir(ir), encoding="utf-8", newline="\n")


def ir_json_schema() -> dict[str, Any]:
    schema = IR.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"Tableau 移轉 IR v{IR_VERSION}",
        **{key: value for key, value in schema.items() if key != "title"},
    }


def ir_json_schema_text() -> str:
    return json.dumps(ir_json_schema(), ensure_ascii=False, indent=2) + "\n"


def schema_filename() -> str:
    return f"ir-{IR_VERSION}.schema.json"
