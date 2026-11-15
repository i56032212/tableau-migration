"""IR 的讀寫與 JSON Schema 產生。

輸出格式固定（UTF-8、縮排 2、保留中文、結尾換行），golden IR 才能直接比對文字。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from tmig.ir.models import IR, IR_VERSION

REPLACE_ATTEMPTS = 25
"""存檔時換上新檔的嘗試次數（Windows 上檔案正被讀取時要等它關閉；每次間隔 20 毫秒）。"""


def load_ir(path: str | Path) -> IR:
    """讀取並驗證 IR JSON 檔；不合法時丟出 pydantic.ValidationError。"""
    return IR.model_validate_json(Path(path).read_bytes())


def dumps_ir(ir: IR) -> str:
    return json.dumps(ir.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def save_ir(ir: IR, path: str | Path) -> None:
    """寫成 JSON 檔。先寫到同一個資料夾的暫存檔再換上（os.replace）：同時讀取的一方讀到的是舊的或新的
    完整內容，不會讀到寫到一半的檔案（編輯器存檔時，其他請求可能正在重新載入這個檔案）。"""
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(dumps_ir(ir), encoding="utf-8", newline="\n")
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                # Windows：另一個請求正開著這個檔案讀取時不能換上，稍等再試
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


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
