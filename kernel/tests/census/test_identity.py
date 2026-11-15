"""連線是否依檢視者的身分：檢視者用自己的資料庫帳號時，移轉後資料庫端的權限不會生效（平台以唯讀服務帳號
連線），列為不符合起步條件或需注意；Initial SQL 帶入登入者時平台代入平台登入的帳號，列為需注意。"""

from __future__ import annotations

from pathlib import Path

from tmig.census import render_markdown, run_census
from tmig.census.collect import IDENTITY_KEY, identity_check
from tmig.census.model import CensusReport, Check, DatasourceInfo, ServerConnection

PROBLEMS = Path(__file__).parent / "data" / "problems.twb"
ORACLE = "authentication='username-password' class='oracle' dbname='' one-time-sql=''"
LIVE = "Sales (Oracle)"


def census(
    tmp_path: Path, replacement: str, server_connections: list[ServerConnection] | None = None
) -> CensusReport:
    """problems.twb 的 Oracle Live 連線換成指定的屬性（Targets 是 Published Data Source）。"""
    text = PROBLEMS.read_text(encoding="utf-8")
    assert ORACLE in text
    path = tmp_path / "identity.twb"
    path.write_text(text.replace(ORACLE, replacement), encoding="utf-8")
    return run_census(path, server_connections=server_connections or ())


def check(report: CensusReport) -> Check:
    return next(c for c in report.checks if c.key == IDENTITY_KEY)


def live(report: CensusReport) -> DatasourceInfo:
    return next(d for d in report.datasources if d.caption == LIVE)


def info(
    caption: str, *, live: bool = True, classes: tuple[str, ...] = ("oracle",), **kw: object
) -> DatasourceInfo:
    return DatasourceInfo.model_validate(
        {
            "name": f"federated.{caption}",
            "caption": caption,
            "published": False,
            "connection_classes": list(classes),
            "data_in_hyper": not live,
            "extract_enabled": None if live else True,
            "extract_files": [] if live else ["Data/Extracts/x.hyper"],
            "extract_tables": [] if live else ["[Extract].[Extract]"],
            "source_relation_types": ["table"],
            "logical_tables": 0,
            "custom_sql": False,
            "field_count": 1,
            "calc_count": 0,
            "groups_or_sets": 0,
            "datasource_filters": 0,
            "extract_filters": 0,
            "filter_user_functions": [],
            **kw,
        }
    )


def server(type_: str = "oracle", *, embed: bool | None = False, **kw: object) -> ServerConnection:
    return ServerConnection.model_validate({"type": type_, "embed_password": embed, **kw})


# ---------------------------------------------------------------- 只看 .twb


def test_initial_sql_with_server_user_warns(tmp_path: Path) -> None:
    """Initial SQL 用到登入者：平台會執行、代入平台登入的帳號，列為需注意（帳號要與資料庫端認得的相同）。"""
    vpd = ORACLE.replace(
        "one-time-sql=''",
        "one-time-sql='BEGIN DBMS_SESSION.SET_IDENTIFIER([TableauServerUser]); END;'",
    )
    report = census(tmp_path, vpd)
    ds = live(report)
    assert ds.initial_sql and ds.initial_sql_user == ["[TableauServerUser]"]
    result = check(report)
    assert result.status == "warn"
    assert result.detail.startswith(
        "「Sales (Oracle)」的 Initial SQL 用到 [TableauServerUser]：平台開啟連線時會執行、代入平台登入的帳號"
    )
    assert "請確認平台的帳號與資料庫端認得的身分相同" in result.detail
    assert "Initial SQL（[TableauServerUser]）" in render_markdown(report)


def test_other_initial_sql_passes(tmp_path: Path) -> None:
    """其他的 Initial SQL 平台照樣執行，不依賴檢視者的身分；連線欄仍註明有 Initial SQL。"""
    nls = ORACLE.replace("one-time-sql=''", "one-time-sql='ALTER SESSION SET NLS_SORT = BINARY'")
    report = census(tmp_path, nls)
    assert check(report).status == "pass"
    assert "Initial SQL" in render_markdown(report)


def test_prompt_user_fails(tmp_path: Path) -> None:
    report = census(tmp_path, f"{ORACLE} workgroup-auth-mode='prompt'")
    assert live(report).server_auth_modes == ["prompt"]
    result = check(report)
    assert result.status == "fail"
    assert "要求檢視者輸入自己的資料庫帳號" in result.detail
    assert "Tableau Server：prompt" in render_markdown(report)

    # 嵌入帳號密碼（as-is）是一般情況，不列
    embedded = census(tmp_path, f"{ORACLE} workgroup-auth-mode='as-is'")
    assert live(embedded).server_auth_modes == []
    assert check(embedded).status == "pass"


def test_integrated_and_oauth_warn(tmp_path: Path) -> None:
    sspi = check(census(tmp_path, ORACLE.replace("username-password", "sspi")))
    assert sspi.status == "warn"
    assert "以 Windows 整合驗證或 Kerberos（sspi）連線" in sspi.detail
    assert "Kerberos 委派" in sspi.detail

    oauth = check(census(tmp_path, ORACLE.replace("username-password", "oauth")))
    assert oauth.status == "warn"
    assert "以 OAuth（oauth）連線" in oauth.detail

    # 伺服器上確認嵌入了登入資訊：不是檢視者自己登入
    embedded = census(
        tmp_path,
        ORACLE.replace("username-password", "oauth"),
        server_connections=[server(datasource=LIVE, embed=True)],
    )
    result = check(embedded)
    assert result.status == "pass"
    assert result.detail == "已比對 Tableau Server 的連線設定：Live 連線都嵌入了帳號密碼"


