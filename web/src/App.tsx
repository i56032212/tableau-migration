import { useEffect, useState } from "react";
import { api } from "./api";
import { caseState, DashboardView } from "./Dashboard";
import {
  closeDocument,
  DIALOG_EVENT,
  type DesktopDialog,
  installMenuCommands,
  openDialog,
  openDocument,
  saveDocument,
  setDesktopMode,
  toast,
} from "./desktop";
import { exportView } from "./exporter";
import {
  DesktopTokensButton,
  Modal,
  OpenFromServerDialog,
  PublishDialog,
  ServerPanel,
  VersionsButton,
} from "./Server";
import { SessionGate, UserBar } from "./Session";
import { stateFor, type ViewState } from "./state";
import { DatasourceView } from "./DatasourceView";
import { DashboardEditor } from "./editor/DashboardEditor";
import { Editor } from "./editor/Editor";
import { StoryEditor } from "./editor/StoryEditor";
import { StoryView } from "./Story";
import type { CaseInfo, DatasourceSummary, IR, Me, WorkbookSummary } from "./types";
import { PageControl, pagesOf } from "./PageControl";
import { WorksheetView } from "./WorksheetView";

// 路由：/、/w/<報表>、/w/<報表>/d/<dashboard>、/w/<報表>/s/<工作表>、/w/<報表>/story/<Story>、
// /w/<報表>/ds/<資料源>、/w/<報表>/edit/<工作表>（桌面版的編輯器）；?case=<篩選組合>
type Route =
  | { page: "home" }
  | { page: "workbook"; workbook: string }
  | { page: "dashboard"; workbook: string; dashboard: string }
  | { page: "sheet"; workbook: string; worksheet: string }
  | { page: "story"; workbook: string; story: string }
  | { page: "datasource"; workbook: string; datasource: string }
  | { page: "edit"; workbook: string; worksheet: string }
  | { page: "edit-dashboard"; workbook: string; dashboard: string }
  | { page: "edit-story"; workbook: string; story: string };

function parseRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "d" && parts[3]) return { page: "dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "s" && parts[3]) return { page: "sheet", workbook: parts[1], worksheet: parts[3] };
    if (parts[2] === "story" && parts[3]) return { page: "story", workbook: parts[1], story: parts[3] };
    if (parts[2] === "ds" && parts[3]) return { page: "datasource", workbook: parts[1], datasource: parts[3] };
    if (parts[2] === "edit" && parts[3]) return { page: "edit", workbook: parts[1], worksheet: parts[3] };
    if (parts[2] === "edit-d" && parts[3]) return { page: "edit-dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "edit-story" && parts[3]) return { page: "edit-story", workbook: parts[1], story: parts[3] };
    return { page: "workbook", workbook: parts[1] };
  }
  return { page: "home" };
}

/** 檢視（dashboard、工作表、Story）、資料源分頁與編輯器的網址。 */
function viewPath(workbook: string, kind: "d" | "s" | "story" | "ds" | "edit" | "edit-d" | "edit-story", id: string): string {
  return `/w/${encodeURIComponent(workbook)}/${kind}/${encodeURIComponent(id)}`;
}

