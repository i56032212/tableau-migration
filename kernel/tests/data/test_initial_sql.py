"""連線的 Initial SQL（Tableau 的「初始 SQL」）：參數的代入、<ServerOnly>、拆成敘述，以及開啟連線時執行。

- DuckDB 檔模擬的來源：Initial SQL 建立的巨集與變數在之後的查詢中可以用；用到登入者時每個登入者各自一條連線
- PostgreSQL、MySQL 實跑（沒有時略過；TMIG_REQUIRE_ENGINES=1 時改為失敗）：設定工作階段、建立暫存資料表、
  自訂 SQL 讀到 Initial SQL 設定的登入者，之後的查詢仍是唯讀
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tmig.data import initial_sql
from tmig.data.initial_sql import Session, resolve, session_key, statements, user_parameters
from tmig.data.runtime import DataRuntime
from tmig.data.sources import ConnectionConfig, SourceConnectionError, open_executor
from tmig.ir import LiveConnection

from ..engines import mysql_connect, mysql_server, pg_connect, postgres_cluster

AMY = Session(username="amy", full_username="corp\\amy", workbook="Sales Report")
BOB = Session(username="bob", workbook="Sales Report")


def live(connection_id: str, dialect: str, sql: str | None) -> LiveConnection:
    dbclass = {"duckdb": "postgres", "postgres": "postgres", "mysql": "mysql"}[dialect]
    return LiveConnection(
        kind="live",
        connection_id=connection_id,
        dbclass=dbclass,
        dialect="postgres" if dialect == "duckdb" else dialect,  # type: ignore[arg-type]
        initial_sql=sql,
    )


# ---------------------------------------------------------------- 代入參數與拆成敘述


def test_parameters_and_server_only() -> None:
    sql = (
        "SET app.user = '[TableauServerUser]'; "
        "<ServerOnly>CALL audit('[tableauserveruserfull]', '[WorkbookName]', "
        "'[TableauApp] [TableauVersion]');</ServerOnly> SELECT [dbo].[Orders]"
    )
    assert resolve(sql, AMY) == (
        "SET app.user = 'amy'; CALL audit('corp\\amy', 'Sales Report', "
        "'Tableau Server 2021.2'); SELECT [dbo].[Orders]"
    )
    # 桌面版：不執行 <ServerOnly> 的部分；沒有登入伺服器時帳號是空的
    desktop = Session(app=initial_sql.APP_DESKTOP, server=False)
    assert resolve(sql, desktop) == "SET app.user = '';  SELECT [dbo].[Orders]"
    # 值中的單引號重複一次：不會跳出字串常值
    quoted = Session(username="o'brien'; DROP TABLE x; --")
    assert resolve("SET app.user = '[TableauServerUser]'", quoted) == (
        "SET app.user = 'o''brien''; DROP TABLE x; --'"
    )
    # 以反斜線跳脫的方言（MySQL、BigQuery 等）：反斜線也跳脫，帶網域的帳號原樣送到、結尾的反斜線不會吃掉引號
    tricky = Session(username="amy\\", full_username="corp\\amy's")
    text = "SET @u = '[TableauServerUser]', @f = '[TableauServerUserFull]'"
    assert resolve(text, tricky, "mysql") == "SET @u = 'amy\\\\', @f = 'corp\\\\amy''s'"
    assert resolve(text, tricky, "bigquery") == "SET @u = 'amy\\\\', @f = 'corp\\\\amy\\'s'"
    assert resolve(text, tricky, "postgres") == "SET @u = 'amy\\', @f = 'corp\\amy''s'"
    assert resolve(text, tricky, "oracle") == resolve(text, tricky) == resolve(text, tricky, "tsql")
    # 沒有網域時 [TableauServerUserFull] 與帳號相同
    assert resolve("[TableauServerUserFull]", BOB) == "bob"
    assert user_parameters(sql) == ["[TableauServerUserFull]", "[TableauServerUser]"]
    assert user_parameters("ALTER SESSION SET NLS_SORT = BINARY") == []


def test_split_into_statements() -> None:
    text = (
        "SET a = 'x;y'; -- 註解; 不算\n"
        'CREATE TEMP TABLE t AS SELECT 1 AS "a;b";\n'
        "DO $$ BEGIN PERFORM 1; END $$;;  /* 結尾; */ "
    )
    assert statements(text, "postgres") == [
        "SET a = 'x;y'",
        '-- 註解; 不算\nCREATE TEMP TABLE t AS SELECT 1 AS "a;b"',
        "DO $$ BEGIN PERFORM 1; END $$",
    ]
    # Oracle 的 PL/SQL 區塊整段送出（保留結尾的分號）；一般的敘述依分號拆開
    block = "BEGIN DBMS_SESSION.SET_IDENTIFIER('amy'); END;"
    assert statements(block, "oracle") == [block]
    assert statements("-- 設定身分\n" + block, "oracle") == ["-- 設定身分\n" + block]
    assert statements("ALTER SESSION SET x = 1; ALTER SESSION SET y = 2;", "oracle") == [
        "ALTER SESSION SET x = 1",
        "ALTER SESSION SET y = 2",
    ]
    # T-SQL 整段是一個批次（變數只在同一個批次裡有效）
    batch = "DECLARE @u sysname = 'amy'; EXEC sp_set_session_context 'u', @u;"
    assert statements(batch, "tsql") == [batch]
    # MySQL：反斜線跳脫與 # 註解
    assert statements("SET @a = 'it\\'s; ok'; # 註解; x\nSET @b = 2", "mysql") == [
        "SET @a = 'it\\'s; ok'",
        "# 註解; x\nSET @b = 2",
    ]
    assert statements("  ;; -- 只有註解\n", "postgres") == []


def test_session_key_separates_users_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = live("db", "postgres", "SET search_path TO sales")
    personal = live("db", "postgres", "SET app.user = '[TableauServerUser]'")
    assert session_key(live("db", "postgres", None), AMY) == ""
    assert session_key(plain, AMY) == session_key(plain, BOB) != ""
    assert session_key(personal, AMY) != session_key(personal, BOB)
    with initial_sql.use(AMY):
        assert session_key(personal) == session_key(personal, AMY)
    assert initial_sql.current() == Session()  # 沒有設定時：沒有登入者的伺服器
    # 停用（Tableau Server 的 vizqlserver.initialsql.disabled）：不送出任何 Initial SQL
    monkeypatch.setenv("TMIG_INITIAL_SQL", "off")
    assert initial_sql.resolved(personal, AMY) == "" and session_key(personal, AMY) == ""


# ---------------------------------------------------------------- DuckDB 檔模擬的來源


@pytest.fixture
def source(tmp_path: Path) -> dict[str, ConnectionConfig]:
    path = tmp_path / "source.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES ('amy', 10), ('bob', 20)) t(owner, amount)"
    )
    con.close()
    return {"db": ConnectionConfig("db", "duckdb", {"driver": "duckdb", "path": str(path)})}


def test_initial_sql_runs_when_the_connection_opens(source: dict[str, ConnectionConfig]) -> None:
    sql = (
        "CREATE TEMP MACRO viewer() AS '[TableauServerUser]';\n"
        "CREATE TEMP TABLE mine AS SELECT * FROM orders WHERE owner = '[TableauServerUser]';"
    )
    connection = live("db", "duckdb", sql)
    with DataRuntime(source) as runtime:
        with initial_sql.use(AMY):
            assert runtime.fetch(connection, "SELECT viewer() AS v").to_pylist() == [{"v": "amy"}]
            assert runtime.fetch(connection, "SELECT amount FROM mine").to_pylist() == [
                {"amount": 10}
            ]
        # 另一個登入者：自己的連線，不會看到前一個人的工作階段
        with initial_sql.use(BOB):
            assert runtime.fetch(connection, "SELECT amount FROM mine").to_pylist() == [
                {"amount": 20}
            ]
        with initial_sql.use(AMY):
            assert runtime.fetch(connection, "SELECT amount FROM mine").to_pylist() == [
                {"amount": 10}
            ]
        assert len(runtime._executors) == 2  # 每個登入者一條，重複使用
        # 沒有登入者（排程、指令列）：帳號是空字串
        assert runtime.fetch(connection, "SELECT viewer() AS v").to_pylist() == [{"v": ""}]
    # 沒有 Initial SQL 的連線照舊
    with DataRuntime(source) as runtime:
        plain = live("db", "duckdb", None)
        assert runtime.fetch(plain, "SELECT count(*) AS n FROM orders").to_pylist() == [{"n": 2}]
        assert list(runtime._executors) == ["db"]


def test_initial_sql_failure_and_disabling(
    source: dict[str, ConnectionConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = live("db", "duckdb", "SET search_path = 'main'; SELECT * FROM no_such_table")
    with pytest.raises(SourceConnectionError, match="連線 db 的 Initial SQL 執行失敗"):
        open_executor(broken, source)
    with DataRuntime(source) as runtime:
        with pytest.raises(SourceConnectionError, match="Initial SQL 執行失敗"):
            runtime.fetch(broken, "SELECT 1")
        assert runtime._executors == {}  # 沒有留下開到一半的連線
        # 停用之後不送出 Initial SQL，連線照常
        monkeypatch.setenv("TMIG_INITIAL_SQL", "off")
        assert runtime.fetch(broken, "SELECT 1 AS one").to_pylist() == [{"one": 1}]


def test_sessions_per_connection_are_limited(
    source: dict[str, ConnectionConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """每個登入者一條連線：太多時關掉最久沒用的（TMIG_SESSION_CONNECTIONS）。"""
    monkeypatch.setenv("TMIG_SESSION_CONNECTIONS", "2")
    connection = live("db", "duckdb", "CREATE TEMP MACRO viewer() AS '[TableauServerUser]'")
    with DataRuntime(source) as runtime:
        for name in ("amy", "bob", "cid", "amy", "dee"):
            with initial_sql.use(Session(username=name)):
                found = runtime.fetch(connection, "SELECT viewer() AS v").to_pylist()
                assert found == [{"v": name}]
            assert len(runtime._executors) <= 2
        keys = set(runtime._executors)
        assert keys == {
            "db\x1f" + session_key(connection, Session(username=name)) for name in ("amy", "dee")
        }


# ---------------------------------------------------------------- PostgreSQL、MySQL 實跑


@pytest.fixture(scope="module")
def postgres() -> Iterator[dict[str, ConnectionConfig]]:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    with postgres_cluster() as dsn:
        admin = pg_connect(dsn)
        admin.execute("DROP DATABASE IF EXISTS initsql")
        admin.execute("CREATE DATABASE initsql")
        admin.close()
        con = pg_connect(make_conninfo(dsn, dbname="initsql"))
        con.execute("CREATE SCHEMA sales")
        con.execute("CREATE TABLE sales.orders (owner TEXT, amount INTEGER)")
        con.execute("INSERT INTO sales.orders VALUES ('amy', 10), ('bob', 20), ('amy', 5)")
        con.close()
        info = conninfo_to_dict(dsn)
        params: dict[str, Any] = {
            k: v for k, v in info.items() if k in ("host", "port", "user", "password")
        }
        yield {"pg": ConnectionConfig("pg", "postgres", {**params, "database": "initsql"})}


def test_postgres_initial_sql(postgres: dict[str, ConnectionConfig]) -> None:
    sql = (
        "SET search_path TO sales;\n"
        "SELECT set_config('app.viewer', '[TableauServerUser]', false);\n"
        "CREATE TEMP TABLE mine AS SELECT * FROM orders WHERE owner = '[TableauServerUser]';"
    )
    connection = live("pg", "postgres", sql)
    with DataRuntime(postgres) as runtime:
        with initial_sql.use(AMY):
            # search_path 生效（不必寫 sales.）、暫存資料表建立了、工作階段的設定讀得到
            total = runtime.fetch(connection, "SELECT sum(amount) AS total FROM mine").to_pylist()
            assert total == [{"total": 15}]
            who = runtime.fetch(connection, "SELECT current_setting('app.viewer') AS v")
            assert who.to_pylist() == [{"v": "amy"}]
            assert runtime.fetch(connection, "SELECT count(*) AS n FROM orders").to_pylist() == [
                {"n": 3}
            ]
            # 之後的查詢仍是唯讀：不能寫入
            with pytest.raises(Exception, match="read-only"):
                runtime.fetch(connection, "INSERT INTO sales.orders VALUES ('x', 1) RETURNING 1")
        with initial_sql.use(BOB):
            total = runtime.fetch(connection, "SELECT sum(amount) AS total FROM mine").to_pylist()
            assert total == [{"total": 20}]
    with pytest.raises(SourceConnectionError, match="Initial SQL 執行失敗"):
        open_executor(live("pg", "postgres", "SELECT * FROM no_such_table"), postgres)


@pytest.fixture(scope="module")
def mysql() -> Iterator[dict[str, ConnectionConfig]]:
    with mysql_server() as params:
        con = mysql_connect(params, "initsql")
        with con.cursor() as cursor:
            cursor.execute("CREATE TABLE orders (owner VARCHAR(20), amount INT)")
            cursor.execute("INSERT INTO orders VALUES ('amy', 10), ('bob', 20), ('amy', 5)")
        con.close()
        config = {k: v for k, v in params.items() if k in ("host", "port", "user", "password")}
        if "unix_socket" in params:
            config["socket"] = params["unix_socket"]
        yield {"my": ConnectionConfig("my", "mysql", {**config, "database": "initsql"})}


def test_mysql_initial_sql(mysql: dict[str, ConnectionConfig]) -> None:
    sql = (
        "SET @viewer = '[TableauServerUser]', @full = '[TableauServerUserFull]';\n"
        "SELECT 1;\n"
        "CREATE TEMPORARY TABLE mine AS SELECT * FROM orders WHERE owner = '[TableauServerUser]';"
    )
    connection = live("my", "mysql", sql)
    with DataRuntime(mysql) as runtime:
        with initial_sql.use(AMY):
            total = runtime.fetch(connection, "SELECT SUM(amount) AS total FROM mine").to_pylist()
            assert [int(r["total"]) for r in total] == [15]
            # 帶網域的帳號中的反斜線在 MySQL 是跳脫字元：代入時跳脫，資料庫看到的是原本的值
            who = runtime.fetch(connection, "SELECT @viewer AS v, @full AS f").to_pylist()
            assert who == [{"v": "amy", "f": "corp\\amy"}]
        # 帳號含單引號、結尾是反斜線：不會跳出字串常值
        odd = Session(username="o'brien\\")
        with initial_sql.use(odd):
            who = runtime.fetch(connection, "SELECT @viewer AS v").to_pylist()
            assert who == [{"v": "o'brien\\"}]
        with initial_sql.use(BOB):
            total = runtime.fetch(connection, "SELECT SUM(amount) AS total FROM mine").to_pylist()
            assert [int(r["total"]) for r in total] == [20]


# ---------------------------------------------------------------- 在編輯器設定


def _with_connection(connection: Any) -> Any:
    """功能報表的資料源換成這個連線。"""
    from tmig.ir import load_ir

    root = Path(__file__).resolve().parents[3]
    ir = load_ir(root / "fixtures" / "superstore_features" / "golden.ir.json")
    first = ir.datasources[0].model_copy(update={"connection": connection})
    return ir.model_copy(update={"datasources": [first, *ir.datasources[1:]]}), first.id


def test_set_initial_sql_in_the_editor() -> None:
    from tmig.authoring import initial_sql_connections, set_initial_sql
    from tmig.edit import EditError
    from tmig.extract_edit import switch_to_extract
    from tmig.ir import FileColumn, FileConnection, FileTable, TableRelation
    from tmig.ir.federation import to_federated

    relation = TableRelation(kind="table", name="Orders", table=["Extract", "Extract"])
    base = live("sales", "duckdb", None).model_copy(update={"relation": relation})
    ir, ds = _with_connection(base)

    # Live 連線：設定、前後的空白去掉；空的表示清除
    changed = set_initial_sql(ir, ds, "  SET search_path TO crm;\n")
    [found] = initial_sql_connections(changed.datasources[0])
    assert found.initial_sql == "SET search_path TO crm;"
    assert found.relation == relation
    cleared = set_initial_sql(changed, ds, "   ")
    assert initial_sql_connections(cleared.datasources[0])[0].initial_sql is None

    # 改成萃取之後 Initial SQL 留在來源連線（更新萃取時執行），仍然可以修改
    extract = switch_to_extract(changed, ds)
    [source] = initial_sql_connections(extract.datasources[0])
    assert source.initial_sql == "SET search_path TO crm;"
    updated = set_initial_sql(extract, ds, "SET ROLE reader", "sales")
    assert initial_sql_connections(updated.datasources[0])[0].initial_sql == "SET ROLE reader"

    # 跨資料庫的 join：同一個資料庫的每張資料表一起改；另一個資料庫不變，有多個時要指定
    columns = [FileColumn(name="Region", datatype="string")]
    single = to_federated(base.model_copy(update={"initial_sql": "SET x = 1"}), lambda _: columns)
    [orders] = single.tables
    assert orders.connection is not None and orders.connection.initial_sql == "SET x = 1"
    other = live("crm", "duckdb", "SET x = 1")
    tables = [
        orders,
        orders.model_copy(update={"name": "Returns", "table": ["Extract", "Returns"]}),
        orders.model_copy(update={"name": "People", "path": "live:crm", "connection": other}),
    ]
    federated, fed = _with_connection(single.model_copy(update={"tables": tables}))
    with pytest.raises(EditError, match="有多個資料庫連線"):
        set_initial_sql(federated, fed, "SET y = 2")
    with pytest.raises(EditError, match="沒有連線 nope"):
        set_initial_sql(federated, fed, "SET y = 2", "nope")
    both = set_initial_sql(federated, fed, "SET y = 2", "sales")
    connection = both.datasources[0].connection
    assert isinstance(connection, FileConnection)
    assert [t.connection.initial_sql for t in connection.tables if t.connection] == [
        "SET y = 2",
        "SET y = 2",
        "SET x = 1",
    ]

    # 沒有資料庫連線的資料源（匯入的檔案）不能設定
    local = FileTable(name="Orders", path="Data/orders.csv", format="text", columns=columns)
    files, fid = _with_connection(single.model_copy(update={"tables": [local]}))
    with pytest.raises(EditError, match="沒有資料庫連線"):
        set_initial_sql(files, fid, "SET z = 3")
