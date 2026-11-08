import { useEffect, useState } from "react";
import { api } from "./api";
import { caseState, DashboardView } from "./Dashboard";
import { closeDocument, installMenuCommands, openDocument, saveDocument } from "./desktop";
import { SessionGate, UserBar } from "./Session";
import { stateFor, type ViewState } from "./state";
import { StoryView } from "./Story";
import type { CaseInfo, IR, Me, WorkbookSummary } from "./types";
import { WorksheetView } from "./WorksheetView";

// 路由：/、/w/<報表>、/w/<報表>/d/<dashboard>、/w/<報表>/s/<工作表>、/w/<報表>/story/<Story>；
// ?case=<篩選組合>
type Route =
  | { page: "home" }
  | { page: "workbook"; workbook: string }
  | { page: "dashboard"; workbook: string; dashboard: string }
  | { page: "sheet"; workbook: string; worksheet: string }
  | { page: "story"; workbook: string; story: string };

function parseRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "d" && parts[3]) return { page: "dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "s" && parts[3]) return { page: "sheet", workbook: parts[1], worksheet: parts[3] };
    if (parts[2] === "story" && parts[3]) return { page: "story", workbook: parts[1], story: parts[3] };
    return { page: "workbook", workbook: parts[1] };
  }
  return { page: "home" };
}

/** 檢視（dashboard、工作表、Story）的網址。 */
function viewPath(workbook: string, kind: "d" | "s" | "story", id: string): string {
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

/** 桌面版：讓外殼的選單（開啟、儲存、另存新檔、關閉報表）呼叫檢視器。 */
function DesktopCommands() {
  useEffect(() => installMenuCommands(navigate), []);
  return null;
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
        <UserBar me={me} />
      </div>
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
  // 篩選組合套用完成前不查詢，避免先以預設狀態查一次
  const [view, setView] = useState<ViewState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.ir(workbook), api.cases(workbook)])
      .then(([loadedIr, loadedCases]) => {
        setIr(loadedIr);
        setCases(loadedCases);
      })
      .catch((e: Error) => setError(e.message));
  }, [workbook]);

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
  const story = route.page === "story" ? stories.find((s) => s.id === route.story) : undefined;
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
  const onNavigate = (to: { dashboard?: string | null; worksheet?: string | null }) => {
    if (to.dashboard) navigate(viewPath(workbook, "d", to.dashboard) + window.location.search);
    else if (to.worksheet) navigate(viewPath(workbook, "s", to.worksheet) + window.location.search);
  };

  return (
    <div className="page">
      <header className="toolbar">
        <Link to="/">報表</Link>
        <span className="sep">/</span>
        <span className="wb">{ir.workbook.name}</span>
        <select
          value={dashboard ? `d:${dashboard.id}` : worksheet ? `s:${worksheet.id}` : story ? `story:${story.id}` : ""}
          onChange={(e) => {
            const [kind, id] = e.target.value.split(":");
            navigate(viewPath(workbook, kind as "d" | "s" | "story", id) + window.location.search);
          }}
        >
          {ir.dashboards.map((d) => (
            <option key={d.id} value={`d:${d.id}`}>
              dashboard：{d.name}
            </option>
          ))}
          {stories.map((st) => (
            <option key={st.id} value={`story:${st.id}`}>
              Story：{st.name}
            </option>
          ))}
          {ir.worksheets.map((w) => (
            <option key={w.id} value={`s:${w.id}`}>
              工作表：{w.name}
            </option>
          ))}
        </select>
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
      {worksheet && (
        <div className="single-sheet">
          <WorksheetView workbook={workbook} worksheet={worksheet.id} state={stateFor(ir, worksheet.id, view)} />
        </div>
      )}
      {story && <StoryView workbook={workbook} ir={ir} story={story} base={view} cases={cases} onNavigate={onNavigate} />}
      {!dashboard && !worksheet && !story && <main className="error">找不到這個 dashboard、工作表或 Story</main>}
    </div>
  );
}
