"""來源資料庫的實跑測試環境：本機 PostgreSQL 與 Spark（沒有時略過）、CI 的 Trino（本機模擬）。

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
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tmig.data.sources import MySQLExecutor, PostgresExecutor, normalize_arrow
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
    # 以平台判斷（而不是 hasattr），pyright 在 Windows 上才不會檢查只有 POSIX 才有的函式
    if sys.platform != "win32" and os.geteuid() == 0:
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
    if sys.platform != "win32" and owner is not None:
        os.chown(root, *owner)
    port = _free_port()

    log = root / "pg.log"

    def run(*args: str, daemon: bool = False) -> None:
        """daemon：pg_ctl start 啟動的伺服器會繼承輸出的管道（Windows 尤其如此），
        擷取輸出會一直等不到結束；改為不擷取，失敗時看伺服器的紀錄檔。"""
        try:
            result = subprocess.run(
                [*prefix, *args],
                capture_output=not daemon,
                stdout=subprocess.DEVNULL if daemon else None,
                stderr=subprocess.DEVNULL if daemon else None,
                stdin=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{Path(args[0]).name} 逾時") from exc
        if result.returncode != 0:
            detail = result.stderr or result.stdout
            if daemon and log.is_file():
                detail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"{Path(args[0]).name} 失敗：{detail}")

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
        run(
            pg_ctl,
            "-D",
            str(data),
            "-o",
            options,
            "-l",
            str(log),
            "-w",
            "-t",
            "60",
            "start",
            daemon=True,
        )
    except RuntimeError as exc:
        shutil.rmtree(root, ignore_errors=True)
        unavailable(f"無法啟動臨時的 PostgreSQL：{exc}")
        return
    try:
        yield f"host=127.0.0.1 port={port} user=tmig dbname=postgres"
    finally:
        with contextlib.suppress(RuntimeError):
            run(pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop", daemon=True)
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


# ---------------------------------------------------------------- MySQL


@contextlib.contextmanager
def mysql_server() -> Iterator[dict[str, Any]]:
    """回傳 PyMySQL 的連線參數。環境變數 TMIG_TEST_MYSQL（user:password@host:port）提供時直接使用
    （CI 用執行環境內建的 MySQL）；否則找 mysqld，在暫存目錄建立臨時的伺服器，測試結束後關閉。"""
    url = os.environ.get("TMIG_TEST_MYSQL")
    if url:
        auth, _, address = url.rpartition("@")
        user, _, password = auth.partition(":")
        host, _, port = address.partition(":")
        yield {"host": host, "port": int(port or 3306), "user": user, "password": password}
        return
    mysqld = shutil.which("mysqld") or (
        "/usr/sbin/mysqld" if Path("/usr/sbin/mysqld").is_file() else None
    )
    if mysqld is None or sys.platform == "win32":
        unavailable("找不到 MySQL 的執行檔（mysqld）")
        return
    root = Path(tempfile.mkdtemp(prefix="tmig-mysql-"))
    data, socket_path = root / "data", root / "mysql.sock"
    base = [mysqld, "--no-defaults", f"--datadir={data}"]
    if sys.platform != "win32" and os.geteuid() == 0:
        base.append("--user=root")
    try:
        subprocess.run(
            [*base, "--initialize-insecure"],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        unavailable(f"無法初始化臨時的 MySQL：{exc}")
        return
    port = _free_port()
    server = subprocess.Popen(
        [
            *base,
            f"--port={port}",
            "--bind-address=127.0.0.1",
            f"--socket={socket_path}",
            "--mysqlx=OFF",
            "--skip-log-bin",
            f"--pid-file={root / 'mysqld.pid'}",
            f"--log-error={root / 'error.log'}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    params = {"host": "127.0.0.1", "port": port, "user": "root", "password": ""}
    try:
        import pymysql
    except ImportError:
        server.terminate()
        shutil.rmtree(root, ignore_errors=True)
        unavailable("未安裝 PyMySQL")
        return
    for _ in range(240):
        try:
            pymysql.connect(**params).close()
            break
        except pymysql.Error:
            if server.poll() is not None:
                break
            time.sleep(0.25)
    else:
        server.terminate()
        shutil.rmtree(root, ignore_errors=True)
        unavailable("臨時的 MySQL 沒有在時間內啟動")
        return
    if server.poll() is not None:
        log = (root / "error.log").read_text(encoding="utf-8", errors="replace")[-1000:]
        shutil.rmtree(root, ignore_errors=True)
        unavailable(f"無法啟動臨時的 MySQL：{log}")
        return
    try:
        yield params
    finally:
        server.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            server.wait(timeout=60)
        shutil.rmtree(root, ignore_errors=True)


MYSQL_TYPES = {
    "int64": "BIGINT",
    "int32": "INT",
    "double": "DOUBLE",
    "float": "FLOAT",
    "string": "VARCHAR(1024)",
    "large_string": "VARCHAR(1024)",
    "bool": "BOOLEAN",
    "date32[day]": "DATE",
}


def mysql_connect(params: dict[str, Any], database: str = "tmig_test") -> Any:
    """建立測試的資料庫（utf8mb4_bin：區分大小寫與重音，與 DuckDB 的比較相同）並連線。"""
    import pymysql

    with (
        contextlib.closing(pymysql.connect(**params, autocommit=True)) as con,
        con.cursor() as cursor,
    ):
        cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
    return pymysql.connect(**params, database=database, charset="utf8mb4", autocommit=True)


def mysql_load(con: Any, name: str, table: pa.Table) -> None:
    """Arrow 資料表 → MySQL 資料表（名稱與欄名原樣，以反引號包住）。"""

    def kind(field: pa.Field) -> str:
        if pa.types.is_timestamp(field.type):
            return "DATETIME(6)"
        return MYSQL_TYPES[str(field.type)]

    columns = ", ".join(f"`{f.name}` {kind(f)}" for f in table.schema)
    names = ", ".join(f"`{n}`" for n in table.column_names)
    marks = ", ".join(["%s"] * len(table.column_names))
    with con.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS `{name}`")
        cursor.execute(f"CREATE TABLE `{name}` ({columns}) COLLATE utf8mb4_bin")
        rows = [tuple(row.values()) for row in table.to_pylist()]
        for start in range(0, len(rows), 1000):
            cursor.executemany(
                f"INSERT INTO `{name}` ({names}) VALUES ({marks})", rows[start : start + 1000]
            )


def mysql_executor(con: Any) -> MySQLExecutor:
    """與 MySQLExecutor.open 相同的工作階段設定；連線的定序也是 utf8mb4_bin（常值之間的比較區分大小寫）。"""
    with con.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute("SET time_zone = '+00:00'")
        cursor.execute("SET lc_time_names = 'en_US'")
        cursor.execute("SET collation_connection = 'utf8mb4_bin'")
    return MySQLExecutor(con)


# ---------------------------------------------------------------- Spark


@contextlib.contextmanager
def spark_session() -> Iterator[Any]:
    try:
        from pyspark.sql import SparkSession  # pyright: ignore[reportMissingImports]
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
        # Oracle 23ai 以前 SELECT 一定要有 FROM：轉譯時補上的 FROM "DUAL"
        con.execute('CREATE OR REPLACE TEMP VIEW "DUAL" AS SELECT \'X\' AS "DUMMY"')

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
        # Oracle 的 NUMBER(19) 是整數；NUMBER 是十進位的浮點數
        target = "BIGINT" if len(node.to.expressions) == 1 else "DOUBLE"
        return exp.Cast(this=node.this, to=exp.DataType.build(target))
    if isinstance(node, exp.Round) and node.args.get("decimals") is not None:
        # DuckDB 的 ROUND 小數位數只接受 INTEGER
        node.set("decimals", exp.Cast(this=node.args["decimals"], to=exp.DataType.build("INTEGER")))
        return node
    if isinstance(node, exp.Chr):
        node.set(
            "expressions",
            [exp.Cast(this=e, to=exp.DataType.build("INTEGER")) for e in node.expressions],
        )
        return node
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


# ---------------------------------------------------------------- SQL Server（模擬）


class TSqlEmulator:
    """測試用：沒有 SQL Server 實機，把轉譯後的 T-SQL 以 SQLGlot 轉回 DuckDB，SQL Server 特有的語意
    （FLOAT(53)、BIT、NVARCHAR(MAX)、LEN 不計結尾的空白、浮點數轉整數時捨去、DATETIME2FROMPARTS、
    VAR／STDEVP 等）依官方文件模擬後在 DuckDB 執行。

    檢查的是轉譯的結構（GROUP BY、TOP、布林值以 BIT 表示、Null 的比較、字串常值）與函式對照的寫法；
    SQL Server 函式本身的語意依官方文件模擬，仍待實機驗證。
    """

    dialect = "tsql"

    def __init__(self, con: Any) -> None:
        self.con = con

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        tree = sqlglot.parse_one(sql, read="tsql")
        duck = _visit(tree, _emulate_tsql).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


TSQL_AGGREGATES = {"VAR": "var_samp", "VARP": "var_pop", "STDEVP": "stddev_pop"}


def _emulate_tsql(node: Any) -> Any:
    from sqlglot import exp

    if isinstance(node, exp.National):
        return exp.Literal.string(node.name)
    if isinstance(node, exp.DataType):
        if node.this in (exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE):
            return exp.DataType.build("DOUBLE")
        if node.this in (
            exp.DataType.Type.NVARCHAR,
            exp.DataType.Type.VARCHAR,
            exp.DataType.Type.TEXT,
        ):
            return exp.DataType.build("VARCHAR")
        if node.this == exp.DataType.Type.BIT:
            return exp.DataType.build("BOOLEAN")
        return node
    if isinstance(node, exp.Cast) and node.to.this in (
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.INT,
    ):
        # SQL Server 的浮點數轉整數是捨去（DuckDB 是四捨五入）；布林值轉整數照常
        # （欄位與整數的轉型不受影響：產生的 SQL 中浮點數的欄位轉整數一定先捨去）
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, (exp.Column, exp.Literal, exp.Cast, exp.Case)):
            return node
        return _duck(f"CAST(trunc({{x}}) AS {node.to.sql('duckdb')})", x=node.this)
    if isinstance(node, exp.Length):
        return _duck("length(rtrim({x}))", x=node.this)
    if isinstance(node, exp.Round) and node.args.get("truncate") is not None:
        return _duck("trunc({x})", x=node.this)
    if isinstance(node, exp.Anonymous):
        name = node.name.upper()
        args = node.expressions
        if name == "ROUND" and len(args) == 3:
            return _duck("trunc({x})", x=args[0])
        if name == "NCHAR":
            return _duck("chr(CAST({x} AS INTEGER))", x=args[0])
        if name in TSQL_AGGREGATES:
            return _duck(f"{TSQL_AGGREGATES[name]}({{x}})", x=args[0])
        if name == "DATETIME2FROMPARTS":
            y, m, d, hh, mi, ss, fraction, precision = args
            return _duck(
                "make_timestamp(CAST({y} AS BIGINT), CAST({m} AS BIGINT), CAST({d} AS BIGINT), "
                "CAST({hh} AS BIGINT), CAST({mi} AS BIGINT), "
                "CAST(trunc({ss}) AS DOUBLE) + CAST(trunc({f}) AS DOUBLE) / pow(10, {p}))",
                y=y,
                m=m,
                d=d,
                hh=hh,
                mi=mi,
                ss=ss,
                f=fraction,
                p=precision,
            )
    return node


# ---------------------------------------------------------------- Snowflake（模擬）


class SnowflakeEmulator:
    """測試用：沒有 Snowflake 實機，把轉譯後的 Snowflake SQL 以 SQLGlot 轉回 DuckDB，Snowflake 特有的函式
    （REGEXP_INSTR 的參數、TRY_TO_TIMESTAMP_NTZ 的格式等）依官方文件模擬後在 DuckDB 執行。

    檢查的是轉譯的結構與函式對照的寫法；Snowflake 函式本身的語意依官方文件模擬，仍待實機驗證。
    """

    dialect = "snowflake"

    def __init__(self, con: Any) -> None:
        self.con = con

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        tree = sqlglot.parse_one(sql, read="snowflake")
        duck = _visit(tree, _emulate_snowflake).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


SNOWFLAKE_TO_STRFTIME = [
    ("YYYY", "%Y"),
    ("HH24", "%H"),
    ("HH12", "%I"),
    ("MMMM", "%B"),
    ("MON", "%b"),
    ("FF6", "%f"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("MI", "%M"),
    ("SS", "%S"),
    ("AM", "%p"),
]


def _snowflake_format(text: str) -> str:
    """Snowflake 的日期格式（文字以雙引號包住）→ strftime。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            end = text.index('"', i + 1)
            out.append(text[i + 1 : end].replace("%", "%%"))
            i = end + 1
            continue
        for token, strftime in SNOWFLAKE_TO_STRFTIME:
            if text.startswith(token, i):
                out.append(strftime)
                i += len(token)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _emulate_snowflake(node: Any) -> Any:
    from sqlglot import exp

    if isinstance(node, exp.RegexpInstr) or (
        isinstance(node, exp.Anonymous) and node.name.upper() == "REGEXP_INSTR"
    ):
        subject = node.this if isinstance(node, exp.RegexpInstr) else node.expressions[0]
        pattern = node.expression if isinstance(node, exp.RegexpInstr) else node.expressions[1]
        flags = node.args.get("parameters") if isinstance(node, exp.RegexpInstr) else None
        if flags is None and isinstance(node, exp.Anonymous) and len(node.expressions) > 5:
            flags = node.expressions[5]
        option = "i" if flags is not None and "i" in flags.name else "c"
        return _duck(
            f"(CASE WHEN regexp_matches({{s}}, {{p}}, '{option}') THEN 1 ELSE 0 END)",
            s=subject,
            p=pattern,
        )
    if isinstance(node, exp.StrToTime) and node.args.get("safe"):
        # SQLGlot 已把格式換成 strftime，但沒有去掉包住文字的雙引號
        fmt = node.args["format"]
        text = fmt.name.replace('"', "") if isinstance(fmt, exp.Literal) else None
        if text is not None:
            return _duck(f"try_strptime({{s}}, '{text}')", s=node.this)
    if isinstance(node, exp.Anonymous) and node.name.upper() == "TRY_TO_TIMESTAMP_NTZ":
        value, fmt = node.expressions
        return _duck(f"try_strptime({{s}}, '{_snowflake_format(fmt.name)}')", s=value)
    return node


