import type { CaseInfo, Domain, IR, Me, QueryState, RenderResponse, WorkbookSummary } from "./types";

/** 查詢服務回應 401（沒有登入或登入逾時）時發出，SessionGate 收到後重新確認登入者。 */
export const UNAUTHORIZED_EVENT = "tmig:unauthorized";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // 回應不是 JSON 時沿用狀態文字
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return (await response.json()) as T;
}

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
  domain: (id: string, fieldKey: string, kind?: "values" | "range") =>
    request<Domain>(`${wb(id)}/fields/${encodeURIComponent(fieldKey)}/domain`, {
      method: "POST",
      body: JSON.stringify(kind ? { kind } : {}),
    }),
};
