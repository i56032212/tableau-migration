"""來源資料庫的實跑測試環境：本機 PostgreSQL 與 Spark（沒有時略過）。

- PostgreSQL：環境變數 TMIG_TEST_POSTGRES 提供連線字串時直接使用；否則找 PostgreSQL 的執行檔
  （PATH、PGBIN、/usr/lib/postgresql/*/bin），在暫存目錄建立臨時的資料庫叢集，測試結束後關閉
- Spark（Databricks 的引擎）：已安裝 pyspark（uv sync --group spark）且有 Java 17 以上時以本機模式啟動，
  工作階段的時區設為 UTC（與 Databricks 執行器相同）

環境變數 TMIG_REQUIRE_ENGINES=1 時，環境不齊全改為測試失敗（CI 用），避免默默略過。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tmig.data.sources import PostgresExecutor, normalize_arrow
from tmig.ir import IR, Datasource, ExtractConnection, LiveConnection, TableRelation

REQUIRED = os.environ.get("TMIG_REQUIRE_ENGINES") == "1"


def unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"TMIG_REQUIRE_ENGINES=1，但{reason}")
    pytest.skip(reason)


# ---------------------------------------------------------------- PostgreSQL


def _pg_tools() -> tuple[str, str] | None:
    """initdb 與 pg_ctl 的完整路徑。"""
    candidates: list[Path] = []
    if os.environ.get("PGBIN"):
        candidates.append(Path(os.environ["PGBIN"]))
    found = shutil.which("initdb")
    if found:
        candidates.append(Path(found).parent)
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    for directory in candidates:
        for suffix in ("", ".exe"):
            initdb, pg_ctl = directory / f"initdb{suffix}", directory / f"pg_ctl{suffix}"
            if initdb.is_file() and pg_ctl.is_file():
                return str(initdb), str(pg_ctl)
    return None


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def postgres_cluster() -> Iterator[str]:
    """回傳連線字串；以 root 執行時改用 postgres 帳號（initdb 不接受 root）。"""
    dsn = os.environ.get("TMIG_TEST_POSTGRES")
    if dsn:
        yield dsn
        return
    tools = _pg_tools()
    if tools is None:
        unavailable("找不到 PostgreSQL 的執行檔")
        return
    initdb, pg_ctl = tools
    prefix: list[str] = []
    owner: tuple[int, int] | None = None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        import pwd

        try:
            entry = pwd.getpwnam("postgres")
        except KeyError:
            unavailable("以 root 執行且沒有 postgres 帳號")
            return
        if shutil.which("runuser") is None:
            unavailable("以 root 執行且沒有 runuser")
            return
        prefix = ["runuser", "-u", "postgres", "--"]
        owner = (entry.pw_uid, entry.pw_gid)
    root = Path(tempfile.mkdtemp(prefix="tmig-pg-"))
    data = root / "data"
    if owner is not None:
        os.chown(root, *owner)
    port = _free_port()

    def run(*args: str) -> None:
        result = subprocess.run([*prefix, *args], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{args[0]} 失敗：{result.stderr or result.stdout}")

    try:
        run(
            initdb,
            "-D",
            str(data),
            "-A",
            "trust",
            "-U",
            "tmig",
            "-E",
            "UTF8",
            "--no-locale",
        )
        options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories="
        run(pg_ctl, "-D", str(data), "-o", options, "-l", str(root / "pg.log"), "-w", "start")
    except RuntimeError as exc:
        shutil.rmtree(root, ignore_errors=True)
        unavailable(f"無法啟動臨時的 PostgreSQL：{exc}")
        return
    try:
        yield f"host=127.0.0.1 port={port} user=tmig dbname=postgres"
    finally:
        with contextlib.suppress(RuntimeError):
            run(pg_ctl, "-D", str(data), "-m", "immediate", "stop")
        shutil.rmtree(root, ignore_errors=True)


def pg_connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


PG_TYPES = {
    "int64": "BIGINT",
    "int32": "INTEGER",
    "double": "DOUBLE PRECISION",
    "float": "REAL",
    "string": "TEXT",
    "large_string": "TEXT",
    "bool": "BOOLEAN",
    "date32[day]": "DATE",
}


def pg_type(kind: pa.DataType) -> str:
    if pa.types.is_timestamp(kind):
        return "TIMESTAMP"
    return PG_TYPES[str(kind)]


def pg_load(con: Any, name: str, table: pa.Table, schema: str | None = None) -> None:
    """Arrow 資料表 → PostgreSQL 資料表（名稱與欄名原樣，以雙引號包住）。"""
    columns = ", ".join(f'"{f.name}" {pg_type(f.type)}' for f in table.schema)
    target = f'"{schema}"."{name}"' if schema else f'"{name}"'
    if schema:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    con.execute(f"DROP TABLE IF EXISTS {target}")
    con.execute(f"CREATE TABLE {target} ({columns})")
    names = ", ".join(f'"{n}"' for n in table.column_names)
    with con.cursor() as cursor, cursor.copy(f"COPY {target} ({names}) FROM STDIN") as copy:
        for row in table.to_pylist():
            copy.write_row(list(row.values()))


def postgres_executor(con: Any) -> PostgresExecutor:
    return PostgresExecutor(con)


# ---------------------------------------------------------------- Spark


@contextlib.contextmanager
def spark_session() -> Iterator[Any]:
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        unavailable("未安裝 pyspark（uv sync --group spark）")
        return
    if shutil.which("java") is None and not os.environ.get("JAVA_HOME"):
        unavailable("找不到 Java（Spark 需要 Java 17 以上）")
        return
    try:
        spark = (
            SparkSession.builder.master("local[2]")
            .appName("tmig-tests")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "2")
            .config("spark.driver.memory", "1g")
            .getOrCreate()
        )
    except Exception as exc:
        unavailable(f"無法啟動 Spark：{exc}")
        return
    spark.sparkContext.setLogLevel("ERROR")
    try:
        yield spark
    finally:
        spark.stop()


class SparkExecutor:
    """測試用：在本機 Spark 執行 Databricks 方言的 SQL。"""

    dialect = "databricks"

    def __init__(self, spark: Any) -> None:
        self.spark = spark

    def fetch(self, sql: str) -> pa.Table:
        return normalize_arrow(self.spark.sql(sql).toArrow())

    def close(self) -> None:
        return None


# ---------------------------------------------------------------- IR 與 fixture


def as_live(ir: IR, dialect: str, schema: list[str] | None = None, prefix: str = "") -> IR:
    """萃取的資料源改成 Live 連線（測試時把萃取的資料載入來源資料庫；資料表名稱為 prefix + 資料源 id）。"""

    def convert(ds: Datasource) -> Datasource:
        if not isinstance(ds.connection, ExtractConnection):
            return ds
        table = [*(schema or []), prefix + ds.id]
        connection = LiveConnection(
            kind="live",
            connection_id=f"test_{dialect}",
            dbclass=dialect,
            dialect=dialect,  # type: ignore[arg-type]
            relation=TableRelation(kind="table", name=ds.id, table=table),
        )
        return ds.model_copy(update={"connection": connection})

    return ir.model_copy(update={"datasources": [convert(ds) for ds in ir.datasources]})


def extract_tables(directory: Path, ir: IR) -> dict[str, pa.Table]:
    """fixture 中各資料源的萃取資料（data/<資料源 id>.duckdb；不在版控中，缺少時由 .twbx 匯入）。"""
    import duckdb

    from tmig.verify.run import ensure_data

    ensure_data(directory, ir)
    tables: dict[str, pa.Table] = {}
    for ds in ir.datasources:
        if not isinstance(ds.connection, ExtractConnection):
            continue
        table = ".".join(f'"{p}"' for p in ds.connection.table.split("."))
        with duckdb.connect(str(directory / "data" / f"{ds.id}.duckdb"), read_only=True) as con:
            tables[ds.id] = con.sql(f"SELECT * FROM {table}").to_arrow_table()
    return tables


# ---------------------------------------------------------------- Oracle（模擬）

TRUNC_UNITS = {
    "YYYY": "year",
    "Q": "quarter",
    "MM": "month",
    "DD": "day",
    "HH24": "hour",
    "MI": "minute",
}
CHAR_PARTS = {
    "Q": "quarter({x})",
    "HH24": "hour({x})",
    "MI": "minute({x})",
    "SS": "second({x})",
    "DDD": "dayofyear({x})",
    "IW": "week({x})",
    "IYYY": "isoyear({x})",
    # 儒略日：DuckDB 的 julian() 為小數
    "J": "CAST(julian({x}) AS BIGINT)",
}
ORACLE_TO_STRFTIME = [
    ("YYYY", "%Y"),
    ("HH24", "%H"),
    ("HH12", "%I"),
    ("MONTH", "%B"),
    ("MON", "%b"),
    ("DDD", "%j"),
    ("FF6", "%f"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("MI", "%M"),
    ("SS", "%S"),
    ("AM", "%p"),
]
TRY_TO_TIMESTAMP = re.compile(
    r"TO_TIMESTAMP\((.*?) DEFAULT NULL ON CONVERSION ERROR, '((?:[^']|'')*)'\)"
)


class OracleEmulator:
    """測試用：沒有 Oracle 實機，把轉譯後的 Oracle SQL 以 SQLGlot 轉回 DuckDB，Oracle 特有的函式
    （TRUNC 的格式、TO_CHAR、ADD_MONTHS 的月底規則、NUMTODSINTERVAL、REGEXP_SUBSTR 等）依 Oracle 的語意
    改寫後在 DuckDB 執行。

    檢查的是轉譯的結構（GROUP BY、*、布林值、Null 的比較、CTE 的串接）與函式對照的寫法；
    Oracle 函式本身的語意依官方文件模擬，仍待實機驗證。
    """

    dialect = "oracle"

    def __init__(self, con: Any) -> None:
        self.con = con

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        sql = TRY_TO_TIMESTAMP.sub(
            lambda m: f"TMIG_TRY_TO_TIMESTAMP({m.group(1)}, '{m.group(2)}')", sql
        )
        # SQLGlot 把只有一個參數的 TRUNC 都當成日期截斷；保留原本的參數個數自行判斷
        sql = re.sub(r"\bTRUNC\(", "TMIG_TRUNC(", sql)
        tree = sqlglot.parse_one(sql, read="oracle")
        duck = _visit(tree, _emulate).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


def _visit(node: Any, fn: Any) -> Any:
    from sqlglot import exp

    for key, value in list(node.args.items()):
        if isinstance(value, exp.Expr):
            node.set(key, _visit(value, fn))
        elif isinstance(value, list):
            node.set(key, [_visit(v, fn) if isinstance(v, exp.Expr) else v for v in value])
    return fn(node)


def _duck(template: str, **parts: Any) -> Any:
    """DuckDB 的運算式樣板；{名稱} 換成對應的語法節點。"""
    import sqlglot
    from sqlglot import exp

    text = template.format(**{k: f"__{k}__" for k in parts})
    tree = sqlglot.parse_one(text, read="duckdb")
    for column in list(tree.find_all(exp.Column)):
        name = column.name
        if name.startswith("__") and name.endswith("__") and name[2:-2] in parts:
            # SQLGlot 產生 SQL 時不會自動補括號
            column.replace(exp.Paren(this=parts[name[2:-2]].copy()))
    return tree


def _date_like(node: Any) -> bool:
    from sqlglot import exp

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return node.to.this in (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP)
    return isinstance(
        node, (exp.CurrentDate, exp.StrToDate, exp.DateStrToDate, exp.DateTrunc, exp.AddMonths)
    )


def _emulate(node: Any) -> Any:
    from sqlglot import exp

    if isinstance(node, exp.Anonymous) and node.name.upper() == "TMIG_TRUNC":
        arg = node.expressions[0]
        if len(node.expressions) == 1 and not _date_like(arg):
            return _duck("trunc({x})", x=arg)
        # 截斷到日以上的 Oracle DATE 以 DuckDB 的 DATE 表示（DATE ± 數字為加減天數、DATE - DATE 為天數）
        fmt = node.expressions[1].name.upper() if len(node.expressions) == 2 else "DD"
        unit = TRUNC_UNITS[fmt]
        kind = "TIMESTAMP" if unit in ("hour", "minute") else "DATE"
        return _duck(f"CAST(date_trunc('{unit}', {{x}}) AS {kind})", x=arg)
    if isinstance(node, exp.StrToTime) and isinstance(node.this, exp.Literal):
        # TIMESTAMP '…' 常值
        return _duck(f"CAST('{node.this.name}' AS TIMESTAMP)")
    if isinstance(node, exp.ToNumber) and isinstance(node.this, (exp.ToChar, exp.TimeToStr)):
        fmt = node.this.args.get("format")
        if isinstance(fmt, exp.Literal) and fmt.name.upper() in CHAR_PARTS:
            return _duck(CHAR_PARTS[fmt.name.upper()], x=node.this.this)
    if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DECIMAL:
        # Oracle 的 NUMBER(19) 是整數（DuckDB 的日期只能加減 INTEGER）；NUMBER 是十進位的浮點數
        target = "INTEGER" if len(node.to.expressions) == 1 else "DOUBLE"
        return exp.Cast(this=node.this, to=exp.DataType.build(target))
    if isinstance(node, exp.ToChar):
        fmt = node.args.get("format")
        if fmt is None:
            return _duck("CAST({x} AS VARCHAR)", x=node.this)
        if isinstance(fmt, exp.Literal) and fmt.name in ("FMMonth", "FMDay"):
            fn = "monthname" if fmt.name == "FMMonth" else "dayname"
            return _duck(f"{fn}({{x}})", x=node.this)
    if isinstance(node, exp.AddMonths):
        # Oracle：月底加減月份後仍是月底
        return _duck(
            "CASE WHEN last_day(CAST({d} AS DATE)) = CAST({d} AS DATE) "
            "THEN CAST(last_day(CAST({d} AS DATE) + to_months(CAST({n} AS INTEGER))) AS TIMESTAMP) "
            "ELSE CAST({d} AS TIMESTAMP) + to_months(CAST({n} AS INTEGER)) END",
            d=node.this,
            n=node.expression,
        )
    if isinstance(node, exp.Anonymous) and node.name.upper() == "NUMTODSINTERVAL":
        amount, unit = node.expressions
        if unit.name.upper() == "SECOND":
            return _duck("to_microseconds(CAST(round({n} * 1000000) AS BIGINT))", n=amount)
        fn = {"DAY": "to_days", "HOUR": "to_hours", "MINUTE": "to_minutes"}[unit.name.upper()]
        return _duck(f"{fn}(CAST({{n}} AS INTEGER))", n=amount)
    if isinstance(node, exp.RegexpSubstr):
        group = node.args.get("group") or exp.Literal.number(0)
        occurrence = node.args.get("occurrence") or exp.Literal.number(1)
        return _duck(
            "list_extract(regexp_extract_all({s}, {p}, {g}), {o})",
            s=node.this,
            p=node.expression,
            g=group,
            o=occurrence,
        )
    if isinstance(node, exp.Pad):
        # DuckDB 的 RPAD 長度只接受 INTEGER
        node.set("expression", exp.Cast(this=node.expression, to=exp.DataType.build("INTEGER")))
        return node
    if isinstance(node, exp.Cast) and node.args.get("default") is not None:
        return exp.TryCast(this=node.this, to=node.to)
    if isinstance(node, exp.Anonymous) and node.name.upper() == "TMIG_TRY_TO_TIMESTAMP":
        value, fmt = node.expressions
        text = fmt.name
        for oracle, strftime in ORACLE_TO_STRFTIME:
            text = text.replace(oracle, strftime)
        text = text.replace('"', "")
        return _duck(f"try_strptime({{s}}, '{text}')", s=value)
    return node
