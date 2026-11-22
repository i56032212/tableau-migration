import { useEffect, useRef, useState } from "react";
import { AdminPage } from "./Admin";
import { AlertButton } from "./Alerts";
import { api, errorText } from "./api";
import { datasourceApi, DatasourcesPage } from "./Datasources";
import { scheduleApi } from "./schedules";
import { MySubscriptionsPage, SubscribeButton } from "./Subscribe";
import {
  CommentsButton,
  CrosstabDialog,
  CustomViewsButton,
  DataDialog,
  FavoriteButton,
  viewSheets,
} from "./ViewerTools";
import { type FavoriteItem, type RecentItem, type ViewerAccess, viewerApi } from "./viewerApi";
import { caseState, DashboardView } from "./Dashboard";
import {
  CHANGED_EVENT,
  closeDocument,
  DIALOG_EVENT,
  type DesktopDialog,
  installMenuCommands,
  openDialog,
  addDatasource,
  addDatasourceFrom,
  connectServer,
  createWorkbook,
  newWorkbook,
  openDocument,
  saveDocument,
  setDesktopMode,
  toast,
} from "./desktop";
import { exportView } from "./exporter";
import { animationFor } from "./markAnimation";
import {
  DesktopTokensButton,
  Modal,
  OpenFromServerDialog,
  PublishDatasourceDialog,
  PublishDialog,
  ServerPanel,
  VersionsButton,
} from "./Server";
import { SessionGate, UserBar } from "./Session";
import { fieldKey, keepMarks, pendingTitle, setDrill, stateFor, tooltipStateFor, type ViewState } from "./state";
import { DatasourceView } from "./DatasourceView";
import { ConnectServerDialog } from "./editor/ConnectServer";
import { ChooseTableDialog, DatasourceEditor } from "./editor/DatasourceEditor";
import { DashboardEditor } from "./editor/DashboardEditor";
import { Editor } from "./editor/Editor";
import { StoryEditor } from "./editor/StoryEditor";
import { DatasourceFrame, DesktopMenuBar, type TabKind, WorkspaceContext, type WorkspaceInfo } from "./editor/Workspace";
import { StoryView } from "./Story";
import type { CaseInfo, DatasourceSummary, IR, Me, WorkbookSummary } from "./types";
import { PageControl, pagesOf } from "./PageControl";
import { PageSetupDialog, type PagesPrinting } from "./PageSetup";
import { SheetControls } from "./SheetControls";
import { WorksheetView } from "./WorksheetView";

// 路由：/、/w/<報表>、/w/<報表>/d/<dashboard>、/w/<報表>/s/<工作表>、/w/<報表>/story/<Story>、
// /w/<報表>/ds/<資料源>、/w/<報表>/edit/<工作表>（桌面版的編輯器）、/admin/<分頁>（站台管理）、/subscriptions（我的訂閱）、/datasources（資料源）；?case=<篩選組合>
type Route =
  | { page: "home" }
  | { page: "admin"; tab?: string }
  | { page: "subscriptions" }
  | { page: "datasources" }
  | { page: "workbook"; workbook: string }
  | { page: "dashboard"; workbook: string; dashboard: string }
  | { page: "sheet"; workbook: string; worksheet: string }
  | { page: "story"; workbook: string; story: string }
  | { page: "datasource"; workbook: string; datasource: string }
  | { page: "edit"; workbook: string; worksheet: string }
  | { page: "edit-dashboard"; workbook: string; dashboard: string }
  | { page: "edit-story"; workbook: string; story: string }
  | { page: "edit-datasource"; workbook: string; datasource: string };

function parseRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "admin") return { page: "admin", tab: parts[1] };
  if (parts[0] === "subscriptions") return { page: "subscriptions" };
  if (parts[0] === "datasources") return { page: "datasources" };
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "d" && parts[3]) return { page: "dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "s" && parts[3]) return { page: "sheet", workbook: parts[1], worksheet: parts[3] };
    if (parts[2] === "story" && parts[3]) return { page: "story", workbook: parts[1], story: parts[3] };
    if (parts[2] === "ds" && parts[3]) return { page: "datasource", workbook: parts[1], datasource: parts[3] };
    if (parts[2] === "edit" && parts[3]) return { page: "edit", workbook: parts[1], worksheet: parts[3] };
    if (parts[2] === "edit-d" && parts[3]) return { page: "edit-dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "edit-story" && parts[3]) return { page: "edit-story", workbook: parts[1], story: parts[3] };
    if (parts[2] === "edit-ds" && parts[3]) return { page: "edit-datasource", workbook: parts[1], datasource: parts[3] };
    return { page: "workbook", workbook: parts[1] };
  }
  return { page: "home" };
}

/** 檢視（dashboard、工作表、Story）、資料源分頁與編輯器的網址。 */
function viewPath(
  workbook: string,
  kind: "d" | "s" | "story" | "ds" | "edit" | "edit-d" | "edit-story" | "edit-ds",
  id: string,
): string {
  return `/w/${encodeURIComponent(workbook)}/${kind}/${encodeURIComponent(id)}`;
}

/** 不是輸入文字用的 input：焦點在上面時快捷鍵照常（勾過的核取方塊、按鈕、滑桿…）。 */
const NOT_TEXT_INPUTS = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image"]);

/** 這一下按鍵是打給輸入文字的地方的（文字的輸入框、多行文字、可以編輯的區塊）。 */
function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element?.closest) return false;
  if (element.isContentEditable || element.closest("textarea")) return true;
  const input = element.closest("input");
  return input !== null && !NOT_TEXT_INPUTS.has(input.type);
}

