"""從 Tableau Server 盤點報表、挑起步候選、下載選定的報表（S2；SDD「盤點與使用率」）。

只用 REST API；盤點與下載不需要管理員權限（看得到哪些報表，就盤點哪些），匯出使用者目錄需要站台管理員。

    # 1. 盤點：列出看得到的報表（含檢視次數），下載結構（不含萃取，很快）並逐份盤點
    uv run python tools/tableau_server/tableau_server.py inventory \\
        --server https://tableau.example.com --site sales --pat-name <PAT 名稱>

    # 2. 看 inventory/inventory.md 的 A 級、B 級候選，各挑一份下載（含萃取）到 fixtures
    uv run python tools/tableau_server/tableau_server.py fetch \\
        --server https://tableau.example.com --site sales --pat-name <PAT 名稱> \\
        --workbook "銷售總覽"

    # 3. 使用者目錄：使用者與群組（查詢服務的身分驗證依此對應，需要站台管理員）
    uv run python tools/tableau_server/tableau_server.py directory \\
        --server https://tableau.example.com --site sales --pat-name <PAT 名稱> --out directory.json

登入方式（擇一）：

- ``--pat-name``：個人存取權杖（建議）；密鑰從環境變數 TABLEAU_PAT_SECRET 讀取，沒有就提示輸入
- ``--user``：帳號密碼；密碼從環境變數 TABLEAU_PASSWORD 讀取，沒有就提示輸入
- ``--browser-session``：沿用 fetch_expected 的瀏覽器登入（適合只能 SSO 登入的環境；實驗性）

工具不會把密鑰、密碼或登入權杖寫進任何檔案。憑證以作業系統的信任清單驗證（公司內部 CA 也適用），
也可以用 ``--ca-bundle`` 指定 PEM 檔。

輸出（預設 inventory/，已列入 .gitignore）：

- ``workbooks/<Project>/<報表>.twbx``：不含萃取的報表檔，重跑時只重新下載有更新的報表；
  連到 Published Data Source 的報表，下載資料源的定義（datasources/，不含萃取）併入後再盤點，
  原本的報表在 original/
- ``server.json``：Project、擁有者、檢視次數、連線類型、下載失敗原因
- ``inventory.md``、``inventory.csv``、``features.csv``、``census/``：見 ``tmig inventory``
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import json
import os
import re
import ssl
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from tmig.ir.models import Zone

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path("inventory")
DEFAULT_PROFILE = Path.home() / ".tmig" / "tableau-profile"
FALLBACK_API_VERSION = "3.10"  # Tableau Server 2021.1
PAGE_SIZE = 1000
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.IGNORECASE)


class TableauError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------- REST API 用戶端


@dataclass
class Session:
    version: str
    token: str
    site_id: str
    site_url: str
    user_id: str | None = None


def _list(value: Any) -> list[dict[str, Any]]:
    """REST API 的 JSON：重複元素可能是陣列、單一物件或不存在。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def ssl_context(ca_bundle: Path | None) -> ssl.SSLContext:
    if ca_bundle is not None:
        return ssl.create_default_context(cafile=str(ca_bundle))
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover - truststore 是 dev 相依
        return ssl.create_default_context()


