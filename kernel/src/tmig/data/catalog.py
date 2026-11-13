"""來源資料庫的目錄（桌面版的「連線到伺服器」）：列出結構描述與資料表、資料表與自訂 SQL 的欄位。

- 連線類型（CONNECTORS）：Tableau 的連線類型（dbclass）→ 方言、連線設定檔的 driver、預設的連接埠與
  連線對話框要填的項目；憑證一律不進 IR（見 tmig.data.sources）
- 資料表（list_tables）：各資料庫的系統目錄（大多是 information_schema），不含系統的結構描述
- 欄位（table_columns、sql_columns）：資料表的欄位依系統目錄的型別；自訂 SQL 取前幾列，依取回的值判斷
  型別（沒有資料的欄為字串）。型別對應與 Tableau 相同：整數、小數（DECIMAL 的小數位數為 0 時為整數）、
  文字、日期、日期時間、布林
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import pyarrow as pa

from tmig.ir.models import DataType, FileColumn

SAMPLE_ROWS = 200
"""判斷自訂 SQL 欄位型別時取回的列數。"""


class CatalogError(ValueError):
    """無法取得資料表或欄位（連線類型不支援、查詢失敗）。"""


class Fetcher(Protocol):
    def fetch(self, sql: str) -> pa.Table: ...


@dataclass(frozen=True)
class Connector:
    """連線對話框的一種連線類型。"""

    dbclass: str
    label: str
    dialect: str
    driver: str
    port: int | None
    database: str | None = "資料庫"
    """資料庫欄位的名稱（Oracle 為服務名稱、Databricks 為 Catalog、BigQuery 為專案）；None 表示不需要"""
    schema: bool = True
    secret: str = "password"
    """連線設定檔中的密碼項目（Databricks 為 access_token）；空字串表示不需要密碼（雲端的預設憑證）"""
    extras: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """其他設定：（設定檔中的鍵、名稱），例如 Snowflake 的倉儲"""

    def to_json(self) -> dict[str, Any]:
        return {
            "dbclass": self.dbclass,
            "label": self.label,
            "dialect": self.dialect,
            "port": self.port,
            "database": self.database,
            "schema": self.schema,
            "secret": self.secret,
            "extras": [{"key": k, "label": label} for k, label in self.extras],
        }


CONNECTORS: dict[str, Connector] = {
    c.dbclass: c
    for c in (
        Connector("postgres", "PostgreSQL", "postgres", "postgres", 5432),
        Connector("sqlserver", "Microsoft SQL Server", "tsql", "sqlserver", 1433),
        Connector("mysql", "MySQL", "mysql", "mysql", 3306),
        Connector("oracle", "Oracle", "oracle", "oracle", 1521, "服務名稱"),
        Connector(
            "snowflake",
            "Snowflake",
            "snowflake",
            "snowflake",
            None,
            extras=(("warehouse", "倉儲"), ("role", "角色")),
        ),
        Connector("redshift", "Amazon Redshift", "redshift", "redshift", 5439),
        Connector(
            "databricks",
            "Databricks",
            "databricks",
            "databricks",
            443,
            "Catalog",
            secret="access_token",
            extras=(("http_path", "HTTP 路徑"),),
        ),
        Connector("spark", "Spark SQL", "spark", "spark", 10000),
        Connector("presto", "Presto／Trino", "trino", "trino", 8080, "Catalog"),
        Connector(
            "athena",
            "Amazon Athena",
            "athena",
            "athena",
            443,
            "Catalog",
            secret="",
            extras=(("s3_staging_dir", "查詢結果的 S3 位置"), ("work_group", "工作群組")),
        ),
        Connector(
            "bigquery",
            "Google BigQuery",
            "bigquery",
            "bigquery",
            None,
            "專案",
            secret="",
            extras=(("credentials_path", "服務帳號金鑰檔的路徑"),),
        ),
        Connector("teradata", "Teradata", "teradata", "teradata", 1025, None),
        Connector("hadoophive", "Cloudera Hadoop（Hive）", "hive", "hive", 10000),
        Connector("impala", "Impala", "impala", "impala", 21050),
        Connector("db2", "IBM Db2", "db2", "db2", 50000),
        Connector("saphana", "SAP HANA", "hana", "hana", 30015, None),
    )
}


# ---------------------------------------------------------------- 資料表


@dataclass(frozen=True)
class RemoteTable:
    schema: str | None
    name: str
    kind: str
    """table 或 view"""

    @property
    def path(self) -> list[str]:
        return [self.schema, self.name] if self.schema else [self.name]

    def to_json(self) -> dict[str, Any]:
        return {"schema": self.schema, "name": self.name, "kind": self.kind, "path": self.path}


SYSTEM_SCHEMAS = (
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "sys",
    "mysql",
    "performance_schema",
    "INFORMATION_SCHEMA",
)


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def tables_sql(dialect: str, database: str | None, schema: str | None) -> str:
    """列出資料表的查詢（結構描述、名稱、種類）；schema 有值時只列那個結構描述。"""
    system = ", ".join(_quote_literal(s) for s in SYSTEM_SCHEMAS)
    only = f" AND table_schema = {_quote_literal(schema)}" if schema else ""
    info = (
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        f"WHERE table_schema NOT IN ({system}){only} ORDER BY 1, 2"
    )
    if dialect in ("postgres", "redshift"):
        return (
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema NOT IN ({system}) AND table_schema NOT LIKE 'pg_temp%'"
            f"{only} ORDER BY 1, 2"
        )
    if dialect in ("trino", "athena", "snowflake", "duckdb"):
        return info
    if dialect == "mysql":
        target = schema or database
        scope = (
            f"table_schema = {_quote_literal(target)}"
            if target
            else f"table_schema NOT IN ({system})"
        )
        return (
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            f"WHERE {scope} ORDER BY 1, 2"
        )
    if dialect == "tsql":
        return (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA NOT IN ({system})"
            + (f" AND TABLE_SCHEMA = {_quote_literal(schema)}" if schema else "")
            + " ORDER BY 1, 2"
        )
    if dialect in ("databricks", "spark", "hive", "impala"):
        return f"SHOW TABLES IN {schema}" if schema else "SHOW TABLES"
    if dialect == "bigquery":
        if not schema:
            raise CatalogError("BigQuery 要指定資料集（結構描述）")
        return (
            "SELECT table_schema, table_name, table_type "
            f"FROM `{schema}`.INFORMATION_SCHEMA.TABLES ORDER BY 1, 2"
        )
    if dialect == "oracle":
        owner = (
            f"owner = {_quote_literal(schema.upper())}"
            if schema
            else "owner NOT IN ('SYS', 'SYSTEM', 'XDB', 'MDSYS', 'CTXSYS', 'OUTLN', 'DBSNMP', "
            "'APPQOSSYS', 'WMSYS', 'ORDSYS', 'OLAPSYS', 'LBACSYS', 'DVSYS', 'AUDSYS', 'GSMADMIN_INTERNAL')"
        )
        return (
            f"SELECT owner, table_name, 'TABLE' FROM all_tables WHERE {owner} "
            f"UNION ALL SELECT owner, view_name, 'VIEW' FROM all_views WHERE {owner} ORDER BY 1, 2"
        )
    if dialect == "teradata":
        name = schema or database
        scope = f"DatabaseName = {_quote_literal(name)}" if name else "1 = 1"
        return (
            "SELECT DatabaseName, TableName, TableKind FROM DBC.TablesV "
            f"WHERE TableKind IN ('T', 'V', 'O') AND {scope} ORDER BY 1, 2"
        )
    if dialect == "db2":
        scope = (
            f"TABSCHEMA = {_quote_literal(schema.upper())}"
            if schema
            else "TABSCHEMA NOT LIKE 'SYS%'"
        )
        return f"SELECT TABSCHEMA, TABNAME, TYPE FROM SYSCAT.TABLES WHERE {scope} ORDER BY 1, 2"
    if dialect == "hana":
        scope = (
            f"SCHEMA_NAME = {_quote_literal(schema)}" if schema else "SCHEMA_NAME NOT LIKE '%SYS%'"
        )
        return (
            f"SELECT SCHEMA_NAME, TABLE_NAME, 'TABLE' FROM SYS.TABLES WHERE {scope} UNION ALL "
            f"SELECT SCHEMA_NAME, VIEW_NAME, 'VIEW' FROM SYS.VIEWS WHERE {scope} ORDER BY 1, 2"
        )
    raise CatalogError(f"不支援的方言 {dialect}")


def list_tables(
    fetcher: Fetcher, dialect: str, database: str | None = None, schema: str | None = None
) -> list[RemoteTable]:
    """資料庫中的資料表與檢視表。"""
    result = fetcher.fetch(tables_sql(dialect, database, schema)).to_pylist()
    tables: list[RemoteTable] = []
    for row in result:
        values = list(row.values())
        if dialect in ("databricks", "spark", "hive", "impala"):
            # SHOW TABLES：database／namespace、tableName（Hive、Impala 只有 tab_name／name）
            names = {str(k).lower(): v for k, v in row.items()}
            name = (
                names.get("tablename") or names.get("tab_name") or names.get("name") or values[-1]
            )
            space = names.get("database") or names.get("namespace") or schema
            if names.get("istemporary") is True:
                continue
            tables.append(RemoteTable(str(space) if space else None, str(name), "table"))
            continue
        space, name, kind = [*values, None, None, None][:3]
        text = str(kind or "").upper()
        tables.append(
            RemoteTable(
                str(space) if space is not None else None,
                str(name),
                "view" if "VIEW" in text or text == "V" else "table",
            )
        )
    return tables


# ---------------------------------------------------------------- 欄位

_INTEGER = re.compile(
    r"^(tiny|small|medium|big)?int(eger)?\d*$|^(int[248]|serial|bigserial|smallserial|byteint)$"
)


def tableau_type(type_name: str | None, scale: int | None = None) -> DataType:
    """資料庫的型別名稱 → Tableau 的型別。"""
    text = (type_name or "").strip().lower()
    base = re.sub(r"\(.*$", "", text).strip()
    base = re.sub(r"\s+unsigned$|\s+signed$", "", base)
    if base in ("bool", "boolean", "bit"):
        return "boolean"
    if _INTEGER.match(base):
        return "integer"
    if base in ("number", "numeric", "decimal", "dec", "money", "smallmoney"):
        if scale == 0 or (scale is None and re.search(r"\(\s*\d+\s*(,\s*0\s*)?\)", text)):
            return "integer"
        return "real"
    if base in (
        "real",
        "float",
        "float4",
        "float8",
        "double",
        "double precision",
        "binary_float",
        "binary_double",
        "float64",
    ):
        return "real"
    if base == "date":
        return "date"
    if base.startswith(("timestamp", "datetime", "smalldatetime")):
        return "datetime"
    if base in ("geometry", "geography"):
        return "spatial"
    return "string"


def columns_sql(dialect: str, table: Sequence[str]) -> str | None:
    """資料表欄位的查詢（欄名、型別、小數位數），依欄的順序；None 表示改以取樣判斷。"""
    if not table:
        raise CatalogError("沒有指定資料表")
    name = table[-1]
    schema = table[-2] if len(table) >= 2 else None
    where = f"table_name = {_quote_literal(name)}" + (
        f" AND table_schema = {_quote_literal(schema)}" if schema else ""
    )
    if dialect in ("postgres", "redshift", "mysql", "snowflake", "duckdb"):
        return (
            "SELECT column_name, data_type, numeric_scale FROM information_schema.columns "
            f"WHERE {where} ORDER BY ordinal_position"
        )
    if dialect in ("trino", "athena"):
        # Trino 的 information_schema.columns 沒有 numeric_scale：小數位數寫在型別中，例如 decimal(10,2)
        return (
            "SELECT column_name, data_type, NULL AS numeric_scale FROM information_schema.columns "
            f"WHERE {where} ORDER BY ordinal_position"
        )
    if dialect == "tsql":
        return (
            "SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_SCALE FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME = {_quote_literal(name)}"
            + (f" AND TABLE_SCHEMA = {_quote_literal(schema)}" if schema else "")
            + " ORDER BY ORDINAL_POSITION"
        )
    if dialect == "oracle":
        owner = f" AND owner = {_quote_literal(schema)}" if schema else ""
        return (
            "SELECT column_name, data_type, data_scale FROM all_tab_columns "
            f"WHERE table_name = {_quote_literal(name)}{owner} ORDER BY column_id"
        )
    if dialect == "db2":
        owner = f" AND TABSCHEMA = {_quote_literal(schema)}" if schema else ""
        return (
            "SELECT COLNAME, TYPENAME, SCALE FROM SYSCAT.COLUMNS "
            f"WHERE TABNAME = {_quote_literal(name)}{owner} ORDER BY COLNO"
        )
    if dialect == "hana":
        owner = f" AND SCHEMA_NAME = {_quote_literal(schema)}" if schema else ""
        return (
            "SELECT COLUMN_NAME, DATA_TYPE_NAME, SCALE FROM SYS.TABLE_COLUMNS "
            f"WHERE TABLE_NAME = {_quote_literal(name)}{owner} ORDER BY POSITION"
        )
    if dialect == "bigquery" and schema:
        return (
            "SELECT column_name, data_type, NULL FROM "
            f"`{schema}`.INFORMATION_SCHEMA.COLUMNS WHERE table_name = {_quote_literal(name)} "
            "ORDER BY ordinal_position"
        )
    return None


def _quoted(dialect: str, parts: Sequence[str]) -> str:
    if dialect in ("mysql", "databricks", "spark", "hive", "impala", "bigquery"):
        return ".".join("`" + p.replace("`", "``") + "`" for p in parts)
    if dialect == "tsql":
        return ".".join("[" + p.replace("]", "]]") + "]" for p in parts)
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def sample_sql(dialect: str, source: str, rows: int = SAMPLE_ROWS) -> str:
    """前幾列的查詢；source 為資料表的寫法或括號中的自訂 SQL（加上別名）。"""
    if dialect in ("tsql", "teradata"):
        return f"SELECT TOP {rows} * FROM {source}"
    if dialect == "oracle":
        return f"SELECT * FROM {source} WHERE ROWNUM <= {rows}"
    if dialect == "db2":
        return f"SELECT * FROM {source} FETCH FIRST {rows} ROWS ONLY"
    return f"SELECT * FROM {source} LIMIT {rows}"


def arrow_type(kind: pa.DataType) -> DataType:
    """Arrow 的型別 → Tableau 的型別（沒有資料的欄為字串）。"""
    if pa.types.is_boolean(kind):
        return "boolean"
    if pa.types.is_integer(kind):
        return "integer"
    if pa.types.is_floating(kind) or pa.types.is_decimal(kind):
        return "real"
    if pa.types.is_date(kind):
        return "date"
    if pa.types.is_timestamp(kind):
        return "datetime"
    return "string"


def _sampled(fetcher: Fetcher, dialect: str, source: str) -> list[FileColumn]:
    table = fetcher.fetch(sample_sql(dialect, source))
    columns: list[FileColumn] = []
    seen: set[str] = set()
    for name, kind in zip(table.column_names, table.schema.types, strict=True):
        if name in seen:
            raise CatalogError(f"欄名「{name}」重複，請在 SQL 中給每一欄不同的名稱")
        seen.add(name)
        columns.append(FileColumn(name=name, datatype=arrow_type(kind)))
    return columns


def table_columns(fetcher: Fetcher, dialect: str, table: Sequence[str]) -> list[FileColumn]:
    """資料表的欄位（欄名與 Tableau 的型別）。"""
    sql = columns_sql(dialect, table)
    if sql is not None:
        rows = fetcher.fetch(sql).to_pylist()
        columns: list[FileColumn] = []
        for row in rows:
            name, type_name, scale = [*row.values(), None, None, None][:3]
            scale_value = int(scale) if isinstance(scale, int | float) else None
            datatype = tableau_type(str(type_name) if type_name is not None else None, scale_value)
            if datatype == "spatial":
                continue  # 資料庫的空間欄位在 Live 連線中尚不支援
            columns.append(FileColumn(name=str(name), datatype=datatype))
        if columns:
            return columns
    return _sampled(fetcher, dialect, _quoted(dialect, table))


def sql_columns(fetcher: Fetcher, dialect: str, sql: str) -> list[FileColumn]:
    """自訂 SQL 的欄位：取前幾列，依取回的值判斷型別。"""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise CatalogError("自訂 SQL 是空的")
    alias = "" if dialect == "oracle" else " AS tmig_custom"
    return _sampled(fetcher, dialect, f"({text}){alias}")


__all__ = [
    "CONNECTORS",
    "CatalogError",
    "Connector",
    "Fetcher",
    "RemoteTable",
    "arrow_type",
    "columns_sql",
    "list_tables",
    "sample_sql",
    "sql_columns",
    "table_columns",
    "tableau_type",
    "tables_sql",
]