export function navigate(to: string) {
  window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function Link({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <a
      href={to}
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
          {route.page === "home" ? <Home me={me} /> : <WorkbookPage route={route} caseName={caseName} me={me} />}
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
  if (dialog.kind === "open-server") return <OpenFromServerDialog onClose={close} go={navigate} />;
  return (
    <Modal title="登入伺服器" onClose={close}>
      <ServerPanel onOpenFromServer={() => setDialog({ kind: "open-server" })} />
    </Modal>
  );
}

function Home({ me }: { me: Me }) {
  const [workbooks, setWorkbooks] = useState<WorkbookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.workbooks().then(setWorkbooks).catch((e: Error) => setError(e.message));
  }, []);
  const desktop = me.mode === "desktop";
  return (
    <main className="home">
      <div className="home-header">
        <h1>{desktop ? "報表桌面版" : "Tableau 移轉檢視器"}</h1>
        {desktop && (
          <button type="button" className="primary" onClick={() => void openDocument(navigate)}>
            開啟檔案…
          </button>
        )}
        {!desktop && me.user && me.mode !== "none" && <DesktopTokensButton />}
        <UserBar me={me} />
      </div>
      {desktop && <ServerPanel onOpenFromServer={() => openDialog({ kind: "open-server" })} />}
      {error && <p className="error">無法連到查詢服務：{error}</p>}
      {workbooks?.length === 0 && (
        <p>
          {desktop
            ? "還沒有開啟的報表：按「開啟檔案…」（Ctrl+O）開啟 Tableau 的 .twbx、.twb，或報表套件 .tmigx。"
            : me.mode === "none"
              ? "fixtures 中沒有報表。"
              : "沒有可以檢視的報表；請向報表的擁有者申請權限。"}
        </p>
      )}
      <ul className="workbooks">
        {workbooks?.map((wb) => (
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
              {wb.worksheets.map((w) => (
                <Link key={w.id} to={viewPath(wb.id, "s", w.id)}>
                  {w.name}
                </Link>
              ))}
            </div>
            {wb.unsupported > 0 && <div className="error">有 {wb.unsupported} 筆不支援的元素</div>}
            {desktop && <DocumentActions workbook={wb.id} onClosed={() => setWorkbooks((list) => list?.filter((w) => w.id !== wb.id) ?? null)} />}
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
  route: Exclude<Route, { page: "home" }>;
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
    if (ir) setView(caseState(ir, cases.find((c) => c.name === caseName)));
  }, [ir, cases, caseName]);

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

  if (error) return <main className="error">載入失敗：{error}</main>;
  if (!ir || !view) return <main>載入中…</main>;

  const stories = ir.stories ?? [];
  const dashboard = route.page === "dashboard" ? ir.dashboards.find((d) => d.id === route.dashboard) : undefined;
  const worksheet = route.page === "sheet" ? ir.worksheets.find((w) => w.id === route.worksheet) : undefined;
  const editing = route.page === "edit" && me.mode === "desktop" ? route.worksheet : null;
  const editingDashboard = route.page === "edit-dashboard" && me.mode === "desktop" ? route.dashboard : null;
  const editingStory = route.page === "edit-story" && me.mode === "desktop" ? route.story : null;
  const story = route.page === "story" ? stories.find((s) => s.id === route.story) : undefined;
  const datasource =
    route.page === "datasource" ? datasources.find((d) => d.id === route.datasource) : undefined;
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
            ? viewPath(workbook, "s", ir.worksheets[0].id)
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

  return (
    <div className="page">
      <header className="toolbar">
        <Link to="/">報表</Link>
        <span className="sep">/</span>
        <span className="wb">{ir.workbook.name}</span>
        <select
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
                          : ""
          }
          onChange={(e) => {
            const [kind, id] = e.target.value.split(":");
            // 編輯中切換到另一個工作表、dashboard 或 Story 時繼續編輯
            const editingAny = editing || editingDashboard || editingStory;
            const target = !editingAny ? kind : kind === "s" ? "edit" : kind === "d" ? "edit-d" : kind === "story" ? "edit-story" : kind;
            navigate(viewPath(workbook, target as "d" | "s" | "story" | "ds" | "edit" | "edit-d" | "edit-story", id) + window.location.search);
          }}
        >
          {datasources.map((d) => (
            <option key={d.id} value={`ds:${d.id}`}>
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
          {ir.worksheets.map((w) => (
            <option key={w.id} value={editing === w.id ? `edit:${w.id}` : `s:${w.id}`}>
              工作表：{w.name}
            </option>
          ))}
        </select>
        {me.mode === "desktop" && worksheet && (
          <button type="button" className="primary" onClick={() => navigate(viewPath(workbook, "edit", worksheet.id))}>
            編輯工作表
          </button>
        )}
        {me.mode === "desktop" && dashboard && (
          <button type="button" className="primary" onClick={() => navigate(viewPath(workbook, "edit-d", dashboard.id))}>
            編輯 dashboard
          </button>
        )}
        {me.mode === "desktop" && story && (
          <button type="button" className="primary" onClick={() => navigate(viewPath(workbook, "edit-story", story.id))}>
            編輯 Story
          </button>
        )}
        {(dashboard || worksheet || story) && (
          <select
            aria-label="匯出"
            value=""
            onChange={(e) => {
              const kind = e.target.value;
              e.target.value = "";
              if (kind === "pdf") {
                window.print();
                return;
              }
              const element = document.querySelector<HTMLElement>(".page .dashboard, .page .single-sheet, .page .story");
              const title = dashboard?.name ?? worksheet?.name ?? story?.name ?? "export";
              if (!element || (kind !== "image" && kind !== "powerpoint")) return;
              void exportView(element, kind, title, me.mode === "desktop")
                .then((path) => path && toast(`已匯出：${path}`))
                .catch((err: Error) => toast(`無法匯出：${err.message}`, "error"));
            }}
          >
            <option value="">匯出…</option>
            <option value="image">圖片（PNG）</option>
            <option value="powerpoint">PowerPoint</option>
            <option value="pdf">PDF（列印）</option>
          </select>
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
        <UserBar me={me} />
      </header>
      {dashboard && (
        <DashboardView
          workbook={workbook}
          ir={ir}
          dashboard={dashboard}
          view={view}
          onView={(next) => setView(next)}
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
        <div className="single-sheet">
          {worksheet.pages && view && (
            <PageControl workbook={workbook} worksheet={worksheet.id} {...pagesOf(ir, worksheet.id)} view={view} onView={setView} />
          )}
          <WorksheetView workbook={workbook} worksheet={worksheet.id} state={stateFor(ir, worksheet.id, view)} />
        </div>
      )}
      {story && <StoryView workbook={workbook} ir={ir} story={story} base={view} cases={cases} onNavigate={onNavigate} />}
      {datasource && <DatasourceView workbook={workbook} datasource={datasource} />}
      {route.page === "datasource" && !datasource && (datasources.length > 0 || datasourceError) && (
        <main className="error">{datasourceError ? `無法開啟資料源：${datasourceError}` : "找不到這個資料源"}</main>
      )}
      {!dashboard && !worksheet && !story && !editing && !editingDashboard && !editingStory && route.page !== "datasource" && (
        <main className="error">找不到這個 dashboard、工作表或 Story</main>
      )}
    </div>
  );
}