# ---------------------------------------------------------------- Redshift（模擬）


class RedshiftEmulator:
    """測試用：沒有 Redshift 實機，把轉譯後的 Redshift SQL 以 SQLGlot 轉回 DuckDB，Redshift 特有的函式
    （DATEADD、REGEXP_INSTR／REGEXP_SUBSTR 的參數、TO_CHAR、TO_TIMESTAMP 的格式等）依官方文件模擬後
    在 DuckDB 執行。

    檢查的是轉譯的結構與函式對照的寫法；Redshift 函式本身的語意依官方文件模擬，仍待實機驗證。
    """

    dialect = "redshift"

    def __init__(self, con: Any) -> None:
        self.con = con

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        # SQLGlot 會把 TO_CHAR、TO_TIMESTAMP 的格式換成 strftime（不完全）：保留原文自行轉換
        sql = re.sub(r"\bTO_CHAR\(", "TMIG_TO_CHAR(", sql)
        sql = re.sub(r"\bTO_TIMESTAMP\(", "TMIG_TO_TIMESTAMP(", sql)
        sql = re.sub(r"\bTO_DATE\(", "TMIG_TO_DATE(", sql)
        tree = sqlglot.parse_one(sql, read="redshift")
        duck = _visit(tree, _emulate_redshift).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


