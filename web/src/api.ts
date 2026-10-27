import type { CaseInfo, Domain, IR, QueryState, RenderResponse, WorkbookSummary } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
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

export const api = {
  workbooks: () => request<WorkbookSummary[]>("/api/workbooks"),
  ir: (id: string) => request<IR>(`${wb(id)}/ir`),
  cases: (id: string) => request<CaseInfo[]>(`${wb(id)}/cases`),
  render: (id: string, worksheet: string, state: QueryState) =>
    request<RenderResponse>(`${wb(id)}/worksheets/${encodeURIComponent(worksheet)}/render`, {
      method: "POST",
      body: JSON.stringify(state),
    }),
  domain: (id: string, fieldKey: string, kind?: "values" | "range") =>
    request<Domain>(`${wb(id)}/fields/${encodeURIComponent(fieldKey)}/domain`, {
      method: "POST",
      body: JSON.stringify(kind ? { kind } : {}),
    }),
};