class TableauClient:
    def __init__(
        self,
        server: str,
        *,
        verify: ssl.SSLContext | bool = True,
        timeout: float = 120.0,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self.server = server.rstrip("/")
        self.page_size = page_size
        self.http = httpx.Client(
            base_url=self.server,
            verify=verify,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self.session: Session | None = None

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ 連線與登入

    def api_version(self) -> tuple[str | None, str]:
        """(Tableau Server 版本, REST API 版本)；查不到時用 2021.1 的 API 版本。"""
        for version in ("2.4", "3.0"):
            try:
                response = self.http.get(f"/api/{version}/serverinfo")
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                info = response.json().get("serverInfo", {})
            except ValueError:
                continue
            api = info.get("restApiVersion")
            product = (info.get("productVersion") or {}).get("value")
            if api:
                return product, str(api)
        return None, FALLBACK_API_VERSION

    def sign_in(
        self, site: str, *, secret: str, pat_name: str | None = None, user: str | None = None
    ) -> Session:
        _, version = self.api_version()
        if pat_name:
            credentials: dict[str, Any] = {
                "personalAccessTokenName": pat_name,
                "personalAccessTokenSecret": secret,
            }
        elif user:
            credentials = {"name": user, "password": secret}
        else:
            raise TableauError("需要 --pat-name 或 --user")
        credentials["site"] = {"contentUrl": site}
        response = self.http.post(f"/api/{version}/auth/signin", json={"credentials": credentials})
        if response.status_code != 200:
            raise _error(response, "登入失敗")
        data = response.json()["credentials"]
        self.session = Session(
            version=version,
            token=data["token"],
            site_id=data["site"]["id"],
            site_url=data["site"].get("contentUrl", site),
            user_id=(data.get("user") or {}).get("id"),
        )
        return self.session

    def use_token(self, token: str) -> Session:
        """沿用瀏覽器登入的工作階段（workgroup_session_id）。"""
        _, version = self.api_version()
        response = self.http.get(
            f"/api/{version}/sessions/current", headers={"X-Tableau-Auth": token}
        )
        if response.status_code != 200:
            raise _error(response, "瀏覽器的登入狀態無法用於 REST API，請改用 --pat-name")
        data = response.json()["session"]
        self.session = Session(
            version=version,
            token=token,
            site_id=data["site"]["id"],
            site_url=data["site"].get("contentUrl", ""),
            user_id=(data.get("user") or {}).get("id"),
        )
        return self.session

    def sign_out(self) -> None:
        if self.session is None:
            return
        with contextlib.suppress(httpx.HTTPError):
            self.http.post(f"/api/{self.session.version}/auth/signout", headers=self._auth())
        self.session = None

    # ------------------------------------------------------------ 查詢

    def _auth(self) -> dict[str, str]:
        if self.session is None:
            raise TableauError("尚未登入")
        return {"X-Tableau-Auth": self.session.token}

    def _site(self, path: str) -> str:
        assert self.session is not None
        return f"/api/{self.session.version}/sites/{self.session.site_id}/{path}"

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.http.get(self._site(path), params=params, headers=self._auth())
        if response.status_code != 200:
            raise _error(response, f"查詢失敗：{path}")
        return response.json()

    def paged(
        self, path: str, group: str, item: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        page = 1
        seen = 0
        while True:
            query = {**(params or {}), "pageSize": self.page_size, "pageNumber": page}
            data = self.get_json(path, query)
            items = _list((data.get(group) or {}).get(item))
            yield from items
            seen += len(items)
            total = _int((data.get("pagination") or {}).get("totalAvailable"))
            if not items or total is None or seen >= total:
                return
            page += 1

    def current_identity(self) -> dict[str, Any]:
        """登入帳號的身分（cases.yaml 的 user，P1）：使用者名稱、全名、網域與所屬群組。

        群組寫成 ISMEMBEROF 比對的名稱：本機群組為名稱，Active Directory 群組為「網域\\名稱」。
        """
        assert self.session is not None
        user_id = self.session.user_id
        if not user_id:
            raise TableauError("登入資訊沒有使用者 id")
        user = self.get_json(f"users/{user_id}").get("user") or {}
        groups = {group_name(g) for g in self.paged(f"users/{user_id}/groups", "groups", "group")}
        groups.discard("")
        return {
            "username": str(user.get("name") or ""),
            "full_name": user.get("fullName") or None,
            "domain": (user.get("domain") or {}).get("name") or None,
            "groups": sorted(groups),
        }

    def workbooks(self) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = [
            {"fields": "_default_,project.name,owner.name,hasExtracts"},
            {"fields": "_default_,project.name"},
            {},
        ]
        last: TableauError | None = None
        for params in attempts:
            try:
                return list(self.paged("workbooks", "workbooks", "workbook", params))
            except TableauError as exc:
                if exc.status != 400:
                    raise
                last = exc  # 舊版不支援某些欄位：退回較少的欄位
        assert last is not None
        raise last

    def users(self) -> list[dict[str, Any]]:
        """站台上的使用者（需要站台管理員）；舊版不支援 fields 時退回預設欄位。"""
        try:
            return list(self.paged("users", "users", "user", {"fields": "_all_"}))
        except TableauError as exc:
            if exc.status != 400:
                raise
            return list(self.paged("users", "users", "user"))

    def groups(self) -> list[dict[str, Any]]:
        return list(self.paged("groups", "groups", "group"))

    def group_users(self, group_id: str) -> list[dict[str, Any]]:
        return list(self.paged(f"groups/{group_id}/users", "users", "user"))

    def views(self) -> list[dict[str, Any]]:
        return list(self.paged("views", "views", "view", {"includeUsageStatistics": "true"}))

    def workbook_views(self, workbook_id: str) -> list[dict[str, Any]]:
        data = self.get_json(f"workbooks/{workbook_id}/views")
        return _list((data.get("views") or {}).get("view"))

    def connections(self, workbook_id: str) -> list[dict[str, Any]]:
        data = self.get_json(f"workbooks/{workbook_id}/connections")
        return _list((data.get("connections") or {}).get("connection"))

    def datasources(self) -> list[dict[str, Any]]:
        """Published Data Source 清單（網址名稱、更新時間）。"""
        return list(self.paged("datasources", "datasources", "datasource"))

    def download(self, workbook_id: str, target: Path, *, include_extract: bool) -> Path:
        """下載報表到 target（副檔名依伺服器回傳的檔案類型決定）；回傳實際路徑。"""
        return self._download(f"workbooks/{workbook_id}/content", target, include_extract, WORKBOOK)

    def download_datasource(
        self, datasource_id: str, target: Path, *, include_extract: bool
    ) -> Path:
        """下載 Published Data Source（.tdsx 或 .tds）。"""
        return self._download(
            f"datasources/{datasource_id}/content", target, include_extract, DATASOURCE
        )

    def _download(
        self, endpoint: str, target: Path, include_extract: bool, kinds: tuple[str, str]
    ) -> Path:
        params = {"includeExtract": "True" if include_extract else "False"}
        with self.http.stream(
            "GET", self._site(endpoint), params=params, headers=self._auth()
        ) as response:
            if response.status_code != 200:
                response.read()
                raise _error(response, "下載失敗")
            ext = _download_suffix(response, kinds)
            path = target.with_name(target.name + ext)  # 報表名稱可能含「.」，不能用 with_suffix
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".part")
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
        partial.replace(path)
        return path


WORKBOOK = (".twb", ".twbx")
DATASOURCE = (".tds", ".tdsx")
"""（XML 檔、封裝檔）的副檔名。"""


def _download_suffix(response: httpx.Response, kinds: tuple[str, str] = WORKBOOK) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    if match:
        suffix = Path(unquote(match.group(1))).suffix.lower()
        if suffix in kinds:
            return suffix
    content_type = response.headers.get("content-type", "")
    return kinds[0] if "xml" in content_type else kinds[1]


def _error(response: httpx.Response, prefix: str) -> TableauError:
    code = None
    detail = response.text[:300]
    try:
        error = response.json().get("error", {})
        code = error.get("code")
        detail = "；".join(v for v in (error.get("summary"), error.get("detail")) if v) or detail
    except ValueError:
        pass
    hint = ""
    if code == "403019":
        hint = "（沒有下載權限：需要「下載／另存新檔」權限）"
    elif response.status_code == 401:
        hint = "（登入資訊錯誤或已失效）"
    text = f"{prefix}：HTTP {response.status_code}" + (f" {code}" if code else "")
    return TableauError(f"{text}{hint}：{detail}", response.status_code, code)


# ---------------------------------------------------------------- 登入流程


def connect(args: argparse.Namespace) -> TableauClient:
    client = TableauClient(
        args.server, verify=ssl_context(args.ca_bundle), page_size=args.page_size
    )
    try:
        if args.browser_session:
            client.use_token(browser_token(args.server, args.profile))
        elif args.pat_name:
            secret = os.environ.get("TABLEAU_PAT_SECRET") or getpass.getpass(
                f"個人存取權杖「{args.pat_name}」的密鑰："
            )
            client.sign_in(args.site, pat_name=args.pat_name, secret=secret)
        elif args.user:
            secret = os.environ.get("TABLEAU_PASSWORD") or getpass.getpass(f"{args.user} 的密碼：")
            client.sign_in(args.site, user=args.user, secret=secret)
        else:
            raise TableauError("請用 --pat-name、--user 或 --browser-session 其中一種方式登入")
    except Exception:
        client.close()
        raise
    return client


def browser_token(server: str, profile: Path) -> str:
    """從 fetch_expected 的瀏覽器設定檔取得 Tableau 的登入工作階段。"""
    from playwright.sync_api import sync_playwright

    if not profile.is_dir():
        raise TableauError(
            f"找不到瀏覽器設定檔 {profile}；先執行 fetch_expected.py --login 登入一次"
        )
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), headless=True)
        try:
            cookies = context.cookies(server)
        finally:
            context.close()
    for cookie in cookies:
        value = cookie.get("value")
        if cookie.get("name") == "workgroup_session_id" and value:
            return str(value)
    raise TableauError("瀏覽器設定檔中沒有 Tableau 的登入狀態；先執行 fetch_expected.py --login")


