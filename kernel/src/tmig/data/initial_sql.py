"""連線的 Initial SQL（Tableau 的「初始 SQL」，.twb 連線的 ``one-time-sql``）。

Tableau 在連上資料庫時執行一次，用來設定工作階段（例如 ``SET search_path``、``ALTER SESSION``）、建立這次
工作階段用的暫存資料表，或把登入者帶給資料庫（Oracle VPD 的 ``DBMS_SESSION.SET_IDENTIFIER``、SQL Server 的
``EXECUTE AS USER``）。平台在開啟來源連線時執行（檢視、篩選控制項的可選值、更新萃取、測試連線都一樣）。

- **參數**（與 Tableau 相同的五個，名稱不分大小寫）：``[TableauServerUser]`` 登入者的帳號、
  ``[TableauServerUserFull]`` 帶網域的帳號（網域\\帳號；沒有網域時與帳號相同）、``[TableauApp]`` 應用程式的名稱、
  ``[TableauVersion]`` 版本、``[WorkbookName]`` 報表的名稱。值依連線的方言跳脫（單引號重複一次；MySQL、BigQuery
  等以反斜線跳脫的方言連反斜線一起），放在字串常值裡不會被截斷。
  沒有登入者時（排程的萃取更新、指令列）帳號是空字串，與 Tableau 沒有登入伺服器時相同
- **<ServerOnly>…</ServerOnly>**：只在伺服器上執行的部分；桌面版不執行（與 Tableau 相同）
- **用到登入者的 Initial SQL**：每個登入者各自一條連線（見 session_key），取回的資料也不共用
- **停用**：環境變數 ``TMIG_INITIAL_SQL=off`` 時不送出任何 Initial SQL（Tableau Server 的
  ``vizqlserver.initialsql.disabled``），報表照常開啟

平台不檢查 Initial SQL 的內容，原樣送給資料庫（與 Tableau 相同）。它是報表作者寫的 SQL：請用只有需要的權限的
服務帳號連線。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from tmig.ir.models import LiveConnection

ENABLE_ENV = "TMIG_INITIAL_SQL"
APP_SERVER = "Tableau Server"
APP_DESKTOP = "Tableau Desktop Professional"
"""[TableauApp] 的值：沿用 Tableau 的名稱，依它判斷環境的 Initial SQL 才會照原本的方式執行。"""
VERSION = "2021.2"
"""[TableauVersion] 的值：平台對照的 Tableau 版本。"""

SERVER_ONLY = re.compile(r"<ServerOnly>(.*?)</ServerOnly>", re.IGNORECASE | re.DOTALL)
PARAMETER = re.compile(
    r"\[(TableauServerUserFull|TableauServerUser|TableauApp|TableauVersion|WorkbookName)\]",
    re.IGNORECASE,
)
USER_PARAMETERS = frozenset({"tableauserveruser", "tableauserveruserfull"})
BATCH_DIALECTS = frozenset({"tsql", "sqlserver", "sybase"})
"""整段當成一個批次送出的方言：T-SQL 的變數（DECLARE @x）只在同一個批次裡有效，不能依分號拆開。"""
BLOCK_DIALECTS = frozenset({"oracle", "db2", "hana", "teradata"})
"""以 BEGIN、DECLARE 開頭的程序區塊（PL/SQL 等）整段送出的方言。"""


@dataclass(frozen=True)
class Session:
    """執行 Initial SQL 時代入參數的環境：誰在看、在哪個應用程式、哪一份報表。"""

    username: str = ""
    full_username: str = ""
    app: str = APP_SERVER
    workbook: str = ""
    server: bool = True
    """伺服器（True）或桌面版：桌面版不執行 <ServerOnly> 的部分。"""

    def value(self, name: str) -> str:
        key = name.lower()
        if key == "tableauserveruser":
            return self.username
        if key == "tableauserveruserfull":
            return self.full_username or self.username
        if key == "tableauapp":
            return self.app
        if key == "tableauversion":
            return VERSION
        return self.workbook


def server_session(username: str = "", domain: str | None = None, workbook: str = "") -> Session:
    """伺服器上某位登入者（沒有登入者時是空的帳號，例如排程的萃取更新）看某份報表的環境。"""
    full = f"{domain}\\{username}" if domain and username else username
    return Session(username=username, full_username=full, workbook=workbook)


_SESSION: ContextVar[Session | None] = ContextVar("tmig_initial_sql_session", default=None)


def current() -> Session:
    """目前的環境；沒有設定時（排程、指令列）是沒有登入者的伺服器。"""
    return _SESSION.get() or Session()


def set_session(session: Session | None) -> None:
    """設定這次請求（這個執行緒目前的 context）的環境。"""
    _SESSION.set(session)


@contextmanager
def use(session: Session | None) -> Iterator[None]:
    """在這段程式中使用這個環境，離開時還原。"""
    token = _SESSION.set(session)
    try:
        yield
    finally:
        _SESSION.reset(token)


def enabled() -> bool:
    """有沒有停用 Initial SQL（TMIG_INITIAL_SQL=off）。"""
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
        "disabled",
    )


def user_parameters(sql: str) -> list[str]:
    """Initial SQL 中代表登入者的參數（依 Tableau 的寫法，例如 [TableauServerUser]）。"""
    names = {
        "tableauserveruser": "[TableauServerUser]",
        "tableauserveruserfull": "[TableauServerUserFull]",
    }
    return sorted(
        {names[m.group(1).lower()] for m in PARAMETER.finditer(sql) if m.group(1).lower() in names}
    )


def literal_text(value: str, dialect: str | None = None) -> str:
    """值放在該方言的字串常值（單引號之間）時的寫法：單引號重複一次；以反斜線跳脫的方言（MySQL、BigQuery、
    Spark、Hive、Databricks、Snowflake、Redshift）依它的寫法跳脫單引號與反斜線。登入者的帳號或報表的名稱
    含這些字元時不會跳出字串常值，資料庫看到的是原本的值。"""
    from sqlglot import exp

    from tmig.sqlgen.dialect import SQLGLOT_NAMES

    name = SQLGLOT_NAMES.get(dialect or "")
    if name is None:
        return value.replace("'", "''")
    return exp.Literal.string(value).sql(dialect=name)[1:-1]


def resolve(sql: str, session: Session | None = None, dialect: str | None = None) -> str:
    """代入參數、處理 <ServerOnly> 之後要送給資料庫的 Initial SQL（可能是空字串）。dialect：連線的方言
    （參數的值依它跳脫，見 literal_text）。"""
    context = session or current()
    text = SERVER_ONLY.sub(lambda m: m.group(1) if context.server else "", sql)
    text = PARAMETER.sub(lambda m: literal_text(context.value(m.group(1)), dialect), text)
    return text.strip()


def resolved(connection: LiveConnection, session: Session | None = None) -> str:
    """這個連線這次要執行的 Initial SQL；沒有設定、停用或代入後是空的時為空字串。"""
    if not connection.initial_sql or not enabled():
        return ""
    return resolve(connection.initial_sql, session, connection.dialect)


def session_key(connection: LiveConnection, session: Session | None = None) -> str:
    """分辨連線用的鍵：Initial SQL 代入參數後的內容的雜湊（沒有 Initial SQL 時為空字串）。
    內容相同的請求共用一條連線；用到登入者的 Initial SQL 每個登入者各自一條，取回的資料也依它分開。"""
    text = resolved(connection, session)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def statements(sql: str, dialect: str) -> list[str]:
    """把 Initial SQL 拆成一次送一句的敘述：依最外層的分號拆開（字串、識別字、註解與 PostgreSQL 的
    $…$ 引號中的分號不算），空的敘述與只有註解的敘述去掉。

    - T-SQL（SQL Server）整段是一個批次（變數只在同一個批次裡有效）
    - Oracle、Db2 等以 BEGIN、DECLARE 開頭時是程序區塊（PL/SQL），整段送出、保留結尾的分號
    """
    text = sql.strip()
    if not text:
        return []
    if dialect in BATCH_DIALECTS:
        return [text]
    if dialect in BLOCK_DIALECTS and re.match(r"(?is)^(begin|declare)\b", _without_comments(text)):
        return [text]
    parts: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        if two == "--" or (ch == "#" and dialect == "mysql"):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif two == "/*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch in ("'", '"', "`"):
            i = _skip_quoted(
                text, i, ch, backslash=dialect in ("mysql", "bigquery", "hive", "spark")
            )
        elif ch == "$" and dialect in ("postgres", "redshift", "duckdb"):
            tag = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if tag is None:
                i += 1
            else:
                end = text.find(tag.group(0), i + len(tag.group(0)))
                i = n if end < 0 else end + len(tag.group(0))
        elif ch == ";":
            parts.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if _without_comments(p).strip()]


def _skip_quoted(text: str, start: int, quote: str, backslash: bool) -> int:
    """從引號開始，回傳結尾引號之後的位置（重複兩次的引號是跳脫；backslash 時反斜線也是）。"""
    i = start + 1
    n = len(text)
    while i < n:
        if backslash and text[i] == "\\":
            i += 2
        elif text[i] == quote:
            if text[i + 1 : i + 2] == quote:
                i += 2
            else:
                return i + 1
        else:
            i += 1
    return n


def _without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", text).strip()


__all__ = [
    "APP_DESKTOP",
    "APP_SERVER",
    "ENABLE_ENV",
    "Session",
    "current",
    "enabled",
    "literal_text",
    "resolve",
    "resolved",
    "server_session",
    "session_key",
    "set_session",
    "statements",
    "use",
    "user_parameters",
]