export function navigate(to: string) {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function Link({ to, children, className }: { to: string; children: React.ReactNode; className?: string }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

export function App() {
  const [location, setLocation] = useState(() => ({ path: window.location.pathname, search: window.location.search }));
  useEffect(() => {
    const update = () => setLocation({ path: window.location.pathname, search: window.location.search });
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const route = parseRoute(location.path);
  const caseName = new URLSearchParams(location.search).get("case");

  return (
    <SessionGate>
      {(me) => (
        <>
          {me.mode === "desktop" && <DesktopCommands />}
          {/* 桌面版只有這一排功能表（外殼沒有原生選單）：編輯器有自己的，其他頁面用這一個 */}
          {me.mode === "desktop" && !route.page.startsWith("edit") && <DesktopMenuBar navigate={navigate} />}
          {route.page === "home" ? (
            <Home me={me} />
          ) : route.page === "admin" ? (
            <AdminPage me={me} tab={route.tab} go={navigate} />
          ) : route.page === "subscriptions" ? (
            <MySubscriptionsPage go={navigate} />
          ) : route.page === "datasources" ? (
            <DatasourcesPage go={navigate} />
          ) : (
            <WorkbookPage route={route} caseName={caseName} me={me} />
          )}
        </>
      )}
    </SessionGate>
  );
}

/** 桌面版：讓外殼的選單呼叫檢視器，並顯示伺服器的對話框（登入、從伺服器開啟、發布）。 */
function DesktopCommands() {
  const [dialog, setDialog] = useState<DesktopDialog | null>(null);
  useEffect(() => {
    setDesktopMode(true);
    installMenuCommands(navigate);
  }, []);
  useEffect(() => {
    const show = (e: Event) => setDialog((e as CustomEvent<DesktopDialog>).detail);
    window.addEventListener(DIALOG_EVENT, show);
    return () => window.removeEventListener(DIALOG_EVENT, show);
  }, []);
  const close = () => setDialog(null);
  if (!dialog) return null;
  if (dialog.kind === "publish") return <PublishDialog workbook={dialog.workbook} onClose={close} />;
  if (dialog.kind === "publish-datasource")
    return <PublishDatasourceDialog workbook={dialog.workbook} datasource={dialog.datasource} onClose={close} />;
  if (dialog.kind === "open-server") return <OpenFromServerDialog onClose={close} go={navigate} />;
  if (dialog.kind === "connect-server")
    return <ConnectServerDialog workbook={dialog.workbook} onClose={close} go={navigate} />;
  if (dialog.kind === "choose-table") {
    const chosen = dialog;
    return (
      <ChooseTableDialog
        title={chosen.title}
        tables={chosen.tables}
        onClose={close}
        onChoose={(table) =>
          chosen.workbook
            ? addDatasourceFrom(chosen.workbook, chosen.path, table, navigate)
            : createWorkbook(chosen.path, table, navigate)
        }
      />
    );
  }
  return (
    <Modal title="登入伺服器" onClose={close}>
      <ServerPanel onOpenFromServer={() => setDialog({ kind: "open-server" })} />
    </Modal>
  );
}

function Home({ me }: { me: Me }) {
  const [workbooks, setWorkbooks] = useState<WorkbookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 伺服器有排程管理時才有「我的訂閱」；有報表庫時才有我的最愛，有使用紀錄時才有最近檢視
  const [subscriptions, setSubscriptions] = useState<number | null>(null);
  const [favorites, setFavorites] = useState<FavoriteItem[] | null>(null);
  const [recents, setRecents] = useState<RecentItem[] | null>(null);
  const [sources, setSources] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const desktop = me.mode === "desktop";
  useEffect(() => {
    api
      .workbooks()
      .then(setWorkbooks)
      .catch((e: Error) => setError(e.message));
    if (!desktop && me.user) {
      scheduleApi
        .mine()
        .then((data) => setSubscriptions(data.subscriptions.length))
        .catch(() => setSubscriptions(null));
      viewerApi
        .favorites()
        .then(setFavorites)
        .catch(() => setFavorites(null));
      viewerApi
        .recents()
        .then(setRecents)
        .catch(() => setRecents(null));
      datasourceApi
        .list()
        .then((items) => setSources(items.length))
        .catch(() => setSources(null));
    }
  }, [desktop, me.user]);
  const query = search.trim().toLowerCase();
  const matches = (text: string) => !query || text.toLowerCase().includes(query);
  const shown = workbooks
    ?.map((wb) => {
      const hit = matches(wb.name);
      return {
        ...wb,
        dashboards: wb.dashboards.filter((d) => hit || matches(d.name)),
        stories: wb.stories?.filter((st) => hit || matches(st.name)),
        worksheets: wb.worksheets.filter((w) => hit || matches(w.name)),
        hit,
      };
    })
    .filter((wb) => wb.hit || wb.dashboards.length || wb.worksheets.length || wb.stories?.length);
  // 報表庫的報表以發布時的名稱顯示（與報表清單相同）
  const reportName = (report: string, fallback: string) => workbooks?.find((w) => w.id === report)?.name ?? fallback;
  const viewLink = (report: string, view: string | null) => {
    const wb = workbooks?.find((w) => w.id === report);
    if (!wb || !view) return `/w/${encodeURIComponent(report)}`;
    if (wb.dashboards.some((d) => d.id === view)) return viewPath(report, "d", view);
    if (wb.stories?.some((st) => st.id === view)) return viewPath(report, "story", view);
    return viewPath(report, "s", view);
  };
  return (
    <main className="home">
      <div className="home-header">
        <h1>{desktop ? "報表桌面版" : "Tableau 移轉檢視器"}</h1>
        {desktop && (
          <button type="button" className="primary" onClick={() => void openDocument(navigate)}>
            開啟檔案…
          </button>
        )}
        {desktop && (
          <button type="button" onClick={() => void newWorkbook(navigate)} title="以 Excel、文字檔或空間檔案建立新的報表">
            新增報表（連線到資料）…
          </button>
        )}
        {desktop && (
          <button
            type="button"
            onClick={() => connectServer(null)}
            title="連線到資料庫（PostgreSQL、SQL Server、Oracle、Snowflake 等）建立新的報表"
          >
            新增報表（連線到伺服器）…
          </button>
        )}
        {!desktop && me.user?.admin && <Link to="/admin">站台管理</Link>}
        {subscriptions !== null && (
          <Link to="/subscriptions">我的訂閱{subscriptions ? `（${subscriptions}）` : ""}</Link>
        )}
        {sources !== null && <Link to="/datasources">資料源{sources ? `（${sources}）` : ""}</Link>}
        {!desktop && me.user && me.mode !== "none" && <DesktopTokensButton />}
        <UserBar me={me} />
      </div>
      {desktop && <ServerPanel onOpenFromServer={() => openDialog({ kind: "open-server" })} />}
      {error && <p className="error">無法連到查詢服務：{error}</p>}
      {workbooks && workbooks.length > 0 && (
        <div className="home-search">
          <input
            type="search"
            aria-label="搜尋報表"
            placeholder="搜尋報表、dashboard 或工作表"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      )}
      {!query && favorites && favorites.length > 0 && (
        <section className="home-shortcuts" aria-label="我的最愛">
          <h2>我的最愛</h2>
          <div className="wb-links">
            {favorites.map((f) => (
              <Link key={`${f.report}:${f.view ?? ""}`} to={viewLink(f.report, f.view)}>
                ★ {f.view_name ? `${reportName(f.report, f.report_name)}／${f.view_name}` : reportName(f.report, f.report_name)}
              </Link>
            ))}
          </div>
        </section>
      )}
      {!query && recents && recents.length > 0 && (
        <section className="home-shortcuts" aria-label="最近檢視">
          <h2>最近檢視</h2>
          <div className="wb-links">
            {recents.map((r) => (
              <Link key={`${r.report}:${r.view}`} to={viewLink(r.report, r.view)}>
                {reportName(r.report, r.report_name)}／{r.view_name}
              </Link>
            ))}
          </div>
        </section>
      )}
      {query && shown?.length === 0 && <p className="hint">找不到「{search.trim()}」。</p>}
      {workbooks?.length === 0 && (
        <p>
          {desktop
            ? "還沒有開啟的報表：按「開啟檔案…」（Ctrl+O）開啟 Tableau 的 .twbx、.twb，或報表套件 .tmigx；或按「新增報表（連線到資料）…」（Ctrl+N）以 Excel、文字檔建立新的報表，「新增報表（連線到伺服器）…」以資料庫建立。"
            : me.mode === "none"
              ? "fixtures 中沒有報表。"
              : "沒有可以檢視的報表；請向報表的擁有者申請權限。"}
        </p>
      )}
      <ul className="workbooks">
        {shown?.map((wb) => (
          <li key={wb.id}>
            <div className="wb-name">{wb.name}</div>
            <div className="wb-links">
              {wb.dashboards.map((d) => (
                <Link key={d.id} to={viewPath(wb.id, "d", d.id)}>
                  {d.name}
                </Link>
              ))}
              {wb.stories?.map((st) => (
                <Link key={st.id} to={viewPath(wb.id, "story", st.id)}>
                  Story：{st.name}
                </Link>
              ))}
              {wb.worksheets
                .filter((w) => desktop || !w.hidden)
                .map((w) => (
                  <Link key={w.id} to={viewPath(wb.id, "s", w.id)}>
                    {w.hidden ? `${w.name}（隱藏）` : w.name}
                  </Link>
                ))}
            </div>
            {wb.unsupported > 0 && <div className="error">有 {wb.unsupported} 筆不支援的元素</div>}
            {desktop && (
              <DocumentActions
                workbook={wb.id}
                onClosed={() => setWorkbooks((list) => list?.filter((w) => w.id !== wb.id) ?? null)}
              />
            )}
          </li>
        ))}
      </ul>
    </main>
  );
}

/** 桌面版：一份報表的儲存、另存新檔與關閉。 */
function DocumentActions({ workbook, onClosed }: { workbook: string; onClosed?: () => void }) {
  return (
    <div className="desktop-actions">
      <button type="button" onClick={() => void saveDocument(workbook, false)}>
        儲存
      </button>
      <button type="button" onClick={() => void saveDocument(workbook, true)}>
        另存新檔…
      </button>
      <button type="button" onClick={() => openDialog({ kind: "publish", workbook })}>
        發布…
      </button>
      <button type="button" onClick={() => openDialog({ kind: "publish-datasource", workbook })}>
        發布資料源…
      </button>
      {onClosed && (
        <button
          type="button"
          onClick={() =>
            void closeDocument(workbook, (to) => {
              onClosed();
              navigate(to);
            })
          }
        >
          關閉
        </button>
      )}
    </div>
  );
}

function WorkbookPage({
  route,
  caseName,
  me,
}: {
  route: Exclude<Route, { page: "home" } | { page: "admin" } | { page: "subscriptions" } | { page: "datasources" }>;
  caseName: string | null;
  me: Me;
}) {
  const workbook = route.workbook;
  const [ir, setIr] = useState<IR | null>(null);
  const [cases, setCases] = useState<CaseInfo[]>([]);
  // 資料源分頁：沒有權限（403）時不顯示
  const [datasources, setDatasources] = useState<DatasourceSummary[]>([]);
  const [datasourceError, setDatasourceError] = useState<string | null>(null);
  // 篩選組合套用完成前不查詢，避免先以預設狀態查一次
  const [view, setView] = useState<ViewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 編輯器改變報表後遞增，重新載入報表的定義
  const [version, setVersion] = useState(0);
  // 復原、重做：檢視狀態的歷程（篩選、參數、選取、動作）；重新整理：遞增後重新查詢
  const [history, setHistory] = useState<{ past: ViewState[]; future: ViewState[] }>({ past: [], future: [] });
  const [refresh, setRefresh] = useState(0);
  const [access, setAccess] = useState<ViewerAccess | null>(null);
  const [dialog, setDialog] = useState<"crosstab" | "data" | "print" | null>(null);
  const [customName, setCustomName] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 手機大小的畫面：工具列收成一列（回報表清單、切換檢視、「更多」），其餘的工具按「更多」才展開；
  // 桌面與平板的畫面上「更多」不顯示、工具全部排出來（styles.css）
  const [toolsOpen, setToolsOpen] = useState(false);
  // 簡報模式（Tableau 的「視窗＞簡報模式」）：全螢幕、只留檢視，下方的列切換前後一張；按 Esc 結束
  const [presenting, setPresenting] = useState(false);
  // 全螢幕的要求是非同步的，進入、離開可能比它快（連按 F7、Ctrl+H，或進入之後馬上按 Esc）：
  // - wanted：現在要不要簡報模式。要求有了結果、簡報模式卻已經離開時，把剛進入的全螢幕結束——否則畫面留在
  //   全螢幕、卻不是簡報模式
  // - sawFullscreen：這一次簡報模式真的進入過全螢幕。之後全螢幕結束（使用者按 Esc，瀏覽器自己收下）才算離開；
  //   在那之前收到的「不在全螢幕」是上一次離開留下的通知，不理會。只由這一次的要求有了結果時設定：全螢幕的
  //   通知（fullscreenchange）晚於狀態本身，上一次進入的通知可能在離開、再進入之後才到，那時上一次的全螢幕
  //   還沒結束——以通知設定會把它當成這一次的全螢幕，接著上一次的結束完成，這一次被踢出去
  // - entering：第幾次進入。上一次進入的要求晚到時不算這一次的全螢幕
  // - exiting：離開時結束全螢幕還沒完成。這時馬上再進入，要等它完成才要求全螢幕：否則要求落在「正在結束」的
  //   全螢幕上（立刻成功），接著結束完成，這一次的簡報模式被踢出去
  const presentingWanted = useRef(false);
  const sawFullscreen = useRef(false);
  const entering = useRef(0);
  const exiting = useRef<Promise<void> | null>(null);
  const exitFullscreenNow = () => {
    if (!document.fullscreenElement) return;
    const done = document.exitFullscreen().catch(() => undefined);
    exiting.current = done;
    void done.then(() => {
      if (exiting.current === done) exiting.current = null;
    });
  };
  const enterPresentation = () => {
    const mine = ++entering.current;
    presentingWanted.current = true;
    sawFullscreen.current = false;
    setPresenting(true);
    const root = document.documentElement;
    if (!root.requestFullscreen) return;
    void (exiting.current ?? Promise.resolve()).then(async () => {
      if (!presentingWanted.current || entering.current !== mine) return;
      try {
        await root.requestFullscreen();
      } catch {
        return; // 全螢幕要求可能被瀏覽器拒絕（例如內嵌的視窗）：仍以簡報的版面顯示
      }
      if (!presentingWanted.current) exitFullscreenNow();
      else if (entering.current === mine && document.fullscreenElement) sawFullscreen.current = true;
    });
  };
  useEffect(() => {
    if (!presenting) return;
    presentingWanted.current = true;
    const leave = () => {
      if (!document.fullscreenElement && sawFullscreen.current) setPresenting(false);
    };
    // 一次 Esc 只做一件事：簡報模式中是離開簡報模式（Tableau 的使用經驗；官方文件沒有寫，見 README「已知限制
    // 與待驗證」）。真正全螢幕時瀏覽器自己收下 Esc、結束全螢幕（上面的 leave）；沒有全螢幕時（外殼、不允許
    // 全螢幕的環境）按鍵會送到頁面上，同一下 Esc 原本還會
    // 取消標記的選取（WorksheetView 的 installEscape）。這裡在捕捉階段先處理並標成已處理，選取留著——離開
    // 之後再按一次才取消。開著對話框、選單或清單時，Esc 是它們的（收起來），簡報模式不結束；焦點留在篩選的
    // 控制項上（剛勾過的核取方塊、下拉選單）不算，照樣離開
    const keys = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector(".modal-backdrop, .popup-menu, .options.floating, .notes-list")) return;
      event.preventDefault();
      setPresenting(false);
    };
    document.addEventListener("fullscreenchange", leave);
    document.addEventListener("keydown", keys, true);
    return () => {
      presentingWanted.current = false;
      sawFullscreen.current = false;
      document.removeEventListener("fullscreenchange", leave);
      document.removeEventListener("keydown", keys, true);
      exitFullscreenNow();
    };
  }, [presenting]);
  // 簡報模式的快捷鍵（Tableau 的「Switch in and out of Presentation Mode」：F7、Ctrl+H）：在檢視的頁面上進入或
  // 離開。正在輸入文字、開著對話框或選單時不處理（那一下按鍵是它們的）；編輯器的頁面沒有簡報模式。按鍵標成
  // 已處理：瀏覽器自己的 F7（游標瀏覽）、Ctrl+H（歷程記錄）不跟著出現
  const presentable = route.page === "dashboard" || route.page === "sheet" || route.page === "story";
  const presentingNow = useRef(presenting);
  presentingNow.current = presenting;
  const enterRef = useRef(enterPresentation);
  enterRef.current = enterPresentation;
  useEffect(() => {
    if (!presentable) return;
    const keys = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.altKey || event.shiftKey || event.metaKey) return;
      const shortcut = event.ctrlKey ? event.key.toLowerCase() === "h" : event.key === "F7";
      if (!shortcut || isTyping(event.target)) return;
      if (document.querySelector(".modal-backdrop, .popup-menu, .options.floating, .notes-list")) return;
      event.preventDefault();
      if (presentingNow.current) setPresenting(false);
      else enterRef.current();
    };
    document.addEventListener("keydown", keys);
    return () => document.removeEventListener("keydown", keys);
  }, [presentable]);

  // 報表的定義在檢視器之外改變（例如報表改用發布的資料源）：重新載入
  useEffect(() => {
    const changed = (e: Event) => {
      if ((e as CustomEvent<string>).detail === workbook) setVersion((v) => v + 1);
    };
    window.addEventListener(CHANGED_EVENT, changed);
    return () => window.removeEventListener(CHANGED_EVENT, changed);
  }, [workbook]);

  useEffect(() => {
    Promise.all([api.ir(workbook), api.cases(workbook)])
      .then(([loadedIr, loadedCases]) => {
        setIr(loadedIr);
        setCases(loadedCases);
      })
      .catch((e: Error) => setError(e.message));
    api
      .datasources(workbook)
      .then((loaded) => {
        setDatasources(loaded);
        setDatasourceError(null);
      })
      .catch((e: Error) => {
        setDatasources([]);
        setDatasourceError(e.message);
      });
  }, [workbook, version]);

  useEffect(() => {
    if (ir)
      setView(
        caseState(
          ir,
          cases.find((c) => c.name === caseName),
        ),
      );
    setHistory({ past: [], future: [] });
    setCustomName(null);
  }, [ir, cases, caseName]);

  useEffect(() => {
    if (me.mode === "desktop") {
      setAccess(null);
      return;
    }
    viewerApi
      .access(workbook)
      .then(setAccess)
      .catch(() => setAccess(null));
  }, [workbook, me.mode]);

  // 使用紀錄：每開啟一個 dashboard、工作表或 Story 記一次（切換篩選、故事點不算）
  const viewId =
    route.page === "dashboard"
      ? route.dashboard
      : route.page === "sheet"
        ? route.worksheet
        : route.page === "story"
          ? route.story
          : null;
  const known =
    ir !== null &&
    viewId !== null &&
    [...ir.dashboards, ...ir.worksheets, ...(ir.stories ?? [])].some((v) => v.id === viewId);
  useEffect(() => {
    if (known && viewId) void api.visit(workbook, viewId);
  }, [workbook, viewId, known]);
  // 換到另一個檢視：手機上展開的工具收起來，畫面留給檢視
  useEffect(() => setToolsOpen(false), [workbook, viewId]);
  // 列印頁面架的所有頁面時一頁一頁切換：最新的檢視狀態，與開始列印之前的（印完回到它）
  const viewRef = useRef(view);
  viewRef.current = view;
  const printingFrom = useRef<ViewState | null>(null);

  if (error) return <main className="error">載入失敗：{error}</main>;
  if (!ir || !view) return <main>載入中…</main>;

  /** 改變檢視狀態：記入歷程（最多 50 步），可以復原。 */
  const changeView = (next: ViewState) => {
    setHistory((h) => ({ past: [...h.past, view].slice(-50), future: [] }));
    setView(next);
  };
  const undo = () => {
    const previous = history.past[history.past.length - 1];
    if (!previous) return;
    setHistory({ past: history.past.slice(0, -1), future: [view, ...history.future] });
    setView(previous);
  };
  const redo = () => {
    const next = history.future[0];
    if (!next) return;
    setHistory({ past: [...history.past, view], future: history.future.slice(1) });
    setView(next);
  };
  const revert = () => {
    changeView(
      caseState(
        ir,
        cases.find((c) => c.name === caseName),
      ),
    );
    setCustomName(null);
  };

  const stories = ir.stories ?? [];
  const dashboard = route.page === "dashboard" ? ir.dashboards.find((d) => d.id === route.dashboard) : undefined;
  const worksheet = route.page === "sheet" ? ir.worksheets.find((w) => w.id === route.worksheet) : undefined;
  // 頁面架的「所有頁面」列印（頁面設定）：各頁的值與頁面控制項相同；切換不留在檢視的歷程裡，印完回到原本的頁
  const pagesUse = worksheet?.pages ?? null;
  const pagesPrinting: PagesPrinting | null =
    worksheet && pagesUse
      ? {
          values: async () => {
            printingFrom.current = viewRef.current;
            const domain = await api.domain(workbook, fieldKey(pagesUse), "values");
            return domain.kind === "values" ? domain.values : [];
          },
          show: (value) => {
            const current = viewRef.current;
            if (!current) return false;
            const shown = current.pages && worksheet.id in current.pages ? current.pages[worksheet.id] : (worksheet.page ?? null);
            if (JSON.stringify(shown) === JSON.stringify(value)) return false;
            const next = { ...current, pages: { ...(current.pages ?? {}), [worksheet.id]: value } };
            viewRef.current = next;
            setView(next);
            return true;
          },
          restore: () => {
            const original = printingFrom.current;
            printingFrom.current = null;
            if (!original) return;
            viewRef.current = original;
            setView(original);
          },
        }
      : null;
  const editing = route.page === "edit" && me.mode === "desktop" ? route.worksheet : null;
  const editingDashboard = route.page === "edit-dashboard" && me.mode === "desktop" ? route.dashboard : null;
  const editingStory = route.page === "edit-story" && me.mode === "desktop" ? route.story : null;
  const editingDatasource = route.page === "edit-datasource" && me.mode === "desktop" ? route.datasource : null;
  const story = route.page === "story" ? stories.find((s) => s.id === route.story) : undefined;
  const datasource = route.page === "datasource" ? datasources.find((d) => d.id === route.datasource) : undefined;
  const base = window.location.pathname;
  // 報表本身：開啟第一個 dashboard，沒有時開啟第一個 Story 或工作表
  const target =
    route.page !== "workbook"
      ? null
      : ir.dashboards[0]
        ? viewPath(workbook, "d", ir.dashboards[0].id)
        : stories[0]
          ? viewPath(workbook, "story", stories[0].id)
          : ir.worksheets[0]
            ? viewPath(workbook, "s", (ir.worksheets.find((w) => !w.hidden) ?? ir.worksheets[0]).id)
            : null;
  if (target) {
    navigate(target + window.location.search);
    return null;
  }
  const onNavigate = (to: { dashboard?: string | null; worksheet?: string | null; story?: string | null }) => {
    if (to.dashboard) navigate(viewPath(workbook, "d", to.dashboard) + window.location.search);
    else if (to.worksheet) navigate(viewPath(workbook, "s", to.worksheet) + window.location.search);
    else if (to.story) navigate(viewPath(workbook, "story", to.story) + window.location.search);
  };

  // 簡報時依序切換的檢視：dashboard、Story、沒有隱藏的工作表（與「切換檢視」的順序相同）
  const slides: { path: string; name: string; current: boolean }[] = [
    ...ir.dashboards.map((d) => ({ path: viewPath(workbook, "d", d.id), name: d.name, current: dashboard?.id === d.id })),
    ...stories.map((st) => ({ path: viewPath(workbook, "story", st.id), name: st.name, current: story?.id === st.id })),
    ...ir.worksheets
      .filter((w) => !w.hidden || w.id === worksheet?.id)
      .map((w) => ({ path: viewPath(workbook, "s", w.id), name: w.name, current: worksheet?.id === w.id })),
  ];
  const slide = slides.findIndex((s) => s.current);
  const present = (delta: number) => {
    if (slides.length === 0) return;
    const next = slides[(((slide < 0 ? 0 : slide) + delta) % slides.length + slides.length) % slides.length];
    navigate(next.path + window.location.search);
  };

  // 編輯器的工作區外框（功能表列、分頁列）用到的報表資訊與導覽
  const EDIT_PATHS = { worksheet: "edit", dashboard: "edit-d", story: "edit-story", datasource: "edit-ds" } as const;
  const VIEW_PATHS = { worksheet: "s", dashboard: "d", story: "story", datasource: "ds" } as const;
  const workspace: WorkspaceInfo = {
    workbook,
    name: ir.workbook.name,
    datasources: datasources.map((d) => ({ id: d.id, caption: d.caption })),
    sheets: [
      ...ir.worksheets.map((w) => ({ kind: "worksheet" as const, id: w.id, name: w.name, hidden: w.hidden === true, color: w.tab_color ?? null })),
      ...ir.dashboards.map((d) => ({ kind: "dashboard" as const, id: d.id, name: d.name })),
      ...stories.map((st) => ({ kind: "story" as const, id: st.id, name: st.name })),
    ],
    unsupported: ir.unsupported.length,
    account: <UserBar me={me} />,
    open: (kind: TabKind, id: string) => navigate(viewPath(workbook, EDIT_PATHS[kind], id)),
    view: (kind: TabKind, id: string | null) => {
      setVersion((v) => v + 1);
      navigate(id ? viewPath(workbook, VIEW_PATHS[kind], id) : `/w/${encodeURIComponent(workbook)}`);
    },
    navigate,
    changed: () => setVersion((v) => v + 1),
  };
  // 編輯器有自己的工作區外框：不顯示檢視器的工具列（功能表列、分頁列取代它）
  const framed = Boolean(editing || editingDashboard || editingStory || editingDatasource);

  return (
    <WorkspaceContext.Provider value={workspace}>
    <div className={presenting ? "page presenting" : framed ? "page editing" : "page"} data-presenting={presenting ? "true" : "false"}>
      {presenting && (
        <nav className="presentation-bar" aria-label="簡報模式">
          <button type="button" aria-label="上一張" title="上一張" onClick={() => present(-1)}>
            ◀
          </button>
          <span className="presentation-name">
            {slide >= 0 ? slides[slide].name : ""}（{slide + 1}／{slides.length}）
          </span>
          <button type="button" aria-label="下一張" title="下一張" onClick={() => present(1)}>
            ▶
          </button>
          <button type="button" title="結束簡報模式（Esc、F7、Ctrl+H）" onClick={() => setPresenting(false)}>
            結束簡報
          </button>
        </nav>
      )}
      {!framed && (
      <header className="toolbar" data-tools={toolsOpen ? "open" : "closed"}>
        <Link to="/" className="toolbar-home">
          報表
        </Link>
        <span className="sep">/</span>
        <span className="wb">{ir.workbook.name}</span>
        <select
          className="view-switch"
          aria-label="切換檢視"
          value={
            dashboard
              ? `d:${dashboard.id}`
              : worksheet
                ? `s:${worksheet.id}`
                : story
                  ? `story:${story.id}`
                  : datasource
                    ? `ds:${datasource.id}`
                    : editing
                      ? `edit:${editing}`
                      : editingDashboard
                        ? `edit-d:${editingDashboard}`
                        : editingStory
                          ? `edit-story:${editingStory}`
                          : editingDatasource
                            ? `edit-ds:${editingDatasource}`
                            : ""
          }
          onChange={(e) => {
            const [kind, id] = e.target.value.split(":");
            // 編輯中切換到另一個工作表、dashboard 或 Story 時繼續編輯
            const editingAny = editing || editingDashboard || editingStory;
            const target = !editingAny
              ? kind
              : kind === "s"
                ? "edit"
                : kind === "d"
                  ? "edit-d"
                  : kind === "story"
                    ? "edit-story"
                    : kind;
            navigate(
              viewPath(workbook, target as "d" | "s" | "story" | "ds" | "edit" | "edit-d" | "edit-story" | "edit-ds", id) +
                window.location.search,
            );
          }}
        >
          {datasources.map((d) => (
            <option key={d.id} value={editingDatasource === d.id ? `edit-ds:${d.id}` : `ds:${d.id}`}>
              資料源：{d.caption}
            </option>
          ))}
          {ir.dashboards.map((d) => (
            <option key={d.id} value={editingDashboard === d.id ? `edit-d:${d.id}` : `d:${d.id}`}>
              dashboard：{d.name}
            </option>
          ))}
          {stories.map((st) => (
            <option key={st.id} value={editingStory === st.id ? `edit-story:${st.id}` : `story:${st.id}`}>
              Story：{st.name}
            </option>
          ))}
          {ir.worksheets
            // 隱藏的工作表：檢視器的清單不列出（正在看的除外）；桌面版照樣列出，方便編輯
            .filter((w) => me.mode === "desktop" || !w.hidden || w.id === worksheet?.id || editing === w.id)
            .map((w) => (
              <option key={w.id} value={editing === w.id ? `edit:${w.id}` : `s:${w.id}`} style={w.tab_color ? { backgroundColor: `${w.tab_color}55` } : undefined}>
                工作表：{w.name}
                {w.hidden ? "（隱藏）" : ""}
              </option>
            ))}
        </select>
        <button
          type="button"
          className="toolbar-more"
          aria-label="更多工具"
          aria-expanded={toolsOpen}
          title={toolsOpen ? "收起工具" : "更多工具：簡報模式、復原、匯出等"}
          onClick={() => setToolsOpen(!toolsOpen)}
        >
          ⋯
        </button>
        {me.mode === "desktop" && worksheet && (
          <button type="button" className="primary" onClick={() => navigate(viewPath(workbook, "edit", worksheet.id))}>
            編輯工作表
          </button>
        )}
        {me.mode === "desktop" && dashboard && (
          <button
            type="button"
            className="primary"
            onClick={() => navigate(viewPath(workbook, "edit-d", dashboard.id))}
          >
            編輯 dashboard
          </button>
        )}
        {me.mode === "desktop" && story && (
          <button
            type="button"
            className="primary"
            onClick={() => navigate(viewPath(workbook, "edit-story", story.id))}
          >
            編輯 Story
          </button>
        )}
        {me.mode === "desktop" && datasource && (
          <button
            type="button"
            className="primary"
            onClick={() => navigate(viewPath(workbook, "edit-ds", datasource.id))}
          >
            編輯資料源
          </button>
        )}
        {me.mode === "desktop" && (datasource || editingDatasource) && (
          <button type="button" onClick={() => void addDatasource(workbook, navigate)}>
            新增資料源…
          </button>
        )}
        {me.mode === "desktop" && (datasource || editingDatasource) && (
          <button type="button" title="以資料庫的資料表或自訂 SQL 新增資料源" onClick={() => connectServer(workbook)}>
            連線到伺服器…
          </button>
        )}
        {(dashboard || worksheet || story) && (
          <button
            type="button"
            title="全螢幕、只顯示檢視，可以切換前後一張（Tableau 的「簡報模式」）；按 F7 或 Ctrl+H 進入、離開，Esc 結束"
            onClick={enterPresentation}
          >
            簡報模式
          </button>
        )}
        {(dashboard || worksheet || story) && (
          <span className="history" role="group" aria-label="復原與重做">
            <button type="button" title="復原" aria-label="復原" disabled={history.past.length === 0} onClick={undo}>
              ↶
            </button>
            <button type="button" title="重做" aria-label="重做" disabled={history.future.length === 0} onClick={redo}>
              ↷
            </button>
            <button type="button" title="還原成原始的檢視" onClick={revert}>
              還原
            </button>
            <button type="button" title="重新查詢資料" onClick={() => setRefresh((n) => n + 1)}>
              重新整理
            </button>
          </span>
        )}
        {(dashboard || worksheet || story) && (access === null || access.export_image || access.export_data) && (
          <select
            aria-label="匯出"
            value=""
            onChange={(e) => {
              const kind = e.target.value;
              e.target.value = "";
              if (kind === "crosstab" || kind === "data") {
                setDialog(kind);
                return;
              }
              if (kind === "pdf") {
                // 頁面設定（紙張、方向、縮放、顯示的項目）之後再列印
                setDialog("print");
                return;
              }
              const element = document.querySelector<HTMLElement>(
                ".page .dashboard, .page .single-sheet, .page .story",
              );
              const title = dashboard?.name ?? worksheet?.name ?? story?.name ?? "export";
              if (!element || (kind !== "image" && kind !== "powerpoint")) return;
              void exportView(element, kind, title, me.mode === "desktop")
                .then((path) => path && toast(`已匯出：${path}`))
                .catch((err: unknown) => toast(`無法匯出：${errorText(err)}`, "error"));
            }}
          >
            <option value="">匯出…</option>
            {access?.export_image !== false && <option value="image">圖片（PNG）</option>}
            {access?.export_data !== false && (dashboard || worksheet) && <option value="data">資料…</option>}
            {access?.export_data !== false && (dashboard || worksheet) && <option value="crosstab">交叉資料表…</option>}
            {access?.export_image !== false && <option value="pdf">PDF（列印）</option>}
            {access?.export_image !== false && <option value="powerpoint">PowerPoint</option>}
          </select>
        )}
        {access?.store && viewId && known && (
          <CustomViewsButton
            key={`${workbook}:${viewId}`}
            workbook={workbook}
            view={viewId}
            state={view}
            onApply={(state, name) => {
              changeView(
                state ??
                  caseState(
                    ir,
                    cases.find((c) => c.name === caseName),
                  ),
              );
              setCustomName(name);
            }}
            onSaved={setCustomName}
          />
        )}
        {customName && <span className="custom-view-name">自訂檢視：{customName}</span>}
        {access?.store && access.view_comments && viewId && known && (
          <CommentsButton key={`c:${workbook}:${viewId}`} workbook={workbook} view={viewId} />
        )}
        {access?.store && viewId && known && <FavoriteButton workbook={workbook} view={viewId} />}
        {access?.store && me.user && viewId && known && (dashboard || worksheet) && (
          <AlertButton
            key={`a:${workbook}:${viewId}`}
            workbook={workbook}
            view={viewId}
            ir={ir}
            sheets={viewSheets(ir, dashboard?.id ?? null, worksheet?.id ?? null)}
            state={view}
          />
        )}
        <div className="cases" role="group" aria-label="篩選組合">
          <button type="button" className={caseName === null ? "active" : ""} onClick={() => navigate(base)}>
            預設
          </button>
          {cases.map((c) => (
            <button
              type="button"
              key={c.name}
              className={caseName === c.name ? "active" : ""}
              title={c.note ?? JSON.stringify(c.url_params)}
              onClick={() => navigate(`${base}?case=${encodeURIComponent(c.name)}`)}
            >
              {c.name}
            </button>
          ))}
        </div>
        {ir.unsupported.length > 0 && <span className="error">不支援 {ir.unsupported.length} 筆</span>}
        {me.mode === "desktop" && <DocumentActions workbook={workbook} onClosed={() => undefined} />}
        {me.mode !== "desktop" && <VersionsButton workbook={workbook} />}
        {me.mode !== "desktop" && me.user && (dashboard || worksheet) && (
          <SubscribeButton workbook={workbook} view={dashboard?.name ?? worksheet?.name ?? null} />
        )}
        {notice && <span className="success">{notice}</span>}
        <UserBar me={me} />
      </header>
      )}
      {dialog === "print" && (
        <PageSetupDialog
          target={() => document.querySelector<HTMLElement>(".page .dashboard, .page .single-sheet, .page .story")}
          onClose={() => setDialog(null)}
          pages={pagesPrinting}
        />
      )}
      {dialog === "crosstab" && (dashboard || worksheet) && (
        <CrosstabDialog
          workbook={workbook}
          ir={ir}
          sheets={viewSheets(ir, dashboard?.id ?? null, worksheet?.id ?? null)}
          view={view}
          desktop={me.mode === "desktop"}
          onClose={() => setDialog(null)}
          onDone={(message) => {
            setDialog(null);
            setNotice(message);
          }}
        />
      )}
      {dialog === "data" && (dashboard || worksheet) && (
        <DataDialog
          workbook={workbook}
          ir={ir}
          sheets={viewSheets(ir, dashboard?.id ?? null, worksheet?.id ?? null)}
          view={view}
          access={
            access ?? {
              export_image: true,
              export_data: true,
              underlying_data: true,
              download_workbook: true,
              share_view: false,
              view_comments: false,
              add_comment: false,
              store: false,
            }
          }
          desktop={me.mode === "desktop"}
          onClose={() => setDialog(null)}
        />
      )}
      {dashboard && (
        <DashboardView
          key={refresh}
          workbook={workbook}
          ir={ir}
          dashboard={dashboard}
          view={view}
          onView={changeView}
          onNavigate={onNavigate}
          cases={cases}
        />
      )}
      {editing && (
        <Editor
          key={editing}
          workbook={workbook}
          worksheet={editing}
          onChanged={() => setVersion((v) => v + 1)}
          onOpen={(id) => navigate(viewPath(workbook, "edit", id))}
          onClose={(id) => {
            setVersion((v) => v + 1);
            navigate(id ? viewPath(workbook, "s", id) : `/w/${encodeURIComponent(workbook)}`);
          }}
          onEditDatasource={(id) => navigate(viewPath(workbook, "edit-ds", id))}
        />
      )}
      {editingDashboard && (
        <DashboardEditor
          key={editingDashboard}
          workbook={workbook}
          dashboard={editingDashboard}
          ir={ir}
          onChanged={() => setVersion((v) => v + 1)}
          onOpen={(id) => navigate(viewPath(workbook, "edit-d", id))}
          onClose={(id) => {
            setVersion((v) => v + 1);
            navigate(id ? viewPath(workbook, "d", id) : `/w/${encodeURIComponent(workbook)}`);
          }}
        />
      )}
      {editingStory && (
        <StoryEditor
          key={editingStory}
          workbook={workbook}
          story={editingStory}
          ir={ir}
          onChanged={() => setVersion((v) => v + 1)}
          onOpen={(id) => navigate(viewPath(workbook, "edit-story", id))}
          onClose={(id) => {
            setVersion((v) => v + 1);
            navigate(id ? viewPath(workbook, "story", id) : `/w/${encodeURIComponent(workbook)}`);
          }}
        />
      )}
      {worksheet && (
        <div className={`single-sheet${(worksheet.controls ?? []).length > 0 ? " with-controls" : ""}`}>
          <div className="sheet-main">
            {worksheet.pages && view && (
              <PageControl
                workbook={workbook}
                worksheet={worksheet.id}
                {...pagesOf(ir, worksheet.id)}
                view={view}
                onView={changeView}
              />
            )}
            <WorksheetView
              key={refresh}
              workbook={workbook}
              worksheet={worksheet.id}
              name={pendingTitle(ir, worksheet.id)}
              state={stateFor(ir, worksheet.id, view)}
              revision={refresh}
              animation={animationFor(ir, worksheet.id)}
              onDrill={(key, delta) => changeView(setDrill(view, worksheet.id, key, delta))}
              tooltipState={(target, mark) => tooltipStateFor(ir, worksheet.id, target, view, mark)}
              onKeep={(mode, marks) => changeView(keepMarks(view, worksheet.id, mode, marks))}
            />
          </div>
          {view && <SheetControls ir={ir} workbook={workbook} worksheet={worksheet} view={view} onView={changeView} />}
        </div>
      )}
      {story && (
        <StoryView
          key={refresh}
          workbook={workbook}
          ir={ir}
          story={story}
          base={view}
          cases={cases}
          onNavigate={onNavigate}
        />
      )}
      {datasource && <DatasourceView workbook={workbook} datasource={datasource} />}
      {editingDatasource && (
        <DatasourceFrame datasource={editingDatasource}>
          <DatasourceEditor
            key={editingDatasource}
            workbook={workbook}
            datasource={editingDatasource}
            onChanged={() => setVersion((v) => v + 1)}
            onOpenSheet={() => {
              const sheet = ir.worksheets.find((w) => w.datasource === editingDatasource) ?? ir.worksheets[0];
              if (sheet) navigate(viewPath(workbook, "edit", sheet.id));
            }}
          />
        </DatasourceFrame>
      )}
      {route.page === "datasource" && !datasource && (datasources.length > 0 || datasourceError) && (
        <main className="error">{datasourceError ? `無法開啟資料源：${datasourceError}` : "找不到這個資料源"}</main>
      )}
      {!dashboard &&
        !worksheet &&
        !story &&
        !editing &&
        !editingDashboard &&
        !editingStory &&
        !editingDatasource &&
        route.page !== "datasource" && <main className="error">找不到這個 dashboard、工作表或 Story</main>}
    </div>
    </WorkspaceContext.Provider>
  );
}
