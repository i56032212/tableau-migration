"""萃取設定的編輯（Tableau 資料來源分頁右上角的「即時／萃取」與「萃取」的編輯）。

- 即時 → 萃取：資料源的連線換成萃取（來源是原本的即時連線），每個實體欄位在來源的位置記在
  ExtractRefresh.columns；資料由萃取層從來源建立（tmig.data.extract_refresh.refresh_extract）
- 萃取 → 即時：連線換回萃取的來源，欄位換成來源的欄名；萃取篩選留著（只在更新萃取時套用）
- 增量更新的欄位（ExtractRefresh.increment）

只支援單一資料表、join 或自訂 SQL 的資料源；Relationship 模型（多張邏輯資料表）的萃取每張資料表各自一張
萃取資料表，切換尚不支援。檔案型的資料源一律匯入，沒有即時與萃取之分。
"""

from __future__ import annotations

from typing import Any

from tmig.edit import EditError, _datasource, _replace_datasource, _validated
from tmig.ir import IR, DataField, Datasource
from tmig.ir.models import (
    ExtractConnection,
    ExtractRefresh,
    FileConnection,
    LiveConnection,
    ModelRelation,
    SourceColumn,
)

EXTRACT_TABLE = "Extract.Extract"
"""編輯器建立的萃取的資料表（與 Tableau 單一資料表的萃取相同）。"""


def extract_info(ir: IR, datasource: str) -> dict[str, Any]:
    """資料源的萃取設定：模式（live、extract、file）、能不能切換與原因、增量更新的欄位、萃取篩選。"""
    ds = _datasource(ir, datasource)
    connection = ds.connection
    mode = (
        "file"
        if isinstance(connection, FileConnection)
        else "extract"
        if isinstance(connection, ExtractConnection)
        else "live"
    )
    reason: str | None = None
    try:
        _check_switchable(ds)
    except EditError as exc:
        reason = str(exc)
    refresh = connection.refresh if isinstance(connection, ExtractConnection) else None
    source = connection.source if isinstance(connection, ExtractConnection) else None
    return {
        "mode": mode,
        "switchable": reason is None,
        "reason": reason,
        "refreshable": refresh is not None and source is not None and source.relation is not None,
        "increment": refresh.increment if refresh is not None else None,
        "row_limit": refresh.row_limit if refresh is not None else None,
        "filters": [f.model_dump(mode="json") for f in ds.filters if f.stage == "extract"],
        # 可以當作增量更新欄位的欄位：實體的數字或日期欄位
        "increment_fields": [
            {"id": f.id, "caption": f.caption}
            for f in ds.fields
            if f.column is not None and f.datatype in ("integer", "real", "date", "datetime")
        ],
    }


def _check_switchable(ds: Datasource) -> None:
    connection = ds.connection
    if isinstance(connection, FileConnection):
        raise EditError("檔案型的資料源一律匯入，沒有即時與萃取之分")
    if ds.published is not None:
        raise EditError("來自 Published Data Source 的資料源由伺服器上的資料源決定即時或萃取")
    if isinstance(connection, ExtractConnection):
        source, refresh = connection.source, connection.refresh
        if source is None or refresh is None or source.relation is None:
            raise EditError("萃取的來源無法解析（例如來源是檔案），不能改成即時連線")
        if connection.model is not None or isinstance(source.relation, ModelRelation):
            raise EditError("Relationship 模型（多張邏輯資料表）的萃取尚不支援改成即時連線")
        located = {c.field for c in refresh.columns}
        missing = [f.caption for f in ds.fields if f.column is not None and f.id not in located]
        if missing:
            raise EditError(f"萃取的欄位「{missing[0]}」在來源中的位置不明，不能改成即時連線")
        return
    if connection.relation is None:
        raise EditError("資料源還沒有資料表")
    if isinstance(connection.relation, ModelRelation):
        raise EditError("Relationship 模型（多張邏輯資料表）的資料源尚不支援改成萃取")
    if connection.dialect is None:
        raise EditError(f"連線類型 {connection.dbclass} 尚不支援，不能建立萃取")


def switch_to_extract(ir: IR, datasource: str) -> IR:
    """即時 → 萃取：連線換成萃取，來源是原本的即時連線。萃取中的欄名沿用來源的欄名（不同資料表的同名
    欄位加上資料表名稱）；資料還沒有建立，由呼叫端以 refresh_extract 從來源取回。"""
    ds = _datasource(ir, datasource)
    connection = ds.connection
    if not isinstance(connection, LiveConnection):
        raise EditError(f"資料源「{ds.caption}」已經不是即時連線")
    _check_switchable(ds)
    physical = [f for f in ds.fields if f.column is not None]
    if not physical:
        raise EditError("資料源沒有實體的欄位")
    taken: set[str] = set()
    names: dict[str, str] = {}
    for field in physical:
        assert field.column is not None
        name = field.column
        if name in taken:
            name = f"{field.column} ({field.table or field.id})"
        while name in taken:
            name += "_"
        taken.add(name)
        names[field.id] = name
    refresh = ExtractRefresh(
        columns=[
            SourceColumn(field=f.id, relation=f.table, column=f.column or "") for f in physical
        ]
    )
    extract = ExtractConnection(
        kind="extract",
        hyper_file=f"Data/Extracts/{ds.id}.hyper",
        table=EXTRACT_TABLE,
        source=connection,
        refresh=refresh,
    )
    fields: list[DataField] = [
        f.model_copy(update={"column": names[f.id], "table": None}) if f.id in names else f
        for f in ds.fields
    ]
    updated = ds.model_copy(update={"connection": extract, "fields": fields})
    return _validated(_replace_datasource(ir, updated), "改成萃取後資料源不正確")


def switch_to_live(ir: IR, datasource: str) -> IR:
    """萃取 → 即時：連線換回萃取的來源，欄位換成來源的欄名與資料表。萃取篩選留著（改回萃取時再套用）。"""
    ds = _datasource(ir, datasource)
    connection = ds.connection
    if not isinstance(connection, ExtractConnection):
        raise EditError(f"資料源「{ds.caption}」不是萃取")
    _check_switchable(ds)
    assert connection.source is not None and connection.refresh is not None
    located = {c.field: c for c in connection.refresh.columns}
    fields = [
        f.model_copy(update={"column": located[f.id].column, "table": located[f.id].relation})
        if f.id in located
        else f
        for f in ds.fields
    ]
    updated = ds.model_copy(update={"connection": connection.source, "fields": fields})
    return _validated(_replace_datasource(ir, updated), "改成即時連線後資料源不正確")


def set_extract_increment(ir: IR, datasource: str, increment: str | None) -> IR:
    """增量更新的欄位（Tableau 萃取設定的「增量更新」：只取這個欄位大於現有最大值的列）；None 為只能完整更新。"""
    ds = _datasource(ir, datasource)
    connection = ds.connection
    if not isinstance(connection, ExtractConnection) or connection.refresh is None:
        raise EditError(f"資料源「{ds.caption}」不是可以從來源更新的萃取")
    if increment is not None:
        field = next((f for f in ds.fields if f.id == increment), None)
        if field is None or field.column is None:
            raise EditError("增量更新的欄位必須是資料源中實體的欄位")
        if field.datatype not in ("integer", "real", "date", "datetime"):
            raise EditError(f"增量更新的欄位「{field.caption}」必須是數字或日期")
    refresh = connection.refresh.model_copy(update={"increment": increment})
    updated = ds.model_copy(
        update={"connection": connection.model_copy(update={"refresh": refresh})}
    )
    return _validated(_replace_datasource(ir, updated), "萃取設定不正確")
