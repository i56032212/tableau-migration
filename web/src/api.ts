import type {
  CaseInfo,
  DatasourcePreview,
  DatasourcePublishResult,
  DatasourceSummary,
  DesktopDatasource,
  DesktopDocument,
  DesktopOpened,
  DesktopSaved,
  DesktopToken,
  Domain,
  IR,
  Me,
  PublishResult,
  QueryState,
  RenderResponse,
  ReportChange,
  ReportDetail,
  ServerProject,
  ServerReport,
  ServerStatus,
  WorkbookSummary,
} from "./types";

/** 查詢服務回應 401（沒有登入或登入逾時）時發出，SessionGate 收到後重新確認登入者。 */
export const UNAUTHORIZED_EVENT = "tmig:unauthorized";

/** API 的錯誤：status 與 detail（文字，或發布失敗時的 {error, message, …}）。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
    message: string,
  ) {
    super(message);
  }
}

/** 給使用者看的錯誤說明：API 的錯誤只取說明的文字（message 開頭的 HTTP 狀態碼是給開發者看的）。 */
export function errorText(error: unknown): string {
  if (error instanceof ApiError && typeof error.detail === "string" && error.detail) return error.detail;
  return error instanceof Error ? error.message : String(error);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!response.ok) {
    let detail: unknown = response.statusText;
    let text = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail;
      text =
        typeof body.detail === "string"
          ? body.detail
          : typeof body.detail?.message === "string"
            ? body.detail.message
            : JSON.stringify(body.detail);
    } catch {
      // 回應不是 JSON 時沿用狀態文字
    }
    throw new ApiError(response.status, detail, `${response.status} ${text}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const report = (id: string) => `/api/reports/${encodeURIComponent(id)}`;

/** 報表庫中某一版的報表套件（下載）。 */
export const packageUrl = (id: string, version: number) => `${report(id)}/versions/${version}/package`;

const wb = (id: string) => `/api/workbooks/${encodeURIComponent(id)}`;

/** .twbx 內的圖片網址（圖片區塊與圖片按鈕）。 */
export const fileUrl = (id: string, path: string) =>
  `${wb(id)}/files/${path.split("/").map(encodeURIComponent).join("/")}`;

export const api = {
  me: () => request<Me>("/api/me"),
  devLogin: (username: string) =>
    request<{ user: Me["user"] }>("/auth/dev", { method: "POST", body: JSON.stringify({ username }) }),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  workbooks: () => request<WorkbookSummary[]>("/api/workbooks"),
  ir: (id: string) => request<IR>(`${wb(id)}/ir`),
  cases: (id: string) => request<CaseInfo[]>(`${wb(id)}/cases`),
  render: (id: string, worksheet: string, state: QueryState) =>
    request<RenderResponse>(`${wb(id)}/worksheets/${encodeURIComponent(worksheet)}/render`, {
      method: "POST",
      body: JSON.stringify(state),
    }),
  /** 使用紀錄：開啟一個 dashboard 或工作表（R1）；失敗不影響檢視 */
  visit: (id: string, view: string) =>
    fetch(`${wb(id)}/views/${encodeURIComponent(view)}/visit`, { method: "POST" }).catch(() => undefined),
  /** 資料源分頁：沒有權限時回應 403 */
  datasources: (id: string) => request<DatasourceSummary[]>(`${wb(id)}/datasources`),
  preview: (id: string, datasource: string, table: string | null, limit: number) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (table) query.set("table", table);
    return request<DatasourcePreview>(
      `${wb(id)}/datasources/${encodeURIComponent(datasource)}/preview?${query.toString()}`,
    );
  },
  /** relevant：「只顯示相關的值」——只列出套用這張工作表上其他篩選（目前的狀態）之後還有資料的值 */
  domain: (id: string, fieldKey: string, kind?: "values" | "range", relevant?: { worksheet: string; state: QueryState; scope?: "relevant" | "context" }) =>
    request<Domain>(`${wb(id)}/fields/${encodeURIComponent(fieldKey)}/domain`, {
      method: "POST",
      body: JSON.stringify({ ...(kind ? { kind } : {}), ...(relevant ?? {}) }),
    }),
  /** 桌面版：開啟中的檔案、開啟、存成報表套件、關閉 */
  desktopDocuments: () => request<DesktopDocument[]>("/api/desktop/documents"),
  desktopOpen: (path: string) =>
    request<DesktopOpened>("/api/desktop/open", { method: "POST", body: JSON.stringify({ path }) }),
  desktopSave: (workbook: string, path: string) =>
    request<DesktopSaved>("/api/desktop/save", { method: "POST", body: JSON.stringify({ workbook, path }) }),
  desktopClose: (workbook: string) =>
    request<{ ok: boolean }>("/api/desktop/close", { method: "POST", body: JSON.stringify({ workbook }) }),
  /** 桌面版連到伺服器：登入、Project 與報表、從伺服器開啟、發布 */
  serverStatus: () => request<ServerStatus>("/api/desktop/server"),
  serverSignIn: (url: string) =>
    request<{ authorize_url: string }>("/api/desktop/server/signin", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  serverSignOut: () => request<{ ok: boolean }>("/api/desktop/server/signout", { method: "POST" }),
  serverProjects: () => request<ServerProject[]>("/api/desktop/server/projects"),
  serverReports: () => request<ServerReport[]>("/api/desktop/server/reports"),
  serverOpen: (report: string, version?: number) =>
    request<DesktopOpened>("/api/desktop/server/open", {
      method: "POST",
      body: JSON.stringify(version === undefined ? { report } : { report, version }),
    }),
  serverPublish: (body: {
    workbook: string;
    name?: string;
    project?: string | null;
    comment?: string;
    report?: string | null;
    overwrite?: boolean;
    force?: boolean;
  }) => request<PublishResult>("/api/desktop/server/publish", { method: "POST", body: JSON.stringify(body) }),
  serverDatasources: (workbook: string) =>
    request<{ datasources: DesktopDatasource[] }>(
      `/api/desktop/server/datasources?workbook=${encodeURIComponent(workbook)}`,
    ),
  serverPublishDatasource: (body: {
    workbook: string;
    datasource: string;
    name?: string;
    project?: string;
    comment?: string;
    target?: string;
    overwrite?: boolean;
    link?: boolean;
  }) =>
    request<DatasourcePublishResult>("/api/desktop/server/publish-datasource", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  serverBrowse: (path: string) =>
    request<{ ok: boolean }>("/api/desktop/server/browse", { method: "POST", body: JSON.stringify({ path }) }),
  /** 伺服器：報表庫的版本、比較、回復；桌面版的登入 */
  report: (id: string) => request<ReportDetail>(report(id)),
  compare: (id: string, base: number, target: number) =>
    request<{ base: number; target: number; changes: ReportChange[] }>(
      `${report(id)}/compare?base=${base}&target=${target}`,
    ),
  restore: (id: string, version: number) =>
    request<PublishResult>(`${report(id)}/versions/${version}/restore`, { method: "POST", body: "{}" }),
  desktopTokens: () => request<DesktopToken[]>("/api/desktop-auth/tokens"),
  revokeDesktopToken: (id: string) =>
    request<{ ok: boolean }>(`/api/desktop-auth/tokens/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
