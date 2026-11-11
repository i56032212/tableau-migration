"""桌面版發布資料源（與 Tableau Desktop 的「發布資料源」相同）：報表中的一個資料源存成資料源套件上傳；
報表改用發布的資料源後再發布報表，伺服器上的報表直接使用伺服器上的資料源的萃取。

伺服器與桌面版的本機引擎在同一個行程中執行（見 test_desktop_remote）。
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient
from test_desktop_remote import DEMO, ENGINE, SERVER, TOKEN, browse, engine_app, server_app

from tmig.ir import IR, ExtractConnection
from tmig.package import package_kind
from tmig_service.datasources import DatasourceStore


def signed_in(tmp_path: Path) -> tuple[TestClient, TestClient, object]:
    server = server_app(tmp_path)
    opened: list[str] = []
    app, _ = engine_app(tmp_path, server, opened)
    engine = TestClient(app, base_url=ENGINE, headers={"X-Tmig-Token": TOKEN})
    engine.post("/api/desktop/server/signin", json={"url": SERVER})
    assert browse(server, opened[-1], app).status_code == 200
    amy = TestClient(server)
    assert amy.post("/auth/dev", json={"username": "amy"}).status_code == 200
    return engine, amy, server


def test_publish_datasource_and_use_it(tmp_path: Path) -> None:
    engine, amy, server = signed_in(tmp_path)
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    document = engine.post("/api/desktop/open", json={"path": str(source)}).json()["document"]["id"]

    listed = engine.get("/api/desktop/server/datasources", params={"workbook": document})
    assert listed.status_code == 200, listed.text
    (orders,) = listed.json()["datasources"]
    assert (orders["id"], orders["connection"], orders["content_url"], orders["server"]) == (
        "ds_orders",
        "extract",
        None,
        None,
    )

    # 發布資料源，報表改用發布的資料源
    first = engine.post(
        "/api/desktop/server/publish-datasource",
        json={
            "workbook": document,
            "datasource": "ds_orders",
            "name": "Orders",
            "project": "default",
            "comment": "從桌面版發布",
        },
    )
    assert first.status_code == 200, first.text
    record = first.json()["datasource"]
    assert (record["name"], record["content_url"], record["version"], record["extract"]) == (
        "Orders",
        "Orders",
        1,
        True,
    )
    assert first.json()["linked"] is True and first.json()["address"] == f"{SERVER}/datasources"
    ir = IR.model_validate(engine.get(f"/api/workbooks/{document}/ir").json())
    linked = ir.datasources[0]
    assert linked.published is not None and linked.published.content_url == "Orders"
    assert isinstance(linked.connection, ExtractConnection)
    assert linked.connection.hyper_file.startswith("Data/Published/Orders/")
    documents = {d["id"]: d for d in engine.get("/api/desktop/documents").json()}
    assert documents[document]["modified"] is True  # 關閉前提醒儲存

    # 伺服器上的資料源：欄位與萃取都在；桌面版的清單顯示已經連到它
    stores = DatasourceStore(tmp_path / "store")
    stored = stores.get(record["id"])
    assert stored is not None and stored.latest.format == "tmigds"
    assert stores.ir(stored).datasources[0].published.content_url == "Orders"  # type: ignore[union-attr]
    count = 'SELECT count(*) FROM "Extract"."Extract"'
    own = tmp_path / "workspace" / document / "data" / "ds_orders.duckdb"
    with duckdb.connect(str(stores.data_path(stored)), read_only=True) as con:
        rows = con.execute(count).fetchone()
    with duckdb.connect(str(own), read_only=True) as con:
        assert rows == con.execute(count).fetchone() and rows is not None and rows[0] > 0
    detail = amy.get(f"/api/datasources/{record['id']}").json()
    assert detail["versions"][0]["comment"] == "從桌面版發布"
    assert any(f["caption"] == "Sales" for f in detail["fields"])
    (again,) = engine.get("/api/desktop/server/datasources", params={"workbook": document}).json()[
        "datasources"
    ]
    assert again["content_url"] == "Orders" and again["server"]["id"] == record["id"]

    # 同名再發布：伺服器回應同名；指定覆蓋那個資料源時為新的版本
    exists = engine.post(
        "/api/desktop/server/publish-datasource",
        json={
            "workbook": document,
            "datasource": "ds_orders",
            "name": "Orders",
            "project": "default",
        },
    )
    assert exists.status_code == 409 and exists.json()["detail"]["error"] == "exists"
    assert exists.json()["detail"]["datasource"]["id"] == record["id"]
    second = engine.post(
        "/api/desktop/server/publish-datasource",
        json={"workbook": document, "datasource": "ds_orders", "target": record["id"]},
    )
    assert second.status_code == 200, second.text
    assert (second.json()["datasource"]["id"], second.json()["datasource"]["version"]) == (
        record["id"],
        2,
    )
    download = amy.get(f"/api/datasources/{record['id']}/versions/2/download")
    assert download.status_code == 200
    assert 'filename="Orders.tmigds"' in download.headers["content-disposition"]
    package = tmp_path / "Orders.tmigds"
    package.write_bytes(download.content)
    assert package_kind(package) == "datasource"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "data/ds_orders.duckdb" in archive.namelist()

    # 發布報表：伺服器上的報表使用伺服器上的資料源（萃取更新一次，每份報表都看到新的資料）
    published = engine.post(
        "/api/desktop/server/publish",
        json={"workbook": document, "name": "Superstore", "project": "default"},
    )
    assert published.status_code == 200, published.text
    report = published.json()["report"]["id"]
    workspace = server.state.workspace  # type: ignore[attr-defined]
    stored = stores.get(record["id"])
    assert stored is not None
    assert workspace.database(report, "ds_orders") == stores.data_path(stored)
    used = amy.get(f"/api/datasources/{record['id']}").json()["used_by"]
    assert [u["id"] for u in used] == [report]
    sheet = ir.worksheets[0].id
    local = engine.post(f"/api/workbooks/{document}/worksheets/{sheet}/render", json={}).json()
    remote = amy.post(f"/api/workbooks/{report}/worksheets/{sheet}/render", json={}).json()
    assert remote["rows"] == local["rows"] > 0
    assert remote["table"]["rows"] == local["table"]["rows"]  # 數字相同

    # 伺服器上的萃取換成新的資料（萃取更新）：報表看到新的資料，桌面版仍是自己的那一份
    with duckdb.connect(str(stores.data_path(stored))) as con:
        con.execute('DELETE FROM "Extract"."Extract" WHERE "Region" <> \'West\'')
    refreshed = amy.post(f"/api/workbooks/{report}/worksheets/{sheet}/render", json={}).json()
    assert refreshed["table"]["rows"] != local["table"]["rows"]


def test_publish_without_link_and_names(tmp_path: Path) -> None:
    engine, amy, _ = signed_in(tmp_path)
    source = tmp_path / "demo.twbx"
    shutil.copy(DEMO, source)
    document = engine.post("/api/desktop/open", json={"path": str(source)}).json()["document"]["id"]
    # 名稱沒有英數字：網址名稱用資料源的 id，重複時加序號
    names = []
    for name in ("訂單明細", "訂單二"):
        result = engine.post(
            "/api/desktop/server/publish-datasource",
            json={
                "workbook": document,
                "datasource": "ds_orders",
                "name": name,
                "project": "default",
                "link": False,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["linked"] is False
        names.append(result.json()["datasource"]["content_url"])
    assert names == ["ds_orders", "ds_orders_2"]
    ir = engine.get(f"/api/workbooks/{document}/ir").json()
    assert ir["datasources"][0]["published"] is None  # 沒有改用發布的資料源
    missing = engine.post(
        "/api/desktop/server/publish-datasource",
        json={"workbook": document, "datasource": "nope", "name": "x"},
    )
    assert missing.status_code == 404

    # 伺服器：報表套件不是資料源；資料源套件也可以從網頁上傳
    report_package = tmp_path / "demo.tmigx"
    saved = engine.post(
        "/api/desktop/save", json={"workbook": document, "path": str(report_package)}
    )
    assert saved.status_code == 200, saved.text
    wrong = amy.post(
        "/api/datasources/publish",
        params={"name": "x"},
        content=report_package.read_bytes(),
    )
    assert wrong.status_code == 422 and "報表套件" in wrong.json()["detail"]
    first = DatasourceStore(tmp_path / "store").find("ds_orders")
    assert first is not None
    upload = amy.get(f"/api/datasources/{first.id}/versions/1/download").content
    nameless = amy.post("/api/datasources/publish", params={"project": "default"}, content=upload)
    assert nameless.status_code == 422 and "名稱" in nameless.json()["detail"]
    again = amy.post(
        "/api/datasources/publish",
        params={"name": "訂單明細", "project": "default", "overwrite": "true"},
        content=upload,
    )
    assert again.status_code == 200, again.text
    assert (again.json()["id"], again.json()["version"]) == (first.id, 2)
