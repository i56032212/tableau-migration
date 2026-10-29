import { useEffect, useState } from "react";
import { api } from "./api";
import { caseState, DashboardView } from "./Dashboard";
import { SessionGate, UserBar } from "./Session";
import { stateFor, type ViewState } from "./state";
import type { CaseInfo, IR, Me, WorkbookSummary } from "./types";
import { WorksheetView } from "./WorksheetView";

// 路由：/、/w/<報表>、/w/<報表>/d/<dashboard>、/w/<報表>/s/<工作表>；?case=<篩選組合>
type Route =
  | { page: "home" }
  | { page: "workbook"; workbook: string }
  | { page: "dashboard"; workbook: string; dashboard: string }
  | { page: "sheet"; workbook: string; worksheet: string };

function parseRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "d" && parts[3]) return { page: "dashboard", workbook: parts[1], dashboard: parts[3] };
    if (parts[2] === "s" && parts[3]) return { page: "sheet", workbook: parts[1], worksheet: parts[3] };
    return { page: "workbook", workbook: parts[1] };
  }
  return { page: "home" };
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
      {(me) =>
        route.page === "home" ? <Home me={me} /> : <WorkbookPage route={route} caseName={caseName} me={me} />
      }
    </SessionGate>
  );
}

function Home({ me }: { me: Me }) {
  const [workbooks, setWorkbooks] = useState<WorkbookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.workbooks().then(setWorkbooks).catch((e: Error) => setError(e.message));
  }, []);
  return (
    <main className="home">
      <div className="home-header">
        <h1>Tableau 移轉檢視器</h1>
        <UserBar me={me} />
      </div>
      {error && <p className="error">無法連到查詢服務：{error}</p>}
      {workbooks?.length === 0 && (
        <p>{me.mode === "none" ? "fixtures 中沒有報表。" : "沒有可以檢視的報表；請向報表的擁有者申請權限。"}</p>
      )}
      <ul className="workbooks">
        {workbooks?.map((wb) => (
          <li key={wb.id}>
            <div className="wb-name">{wb.name}</div>
            <div className="wb-links">
              {wb.dashboards.map((d) => (
                <Link key={d.id} to={`/w/${encodeURIComponent(wb.id)}/d/${encodeURIComponent(d.id)}`}>
                  {d.name}
                </Link>
              ))}
              {wb.worksheets.map((w) => (
                <Link key={w.id} to={`/w/${encodeURIComponent(wb.id)}/s/${encodeURIComponent(w.id)}`}>
                  {w.name}
                </Link>
              ))}
            </div>
            {wb.unsupported > 0 && <div className="error">有 {wb.unsupported} 筆不支援的元素</div>}
          </li>
        ))}
      </ul>
    </main>
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

  if (error) return <main className="error">載入失敗：{error}</main>;
  if (!ir || !view) return <main>載入中…</main>;

  const dashboard = route.page === "dashboard" ? ir.dashboards.find((d) => d.id === route.dashboard) : undefined;
  const worksheet = route.page === "sheet" ? ir.worksheets.find((w) => w.id === route.worksheet) : undefined;
  const base = window.location.pathname;
  const target = route.page === "workbook" && ir.dashboards[0]
    ? `/w/${encodeURIComponent(workbook)}/d/${encodeURIComponent(ir.dashboards[0].id)}`
    : null;
  if (target) {
    navigate(target + window.location.search);
    return null;
  }

  return (
    <div className="page">
      <header className="toolbar">
        <Link to="/">報表</Link>
        <span className="sep">/</span>
        <span className="wb">{ir.workbook.name}</span>
        <select
          value={dashboard ? `d:${dashboard.id}` : worksheet ? `s:${worksheet.id}` : ""}
          onChange={(e) => {
            const [kind, id] = e.target.value.split(":");
            navigate(`/w/${encodeURIComponent(workbook)}/${kind}/${encodeURIComponent(id)}${window.location.search}`);
          }}
        >
          {ir.dashboards.map((d) => (
            <option key={d.id} value={`d:${d.id}`}>
              dashboard：{d.name}
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
        <UserBar me={me} />
      </header>
      {dashboard && (
        <DashboardView
          workbook={workbook}
          ir={ir}
          dashboard={dashboard}
          view={view}
          onView={(next) => setView(next)}
          onNavigate={(target) => {
            const [kind, id] = target.dashboard ? ["d", target.dashboard] : ["s", target.worksheet ?? ""];
            if (id) navigate(`/w/${encodeURIComponent(workbook)}/${kind}/${encodeURIComponent(id)}${window.location.search}`);
          }}
          cases={cases}
        />
      )}
      {worksheet && (
        <div className="single-sheet">
          <WorksheetView workbook={workbook} worksheet={worksheet.id} state={stateFor(ir, worksheet.id, view)} />
        </div>
      )}
      {!dashboard && !worksheet && <main className="error">找不到這個 dashboard 或工作表</main>}
    </div>
  );
}