PG_TO_STRFTIME = [
    ("YYYY", "%Y"),
    ("HH24", "%H"),
    ("HH12", "%I"),
    ("Month", "%B"),
    ("Mon", "%b"),
    ("DDD", "%j"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("MI", "%M"),
    ("SS", "%S"),
    ("US", "%f"),
    ("AM", "%p"),
]


def _pg_format(text: str) -> str:
    """PostgreSQL／Redshift 的日期格式（文字以雙引號包住）→ strftime。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            end = text.index('"', i + 1)
            out.append(text[i + 1 : end].replace("%", "%%"))
            i = end + 1
            continue
        for token, strftime in PG_TO_STRFTIME:
            if text.startswith(token, i):
                out.append(strftime)
                i += len(token)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _emulate_redshift(node: Any) -> Any:
    from sqlglot import exp

    def args(n: Any) -> list[Any]:
        if isinstance(n, exp.Anonymous):
            return list(n.expressions)
        return [
            n.this,
            n.expression,
            *[
                v
                for k, v in n.args.items()
                if k not in ("this", "expression") and isinstance(v, exp.Expr)
            ],
        ]

    name = node.name.upper() if isinstance(node, exp.Anonymous) else ""
    if isinstance(node, exp.DataType) and node.this in (
        exp.DataType.Type.VARCHAR,
        exp.DataType.Type.TEXT,
    ):
        return exp.DataType.build("VARCHAR")  # VARCHAR(MAX)
    if isinstance(node, exp.RegexpInstr) or name == "REGEXP_INSTR":
        subject, pattern = args(node)[:2]
        return _duck(
            "(CASE WHEN {s} IS NULL OR {p} IS NULL THEN NULL "
            "WHEN regexp_matches({s}, {p}) THEN 1 ELSE 0 END)",
            s=subject,
            p=pattern,
        )
    if isinstance(node, exp.RegexpExtract) or name == "REGEXP_SUBSTR":
        if isinstance(node, exp.RegexpExtract):
            subject, pattern = node.this, node.expression
            flags = node.args.get("parameters")
        else:
            subject, pattern = node.expressions[0], node.expressions[1]
            flags = node.expressions[4] if len(node.expressions) > 4 else None
        group = 1 if flags is not None and "e" in flags.name else 0
        return _duck(f"regexp_extract({{s}}, {{p}}, {group})", s=subject, p=pattern)
    if isinstance(node, exp.RegexpReplace) or name == "REGEXP_REPLACE":
        subject, pattern, replacement = args(node)[:3]
        return _duck("regexp_replace({s}, {p}, {r}, 'g')", s=subject, p=pattern, r=replacement)
    if name == "TMIG_TO_CHAR":
        value, fmt = node.expressions
        if isinstance(fmt, exp.Literal) and fmt.name == "Month":
            return _duck("monthname({x})", x=value)
        if isinstance(fmt, exp.Literal) and fmt.name == "Day":
            return _duck("dayname({x})", x=value)
        raise AssertionError(f"模擬器不支援 TO_CHAR 的格式 {fmt.sql()}")
    if name == "GETDATE":
        return _duck("current_localtimestamp()")
    if name == "LOG" and len(node.expressions) == 1:
        return _duck("log10({x})", x=node.expressions[0])
    if name == "TMIG_TO_DATE":
        # 非嚴格模式：超出範圍的月、日進位（2024-13-05 → 2025-01-05）
        value, fmt = node.expressions
        assert fmt.name == "YYYY-MM-DD"
        return _duck(
            "CAST(make_date(CAST(split_part({x}, '-', 1) AS BIGINT), 1, 1) "
            "+ to_months(CAST(split_part({x}, '-', 2) AS BIGINT) - 1) "
            "+ to_days(CAST(split_part({x}, '-', 3) AS BIGINT) - 1) AS DATE)",
            x=value,
        )
    if name == "TMIG_TO_TIMESTAMP":
        value, fmt = node.expressions
        return _duck(f"strptime({{x}}, '{_pg_format(fmt.name)}')", x=value)
    return node


# ---------------------------------------------------------------- Trino（CI 實跑；本機模擬）


def service_address(env: str, name: str) -> str | None:
    """CI 以 Docker 啟動的來源資料庫（環境變數提供位址）；本機沒有時回傳 None（改用模擬）。
    TMIG_REQUIRE_SERVICES=1（CI）時沒有提供改為測試失敗，避免默默改用模擬。"""
    value = os.environ.get(env) or None
    if value is None and os.environ.get("TMIG_REQUIRE_SERVICES") == "1":
        pytest.fail(f"TMIG_REQUIRE_SERVICES=1，但沒有 {name}（環境變數 {env}）")
    return value


def trino_address() -> str | None:
    """Trino 的位址（TMIG_TEST_TRINO=主機:連接埠）。"""
    return service_address("TMIG_TEST_TRINO", "Trino")


def trino_connect(address: str) -> Any:
    """連上 Trino（memory catalog）；伺服器剛啟動時會回報 SERVER_STARTING_UP，最多等 5 分鐘。"""
    import trino

    host, _, port = address.partition(":")
    deadline = time.monotonic() + 300
    while True:
        con = trino.dbapi.connect(
            host=host,
            port=int(port or 8080),
            user="tmig",
            catalog="memory",
            schema="default",
            timezone="UTC",
        )
        try:
            cursor = con.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchall()
            return con
        except Exception as exc:
            con.close()
            if time.monotonic() > deadline:
                raise RuntimeError(f"Trino（{address}）沒有回應：{exc}") from exc
            time.sleep(3)


TRINO_TYPES = {
    pa.string(): "VARCHAR",
    pa.large_string(): "VARCHAR",
    pa.float64(): "DOUBLE",
    pa.int64(): "BIGINT",
    pa.int32(): "INTEGER",
    pa.bool_(): "BOOLEAN",
    pa.date32(): "DATE",
}


def _trino_type(kind: pa.DataType) -> str:
    if pa.types.is_timestamp(kind):
        return "TIMESTAMP(6)"
    return TRINO_TYPES[kind]


def _trino_literal(value: Any) -> str:
    import datetime as dt

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"DOUBLE '{value!r}'"
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ', timespec='microseconds')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def trino_load(con: Any, name: str, table: pa.Table, batch: int = 400) -> None:
    """Arrow 資料表 → Trino 的 memory.default（欄名照原文；Trino 的識別字不分大小寫）。"""

    def run(sql: str) -> None:
        cursor = con.cursor()
        cursor.execute(sql)
        cursor.fetchall()

    target = f'memory.default."{name}"'
    run(f"DROP TABLE IF EXISTS {target}")
    columns = ", ".join(f'"{f.name}" {_trino_type(f.type)}' for f in table.schema)
    run(f"CREATE TABLE {target} ({columns})")
    rows = table.to_pylist()
    names = table.column_names
    for start in range(0, len(rows), batch):
        values = ", ".join(
            "(" + ", ".join(_trino_literal(row[n]) for n in names) + ")"
            for row in rows[start : start + batch]
        )
        run(f"INSERT INTO {target} VALUES {values}")


class TrinoEmulator:
    """測試用：本機沒有 Trino 時，把轉譯後的 Trino SQL 以 SQLGlot 轉回 DuckDB 執行；Trino 特有的寫法
    （CODEPOINT、陣列的 REPEAT、SLICE 的長度、取代字串的 $1）依官方文件的語意模擬。
    CI 另以 Docker 的 Trino 實跑（TMIG_TEST_TRINO）。"""

    def __init__(self, con: Any, dialect: str = "trino") -> None:
        self.con = con
        self.dialect = dialect

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        tree = sqlglot.parse_one(sql, read=self.dialect)
        duck = _visit(tree, _emulate_trino).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


def _java_to_re2(text: str) -> str:
    """Java 的取代字串（$1 參照群組、\\$ 為錢字號）→ RE2（\\1）。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\\\\" if nxt == "\\" else nxt)
            i += 2
            continue
        if char == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            out.append("\\" + text[i + 1])
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _emulate_trino(node: Any) -> Any:
    from sqlglot import exp

    if isinstance(node, exp.DataType) and node.this in (
        exp.DataType.Type.VARCHAR,
        exp.DataType.Type.TEXT,
    ):
        return exp.DataType.build("VARCHAR")  # VARCHAR(1)
    if isinstance(node, exp.Anonymous) and node.name.upper() == "CODEPOINT":
        return _duck("unicode({x})", x=node.expressions[0])
    if isinstance(node, exp.ArrayToString) and isinstance(node.this, exp.Repeat):
        # Trino 的 REPEAT 產生陣列：串接起來就是 DuckDB 的字串 repeat
        return node.this
    if isinstance(node, exp.ArraySlice):
        # Trino 的 SLICE(陣列, 起點, 長度)；SQLGlot 把長度當成結尾
        return _duck(
            "list_slice({a}, {s}, {s} + {n} - 1)",
            a=node.this,
            s=node.args["start"],
            n=node.args["end"],
        )
    if isinstance(node, exp.RegexpReplace):
        replacement = node.args.get("replacement")
        if isinstance(replacement, exp.Literal) and replacement.is_string:
            return _duck(
                f"regexp_replace({{s}}, {{p}}, '{_java_to_re2(replacement.name)}', 'g')",
                s=node.this,
                p=node.expression,
            )
    return node


# ---------------------------------------------------------------- BigQuery（CI 以模擬器實跑；本機以 DuckDB 模擬）


def bigquery_address() -> str | None:
    """BigQuery 模擬器（goccy/bigquery-emulator，以 ZetaSQL 實作）的位址（TMIG_TEST_BIGQUERY=主機:連接埠）。"""
    return service_address("TMIG_TEST_BIGQUERY", "BigQuery 模擬器")


def bigquery_client(address: str) -> Any:
    """連上 BigQuery 模擬器（專案與資料集都是 tmig）；剛啟動時最多等 3 分鐘。"""
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    client = bigquery.Client(
        "tmig",
        client_options=ClientOptions(api_endpoint=f"http://{address}"),
        credentials=AnonymousCredentials(),
    )
    deadline = time.monotonic() + 180
    while True:
        try:
            client.create_dataset("tmig", exists_ok=True)
            return client
        except Exception as exc:
            if time.monotonic() > deadline:
                raise RuntimeError(f"BigQuery 模擬器（{address}）沒有回應：{exc}") from exc
            time.sleep(2)


BIGQUERY_TYPES = {
    pa.string(): "STRING",
    pa.large_string(): "STRING",
    pa.float64(): "FLOAT64",
    pa.int64(): "INT64",
    pa.int32(): "INT64",
    pa.bool_(): "BOOL",
    pa.date32(): "DATE",
}


def _bigquery_json(value: Any) -> Any:
    import datetime as dt

    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def bigquery_load(client: Any, name: str, table: pa.Table, batch: int = 500) -> None:
    """Arrow 資料表 → BigQuery 的 tmig.tmig（沒有時區的時間為 DATETIME）。"""
    from google.cloud import bigquery

    ref = f"tmig.tmig.{name}"
    client.delete_table(ref, not_found_ok=True)
    schema = [
        bigquery.SchemaField(
            f.name, "DATETIME" if pa.types.is_timestamp(f.type) else BIGQUERY_TYPES[f.type]
        )
        for f in table.schema
    ]
    client.create_table(bigquery.Table(ref, schema=schema))
    rows = [{k: _bigquery_json(v) for k, v in row.items()} for row in table.to_pylist()]
    for start in range(0, len(rows), batch):
        errors = client.insert_rows_json(ref, rows[start : start + batch])
        if errors:
            raise RuntimeError(f"BigQuery 模擬器寫入 {name} 失敗：{errors[:3]}")


class BigQueryEmulator:
    """測試用：本機沒有 BigQuery 時，把轉譯後的 BigQuery SQL 以 SQLGlot 轉回 DuckDB 執行；BigQuery 特有的寫法
    （EXTRACT(DAYOFWEEK) 星期日為 1、ISOWEEK、SAFE.PARSE_DATETIME、UNNEST 的 0 起算 OFFSET）依官方文件的語意
    模擬。CI 另以 BigQuery 模擬器（ZetaSQL）實跑（TMIG_TEST_BIGQUERY）。"""

    dialect = "bigquery"

    def __init__(self, con: Any) -> None:
        self.con = con
        con.execute("SET TimeZone = 'UTC'")

    def fetch(self, sql: str) -> pa.Table:
        import sqlglot

        tree = sqlglot.parse_one(sql, read="bigquery")
        duck = _visit(tree, _emulate_bigquery).sql(dialect="duckdb")
        return self.con.sql(duck).to_arrow_table()

    def close(self) -> None:
        return None


def _emulate_bigquery(node: Any) -> Any:
    from sqlglot import exp

    if isinstance(node, exp.Extract) and isinstance(node.this, exp.Var):
        part = node.this.name.upper()
        if part == "DAYOFWEEK":
            return _duck("(dayofweek({x}) + 1)", x=node.expression)
        if part == "ISOWEEK":
            return _duck("week({x})", x=node.expression)
    # INT64 轉回 DuckDB 是 BIGINT：DuckDB 的 round 的位數、chr 只接受 INTEGER
    if isinstance(node, exp.Round) and node.args.get("decimals") is not None:
        return _duck("round({x}, CAST({d} AS INTEGER))", x=node.this, d=node.args["decimals"])
    if isinstance(node, exp.Chr):
        return _duck("chr(CAST({x} AS INTEGER))", x=node.expressions[0])
    if isinstance(node, exp.SafeFunc) and isinstance(node.this, exp.ParseDatetime):
        fmt = node.this.args["format"].name.replace("%E*S", "%S.%f")
        return _duck(f"try_strptime({{s}}, '{fmt}')", s=node.this.this)
    if isinstance(node, exp.Array) and node.expressions:
        select = node.expressions[0]
        source = select.args.get("from_") if isinstance(select, exp.Select) else None
        unnest = source.this if source is not None else None
        where = select.args.get("where") if isinstance(select, exp.Select) else None
        if isinstance(unnest, exp.Unnest) and unnest.args.get("offset") and where is not None:
            # UNNEST … WITH OFFSET 的位置從 0 起算（DuckDB 的 WITH ORDINALITY 從 1 起算）
            bounds = where.this
            assert isinstance(bounds, exp.Between)
            return _duck(
                "list_slice({a}, {lo} + 1, {hi} + 1)",
                a=unnest.expressions[0],
                lo=bounds.args["low"],
                hi=bounds.args["high"],
            )
    return node
