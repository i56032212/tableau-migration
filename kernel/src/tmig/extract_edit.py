"""萃取設定的編輯（Tableau 資料來源分頁右上角的「即時／萃取」與「萃取」的編輯）。

- 即時 → 萃取：資料源的連線換成萃取（來源是原本的即時連線），每個實體欄位在來源的位置記在
  ExtractRefresh.columns；資料由萃取層從來源建立（tmig.data.extract_refresh.refresh_extract）
- 萃取 → 即時：連線換回萃取的來源，欄位換成來源的欄名；萃取篩選留著（只在更新萃取時套用）
- 增量更新的欄位（ExtractRefresh.increment）

單一資料表、join 或自訂 SQL 的資料源萃取成一張資料表（Extract.Extract）；Relationship 模型（多張邏輯
資料表）每張邏輯資料表各自一張萃取資料表（Extract.<資料表 id>），關聯照舊。檔案型的資料源一律匯入，
沒有即時與萃取之分。
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
    TableRelation,
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
        "top_rows": refresh.top_rows if refresh is not None else None,
        "aggregate": refresh.aggregate if refresh is not None else False,
        # 彙總可見維度的資料只用在單一張萃取資料表（Relationship 模型各資料表的詳細程度不同）
        "can_aggregate": isinstance(connection, ExtractConnection) and connection.model is None,
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
        if (connection.model is not None) != isinstance(source.relation, ModelRelation):
            raise EditError("萃取與來源的資料模型不一致，不能改成即時連線")
        if connection.model is not None and isinstance(source.relation, ModelRelation):
            known = {t.id for t in source.relation.tables}
            if {t.id for t in connection.model.tables} != known:
                raise EditError("萃取與來源的邏輯資料表不一致，不能改成即時連線")
        located = {c.field for c in refresh.columns}
        missing = [f.caption for f in ds.fields if f.column is not None and f.id not in located]
        if missing:
            raise EditError(f"萃取的欄位「{missing[0]}」在來源中的位置不明，不能改成即時連線")
        return
    if connection.relation is None:
        raise EditError("資料源還沒有資料表")
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
    model = connection.relation if isinstance(connection.relation, ModelRelation) else None
    # 萃取中的欄名沿用來源的欄名；同一張萃取資料表中同名的欄位加上來源資料表的名稱
    taken: set[tuple[str | None, str]] = set()
    names: dict[str, str] = {}
    for field in physical:
        assert field.column is not None
        owner = field.table if model is not None else None
        name = field.column
        if (owner, name) in taken:
            inner = field.physical_table if model is not None else field.table
            name = f"{field.column} ({inner or field.id})"
        while (owner, name) in taken:
            name += "_"
        taken.add((owner, name))
        names[field.id] = name
    if model is None:
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
    else:
        # Relationship 模型：每張邏輯資料表一張萃取資料表（Extract.<id>），關聯與邏輯資料表照舊；
        # 欄位仍屬於原來的邏輯資料表，實體層的 join 已經在萃取中合併
        inner = {t.id: t.relation for t in model.tables}
        refresh = ExtractRefresh(
            columns=[
                SourceColumn(
                    field=f.id,
                    relation=f.physical_table or getattr(inner.get(f.table or ""), "name", None),
                    column=f.column or "",
                )
                for f in physical
            ]
        )
        tables = [
            t.model_copy(
                update={"relation": TableRelation(kind="table", name=t.id, table=["Extract", t.id])}
            )
            for t in model.tables
        ]
        extract = ExtractConnection(
            kind="extract",
            hyper_file=f"Data/Extracts/{ds.id}.hyper",
            table=f"Extract.{model.tables[0].id}",
            model=model.model_copy(update={"tables": tables}),
            source=connection,
            refresh=refresh,
        )
        fields = [
            f.model_copy(update={"column": names[f.id], "physical_table": None})
            if f.id in names
            else f
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
    source = connection.source.relation
    if isinstance(source, ModelRelation):
        # Relationship 模型：欄位仍屬於原來的邏輯資料表；邏輯資料表中有 join 時記下實體的資料表
        joined = {t.id for t in source.tables if not isinstance(t.relation, TableRelation)}
        fields = [
            f.model_copy(
                update={
                    "column": located[f.id].column,
                    "physical_table": located[f.id].relation if f.table in joined else None,
                }
            )
            if f.id in located
            else f
            for f in ds.fields
        ]
    else:
        fields = [
            f.model_copy(update={"column": located[f.id].column, "table": located[f.id].relation})
            if f.id in located
            else f
            for f in ds.fields
        ]
    updated = ds.model_copy(update={"connection": connection.source, "fields": fields})
    return _validated(_replace_datasource(ir, updated), "改成即時連線後資料源不正確")


def set_extract_options(
    ir: IR, datasource: str, *, top_rows: int | None = None, aggregate: bool = False
) -> IR:
    """萃取的資料量（Tableau 萃取設定的「列數」與「彙總」）：只取前 N 筆（None 為全部）、彙總可見維度的
    資料。下次更新萃取時生效。"""
    ds = _datasource(ir, datasource)
    connection = ds.connection
    if not isinstance(connection, ExtractConnection) or connection.refresh is None:
        raise EditError(f"資料源「{ds.caption}」不是可以從來源更新的萃取")
    if top_rows is not None and (isinstance(top_rows, bool) or top_rows <= 0):
        raise EditError("只取前 N 筆的 N 要是正整數")
    if aggregate and connection.model is not None:
        raise EditError("Relationship 模型的萃取不能彙總可見維度的資料（各資料表的詳細程度不同）")
    if aggregate and connection.refresh.increment is not None:
        raise EditError("增量更新的萃取不能彙總（每次只取新的列，彙總的結果會重複）")
    refresh = connection.refresh.model_copy(update={"top_rows": top_rows, "aggregate": aggregate})
    updated = ds.model_copy(
        update={"connection": connection.model_copy(update={"refresh": refresh})}
    )
    return _validated(_replace_datasource(ir, updated), "萃取設定不正確")


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
        if connection.refresh.aggregate:
            raise EditError("彙總可見維度的萃取不能增量更新")
    refresh = connection.refresh.model_copy(update={"increment": increment})
    updated = ds.model_copy(
        update={"connection": connection.model_copy(update={"refresh": refresh})}
    )
    return _validated(_replace_datasource(ir, updated), "萃取設定不正確")