# ---------------------------------------------------------------- Published Data Source（N8）


@dataclass
class PublishedFetch:
    merged: Path
    """併入後的報表（沒有 Published Data Source 時為原本的檔案）。"""
    notes: list[str]


def fetch_published(
    client: TableauClient,
    path: Path,
    *,
    folder: Path,
    original: Path,
    merged: Path,
    include_extract: bool,
    catalog: list[dict[str, Any]] | None = None,
) -> PublishedFetch:
    """報表連到 Published Data Source 時：下載資料源（.tdsx／.tds）到 folder，原本的報表移到 original，
    併入後的報表寫到 merged。folder 中已有、且伺服器上沒有更新的資料源不重新下載。"""
    from tmig.parser.published import merge_published, workbook_refs

    refs = workbook_refs(path)
    if not refs:
        return PublishedFetch(path, [])
    items = catalog if catalog is not None else client.datasources()
    by_url = {str(d.get("contentUrl")): d for d in items}
    notes: list[str] = []
    folder.mkdir(parents=True, exist_ok=True)
    for ref in refs:
        found = by_url.get(ref.content_url)
        if found is None:
            notes.append(
                f"找不到 Published Data Source「{ref.caption}」（{ref.content_url}）或沒有權限"
            )
            continue
        stamp = folder / f"{ref.content_url}.updated"
        existing = [p for p in folder.glob(f"{ref.content_url}.tds*") if p.suffix in DATASOURCE]
        current = str(found.get("updatedAt") or "")
        if existing and stamp.is_file() and stamp.read_text(encoding="utf-8") == current:
            continue
        for old in existing:
            old.unlink()
        try:
            client.download_datasource(
                str(found["id"]), folder / ref.content_url, include_extract=include_extract
            )
        except (TableauError, httpx.HTTPError) as exc:
            notes.append(f"Published Data Source「{ref.caption}」下載失敗：{exc}")
            continue
        stamp.write_text(current, encoding="utf-8")
    original.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != original.resolve():
        path.replace(original)
    result = merge_published(original, folder, merged)
    notes += [
        f"Published Data Source「{r.caption}」未併入（{r.content_url}）" for r in result.missing
    ]
    notes += [
        f"已併入 Published Data Source「{m.ref.caption}」" + (f"（{m.note}）" if m.note else "")
        for m in result.merged
    ]
    return PublishedFetch(result.out, notes)