def test_plain_live_connection_passes(tmp_path: Path) -> None:
    result = check(census(tmp_path, ORACLE))
    assert result.status == "pass"
    assert "是否嵌入密碼請以伺服器盤點" in result.detail


# ---------------------------------------------------------------- 比對 Tableau Server 的連線設定


def test_server_without_embedded_password_fails(tmp_path: Path) -> None:
    report = census(tmp_path, ORACLE, server_connections=[server(datasource=LIVE)])
    result = check(report)
    assert result.status == "fail"
    assert result.detail == (
        "「Sales (Oracle)」在 Tableau Server 上沒有嵌入密碼，檢視者用自己的資料庫帳號連線，"
        "資料庫端的權限隨人不同；移轉後改用唯讀服務帳號連線，每個人會看到相同的資料"
    )
    # 名稱對不到時以連線類型對應（只有一個 Oracle 的 Live 資料源）
    by_type = census(tmp_path, ORACLE, server_connections=[server(datasource="其他名稱")])
    assert check(by_type).status == "fail"


def test_server_prompt_is_listed_once(tmp_path: Path) -> None:
    report = census(
        tmp_path,
        f"{ORACLE} workgroup-auth-mode='prompt'",
        server_connections=[server(datasource=LIVE)],
    )
    assert check(report).detail.count("Sales (Oracle)") == 1


def test_server_embedded_overrides_twb_prompt(tmp_path: Path) -> None:
    """.twb 記錄的是發佈時的設定；伺服器上之後嵌入了密碼（目前的狀態）時不列。"""
    report = census(
        tmp_path,
        f"{ORACLE} workgroup-auth-mode='prompt'",
        server_connections=[server(datasource=LIVE, embed=True)],
    )
    assert check(report).status == "pass"
    # 以檢視者身分模擬本來就嵌入了有模擬權限的帳號：仍不符合
    impersonate = census(
        tmp_path,
        f"{ORACLE} workgroup-auth-mode='impersonate'",
        server_connections=[server(datasource=LIVE, embed=True)],
    )
    assert check(impersonate).status == "fail"
    assert "以檢視者的身分模擬（impersonate）" in check(impersonate).detail


def test_server_windows_auth_only_warns(tmp_path: Path) -> None:
    """Windows 整合驗證沒有密碼可嵌入：伺服器以 Run As 服務帳號或檢視者的身分（Kerberos 委派）連線。"""
    report = census(
        tmp_path,
        ORACLE.replace("username-password", "sspi"),
        server_connections=[server(datasource=LIVE)],
    )
    result = check(report)
    assert result.status == "warn"
    assert result.detail.count("Sales (Oracle)") == 1

    by_rest = identity_check([info("A")], [server(datasource="A", authentication="sspi")])
    assert by_rest.status == "warn" and "Kerberos 委派" in by_rest.detail


def test_extracts_and_files_are_not_viewer_dependent() -> None:
    extract = info("Orders", live=False)
    # 萃取的來源連線沒有嵌入密碼：檢視時讀萃取檔，與檢視者的帳號無關
    result = identity_check([extract], [server(datasource="Orders")])
    assert result.status == "pass"
    assert result.detail == "沒有 Live 連線（萃取的資料不依賴檢視者的帳號）"
    files = identity_check([info("Sheet", classes=("excel-direct",))], [server("excel-direct")])
    assert files.status == "pass"
    no_auth = identity_check([info("A")], [server(authentication="auth-none")])
    assert no_auth.status == "pass"


def test_extract_source_initial_sql_warns() -> None:
    extract = info("Orders", live=False, initial_sql=True, initial_sql_user=["[TableauServerUser]"])
    result = identity_check([extract])
    assert result.status == "warn"
    assert result.detail.startswith("「Orders」的萃取來源有 Initial SQL 用到 [TableauServerUser]")


def test_ambiguous_server_connection_warns() -> None:
    result = identity_check([info("A"), info("B")], [server(datasource="C")])
    assert result.status == "warn"
    assert result.detail.startswith(
        "Tableau Server 上「C」的 oracle 連線沒有嵌入密碼，無法確定是哪個資料源"
    )
    named = identity_check([info("A"), info("B")], [server(datasource="b")])
    assert named.status == "fail" and named.detail.startswith("「B」在 Tableau Server 上")


def test_published_datasource_connections() -> None:
    """併入的 Published Data Source：以報表中的資料源名稱對應伺服器上資料源自己的連線。"""
    merged = info("Targets", classes=("databricks",))
    result = identity_check(
        [info("A"), merged],
        [server("databricks", datasource="Targets PDS", published_datasource="Targets")],
    )
    assert result.status == "fail"
    assert result.detail.startswith("「Targets」在 Tableau Server 上沒有嵌入密碼")