# ---------------------------------------------------------------- 盤點


def safe_name(text: str, fallback: str = "workbook") -> str:
    """Windows 也能用的檔名：去掉保留字元、結尾的點與空白，長度限 80 字。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip().rstrip(". ")
    name = name[:80].rstrip(". ") or fallback
    return f"{name}_" if WINDOWS_RESERVED.match(name) else name


def views_url(server: str, site: str, content_url: str) -> str:
    base = server.rstrip("/")
    return f"{base}/#/site/{site}/views/{content_url}" if site else f"{base}/#/views/{content_url}"


def _load_previous(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {wb["id"]: wb for wb in data.get("workbooks", []) if isinstance(wb, dict) and "id" in wb}


def group_name(group: dict[str, Any]) -> str:
    """ISMEMBEROF 比對的群組名稱：本機群組為名稱，Active Directory 群組為「網域\\名稱」。"""
    name = str(group.get("name") or "")
    domain = str((group.get("domain") or {}).get("name") or "local")
    return name if domain.lower() == "local" else f"{domain}\\{name}"


def export_directory(client: TableauClient, source: str) -> dict[str, Any]:
    """使用者目錄（tmig.access.directory 的格式）：每個使用者的名稱、全名、網域、email、站台角色與群組。"""
    members: dict[str, set[str]] = {}
    for group in client.groups():
        name = group_name(group)
        if not name:
            continue
        for user in client.group_users(str(group["id"])):
            members.setdefault(str(user.get("id")), set()).add(name)
    users = []
    for user in client.users():
        if not user.get("name"):
            continue
        users.append(
            {
                "name": str(user["name"]),
                "full_name": user.get("fullName") or None,
                "domain": (user.get("domain") or {}).get("name") or None,
                "email": user.get("email") or None,
                "site_role": user.get("siteRole") or None,
                "groups": sorted(members.get(str(user.get("id")), set())),
            }
        )
    users.sort(key=lambda u: (u["name"].casefold(), u["domain"] or ""))
    return {
        "source": source,
        "exported_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "users": users,
    }


def run_directory(client: TableauClient, args: argparse.Namespace) -> int:
    session = client.session
    assert session is not None
    source = f"{client.server}（site：{session.site_url or '預設'}）"
    try:
        data = export_directory(client, source)
    except TableauError as exc:
        if exc.status == 403:
            print("失敗：匯出使用者與群組需要站台管理員的權限", file=sys.stderr)
            return 1
        raise
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    groups = {g for u in data["users"] for g in u["groups"]}
    print(f"已匯出 {len(data['users'])} 個使用者、{len(groups)} 個群組 → {out}")
    return 0


def run_inventory(client: TableauClient, args: argparse.Namespace) -> int:
    from tmig.census.inventory import run_inventory as summarize

    session = client.session
    assert session is not None
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    previous = _load_previous(out / "server.json")

    print("讀取報表清單…", file=sys.stderr)
    workbooks = client.workbooks()
    if args.project:
        wanted = set(args.project)
        workbooks = [w for w in workbooks if (w.get("project") or {}).get("name") in wanted]
    workbooks.sort(key=lambda w: ((w.get("project") or {}).get("name") or "", w.get("name") or ""))
    if args.limit:
        workbooks = workbooks[: args.limit]

    print("讀取檢視次數…", file=sys.stderr)
    usage: dict[str, int] = {}
    sheets: dict[str, list[str]] = {}
    try:
        for view in client.views():
            wb_id = (view.get("workbook") or {}).get("id")
            if not wb_id:
                continue
            count = _int((view.get("usage") or {}).get("totalViewCount")) or 0
            usage[wb_id] = usage.get(wb_id, 0) + count
            sheets.setdefault(wb_id, []).append(str(view.get("name", "")))
    except TableauError as exc:
        print(f"注意：讀不到檢視次數（{exc}）", file=sys.stderr)

    entries: list[dict[str, Any]] = []
    used: set[str] = set()
    catalog: list[dict[str, Any]] | None = None
    for i, wb in enumerate(workbooks, start=1):
        wb_id = str(wb["id"])
        name = str(wb.get("name", wb_id))
        project = (wb.get("project") or {}).get("name") or ""
        entry: dict[str, Any] = {
            "id": wb_id,
            "name": name,
            "content_url": wb.get("contentUrl"),
            "project": project or None,
            "owner": (wb.get("owner") or {}).get("name"),
            "views": usage.get(wb_id),
            "sheets": sheets.get(wb_id, []),
            "updated_at": wb.get("updatedAt"),
            "url": wb.get("webpageUrl"),
            "has_extracts": _bool(wb.get("hasExtracts")),
            "connections": [],
            "file": None,
            "download_error": None,
            "published": [],
        }
        try:
            entry["connections"] = sorted(
                {str(c.get("type", "")) for c in client.connections(wb_id) if c.get("type")}
            )
        except TableauError as exc:
            entry["download_error"] = str(exc)

        relative = Path("workbooks") / safe_name(project, "default") / safe_name(name)
        key = relative.as_posix().lower()
        if key in used:
            relative = relative.with_name(f"{relative.name}_{wb_id[:8]}")
        used.add(relative.as_posix().lower())

        old = previous.get(wb_id)
        old_file = out / old["file"] if old and old.get("file") else None
        if (
            not args.refresh
            and old is not None
            and old.get("updated_at") == entry["updated_at"]
            and old_file is not None
            and old_file.is_file()
        ):
            entry["file"] = old["file"]
            status = "未更新，沿用"
        elif args.no_download:
            status = "未下載"
        else:
            try:
                path = client.download(wb_id, out / relative, include_extract=False)
                entry["download_error"] = None
                status = "已下載"
                if not args.no_published and "sqlproxy" in entry["connections"]:
                    # 連到 Published Data Source：下載資料源的定義（不含萃取）併入後再盤點
                    if catalog is None:
                        catalog = client.datasources()
                    fetched = fetch_published(
                        client,
                        path,
                        folder=out / "datasources",
                        original=out / "original" / path.relative_to(out / "workbooks"),
                        merged=path.with_suffix(".twbx"),
                        include_extract=False,
                        catalog=catalog,
                    )
                    path = fetched.merged
                    entry["published"] = fetched.notes
                    status += "；" + "；".join(fetched.notes) if fetched.notes else ""
                entry["file"] = path.relative_to(out).as_posix()
            except (TableauError, httpx.HTTPError) as exc:
                entry["download_error"] = str(exc)
                status = f"下載失敗：{exc}"
        print(f"[{i}/{len(workbooks)}] {project}／{name}：{status}", file=sys.stderr)
        entries.append(entry)

    meta = {
        "server": client.server,
        "site": session.site_url,
        "fetched_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "extracts_omitted": True,
        "workbooks": entries,
    }
    (out / "server.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    if args.no_download:
        print(f"已寫出 {out / 'server.json'}（未下載報表，略過盤點）")
        return 0
    result = summarize(
        [out / "workbooks"] if (out / "workbooks").is_dir() else [],
        out,
        server_meta=out / "server.json",
        progress=lambda m: print(m, file=sys.stderr),
    )
    a, b = len(result.candidates("A")), len(result.candidates("B"))
    print(f"{len(result.rows)} 份報表；起步候選 A 級 {a}、B 級 {b} → {out / 'inventory.md'}")
    return 0


# ---------------------------------------------------------------- 下載選定的報表到 fixtures


def find_workbook(
    workbooks: list[dict[str, Any]], wanted: str, project: str | None
) -> dict[str, Any]:
    def project_of(w: dict[str, Any]) -> str:
        return (w.get("project") or {}).get("name") or ""

    matches = [w for w in workbooks if wanted in (w.get("id"), w.get("contentUrl"))]
    if not matches:
        matches = [w for w in workbooks if w.get("name") == wanted]
    if project is not None:
        matches = [w for w in matches if project_of(w) == project]
    if not matches:
        raise TableauError(
            f"找不到報表「{wanted}」" + (f"（Project：{project}）" if project else "")
        )
    if len(matches) > 1:
        options = "、".join(f"{project_of(w)}／{w.get('name')}（{w.get('id')}）" for w in matches)
        raise TableauError(f"「{wanted}」有 {len(matches)} 份，請加 --project 或改用 id：{options}")
    return matches[0]


def fixture_slug(wb: dict[str, Any]) -> str:
    from tmig.parser.ids import slugify

    for text in (wb.get("contentUrl"), wb.get("name")):
        slug = slugify(str(text or ""))
        if slug:
            return slug
    return f"workbook_{str(wb.get('id', ''))[:8]}"


def starter_cases(
    workbook_id: str,
    url: str,
    pairs: list[tuple[str, str]],
    unpublished: list[tuple[str, str]],
    filters: list[str],
    parameters: list[str],
    *,
    rls: bool = False,
    user: dict[str, Any] | None = None,
) -> str:
    """rls：報表用到使用者函式；user：登入帳號的身分（查不到時寫成待填的註解）。"""
    lines = [
        "# 由 tools/tableau_server 產生的起始檔：預設篩選已列好，請再補 3 組",
        "# （常用值、日期邊界、參數或組合；見 fixtures/superstore_demo/cases.yaml）",
        "# 網址參數的寫法與 Tableau 相同：欄位顯示名稱（大小寫須相符）、日期部分寫成 year(欄位)、多值以逗號分隔",
    ]
    if filters:
        lines.append(f"# dashboard 上的篩選：{'、'.join(filters)}")
    if parameters:
        lines.append(f"# 參數：{'、'.join(parameters)}")
    lines += [
        f"workbook: {workbook_id}",
        f"tableau_views_url: {json.dumps(url, ensure_ascii=False)}",
        "fetched_at: null",
    ]
    if rls:
        lines += _user_lines(user)
    lines.append("sheets:" if pairs else "sheets: []")
    for worksheet, view in pairs:
        lines += [
            f"  - worksheet: {worksheet}",
            f"    view: {json.dumps(view, ensure_ascii=False)}",
        ]
    if unpublished:
        lines.append(
            "  # 下列工作表沒有發佈成 view（隱藏或只在 dashboard 中），無法以網址抓預期結果："
        )
        lines += [f"  # - worksheet: {ws}  # {name}" for ws, name in unpublished]
    lines += [
        "cases:",
        "  - name: default",
        "    url_params: {}",
        "    note: 工作表的預設篩選",
        "  # - name: common_value",
        "  #   url_params:",
        "  #     欄位名稱: 值",
        "  #   note: 常用值",
    ]
    return "\n".join(lines) + "\n"


def _user_lines(user: dict[str, Any] | None) -> list[str]:
    """cases.yaml 的 user：報表用到使用者函式（列層級權限）時，以抓取預期結果的帳號查詢。"""
    note = "# 報表用到使用者函式（列層級權限）：以抓取預期結果的帳號查詢，須與 fetch_expected 登入的帳號相同"
    if user is None or not user.get("username"):
        return [
            note,
            "# 查不到登入帳號的資料，請填寫：",
            "# user:",
            "#   username: 帳號",
            "#   groups: [群組]",
        ]
    lines = [note, "user:", f"  username: {json.dumps(user['username'], ensure_ascii=False)}"]
    for key in ("full_name", "domain"):
        if user.get(key):
            lines.append(f"  {key}: {json.dumps(user[key], ensure_ascii=False)}")
    lines.append(f"  groups: {json.dumps(list(user.get('groups') or []), ensure_ascii=False)}")
    return lines


def run_fetch(client: TableauClient, args: argparse.Namespace) -> int:
    from tmig.census import run_census
    from tmig.parser.workbook import WorkbookParseError, parse_workbook

    session = client.session
    assert session is not None
    wb = find_workbook(client.workbooks(), args.workbook, args.project)
    slug = fixture_slug(wb)
    fixture: Path = args.fixture or ROOT / "fixtures" / slug
    print(f"下載「{wb.get('name')}」（含萃取）到 {fixture}…", file=sys.stderr)
    path = client.download(str(wb["id"]), fixture / fixture.name, include_extract=True)
    # 連到 Published Data Source：下載資料源（含萃取）併入報表；原本的報表移到 original/
    fetched = fetch_published(
        client,
        path,
        folder=fixture / "published",
        original=fixture / "original" / path.name,
        merged=fixture / f"{fixture.name}.twbx",
        include_extract=True,
    )
    for note in fetched.notes:
        print(note)
    if fetched.merged != path:
        path = fetched.merged
    elif path.suffix == ".twb":
        print("注意：伺服器回傳 .twb（不含資料）；這份報表沒有嵌入式萃取，資料要從 Live 連線取得")

    report = run_census(path)
    failed = [c for c in report.checks if c.status == "fail"]
    print(
        f"盤點：{report.grade} 級；"
        + ("起步條件全部符合" if not failed else "不符合：" + "、".join(c.title for c in failed))
    )
    for check in failed:
        print(f"  - {check.title}：{check.detail}")

    cases_path = fixture / "cases.yaml"
    if cases_path.exists():
        print(f"已有 {cases_path}，不覆蓋")
    else:
        try:
            ir = parse_workbook(path, lenient=True)
        except WorkbookParseError as exc:
            print(f"注意：解析失敗，未產生 cases.yaml（{exc}）")
        else:
            views = client.workbook_views(str(wb["id"]))
            by_name = {
                str(v.get("name")): str(
                    v.get("viewUrlName") or str(v.get("contentUrl", "")).rsplit("/", 1)[-1]
                )
                for v in views
            }
            pairs = [(w.id, by_name[w.name]) for w in ir.worksheets if w.name in by_name]
            unpublished = [(w.id, w.name) for w in ir.worksheets if w.name not in by_name]
            fields = {f.id: f.caption for ds in ir.datasources for f in ds.fields}
            filters: set[str] = set()
            for board in ir.dashboards:
                for zone in _zones(board.zones):
                    if zone.kind == "filter" and zone.field is not None:
                        filters.add(fields.get(zone.field.field, zone.field.field))
            parameters = [p.caption for p in ir.parameters]
            url = views_url(client.server, session.site_url, str(wb.get("contentUrl", "")))
            rls = any(c.key == "no_rls" and c.status != "pass" for c in report.checks)
            user: dict[str, Any] | None = None
            if rls:
                try:
                    user = client.current_identity()
                except (TableauError, httpx.HTTPError) as exc:
                    print(f"注意：查不到登入帳號的群組（{exc}），請在 cases.yaml 填寫 user")
            cases_path.write_text(
                starter_cases(
                    ir.workbook.id,
                    url,
                    pairs,
                    unpublished,
                    sorted(filters),
                    parameters,
                    rls=rls,
                    user=user,
                ),
                encoding="utf-8",
                newline="\n",
            )
            if user is not None:
                print(
                    f"報表用到使用者函式：cases.yaml 的 user 為 {user['username']}"
                    f"（{len(user['groups'])} 個群組），fetch_expected 須以同一個帳號登入"
                )
            print(f"已產生 {cases_path}（{len(pairs)} 張工作表，請補 3 組篩選）")
            if ir.unsupported:
                print(
                    f"注意：解析出 {len(ir.unsupported)} 筆不支援的元素，見 uv run tmig parse {path}"
                )
    print("下一步：README「移轉一份真實報表」第 3 步（tmig parse）")
    return 0


def _zones(zones: list[Zone]) -> Iterator[Zone]:
    for zone in zones:
        yield zone
        yield from _zones(zone.children)


# ---------------------------------------------------------------- 命令列


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tableau Server 報表盤點與下載（REST API）")
    commands = parser.add_subparsers(dest="command", required=True, metavar="指令")

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--server", required=True, help="例如 https://tableau.example.com")
        sub.add_argument(
            "--site", default="", help="site 的網址名稱（網址中 /site/ 後面那段）；預設 site 留空"
        )
        auth = sub.add_mutually_exclusive_group()
        auth.add_argument(
            "--pat-name", help="個人存取權杖名稱；密鑰讀 TABLEAU_PAT_SECRET 或提示輸入"
        )
        auth.add_argument("--user", help="帳號；密碼讀 TABLEAU_PASSWORD 或提示輸入")
        auth.add_argument(
            "--browser-session",
            action="store_true",
            help="沿用 fetch_expected 的瀏覽器登入（SSO 環境；實驗性）",
        )
        sub.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help=argparse.SUPPRESS)
        sub.add_argument("--ca-bundle", type=Path, help="公司內部 CA 的 PEM 檔（通常不需要）")
        sub.add_argument("--page-size", type=int, default=PAGE_SIZE, help=argparse.SUPPRESS)

    inventory = commands.add_parser("inventory", help="盤點看得到的報表並排出起步候選")
    common(inventory)
    inventory.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="輸出目錄（預設 inventory）"
    )
    inventory.add_argument("--project", action="append", help="只盤點這個 Project（可重複）")
    inventory.add_argument("--limit", type=int, help="只盤點前 N 份（試跑用）")
    inventory.add_argument("--refresh", action="store_true", help="全部重新下載，不沿用上次的檔案")
    inventory.add_argument("--no-download", action="store_true", help="只列清單與檢視次數，不下載")
    inventory.add_argument(
        "--no-published",
        action="store_true",
        help="不下載報表用到的 Published Data Source 定義（預設會下載並併入後再盤點）",
    )
    inventory.set_defaults(handler=run_inventory)

    fetch = commands.add_parser(
        "fetch", help="下載選定的報表（含萃取）到 fixtures，並產生 cases.yaml"
    )
    common(fetch)
    fetch.add_argument("--workbook", required=True, help="報表名稱、id 或網址名稱")
    fetch.add_argument("--project", help="同名報表在不同 Project 時用來區分")
    fetch.add_argument("--fixture", type=Path, help="fixture 目錄；預設 fixtures/<報表網址名稱>")
    fetch.set_defaults(handler=run_fetch)

    directory = commands.add_parser(
        "directory", help="匯出使用者與群組（查詢服務的使用者目錄；需要站台管理員）"
    )
    common(directory)
    directory.add_argument(
        "--out", type=Path, default=Path("directory.json"), help="輸出檔（預設 directory.json）"
    )
    directory.set_defaults(handler=run_directory)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        client = connect(args)
    except (TableauError, httpx.HTTPError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    try:
        return args.handler(client, args)
    except (TableauError, httpx.HTTPError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    finally:
        client.sign_out()
        client.close()


if __name__ == "__main__":
    sys.exit(main())
