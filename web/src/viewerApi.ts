import { ApiError, request, UNAUTHORIZED_EVENT } from "./api";
import type { QueryState } from "./types";

// 檢視者的功能：權限、下載、我的最愛、最近、自訂檢視與留言（tmig_service.viewer_api、app）

export interface ViewerAccess {
  export_image: boolean;
  export_data: boolean;
  underlying_data: boolean;
  download_workbook: boolean;
  share_view: boolean;
  view_comments: boolean;
  add_comment: boolean;
  /** 伺服器有報表庫：我的最愛、自訂檢視與留言 */
  store: boolean;
}

export interface FavoriteItem {
  report: string;
  view: string | null;
  added_at: string;
  report_name: string;
  view_name: string | null;
}

export interface RecentItem {
  report: string;
  view: string;
  view_name: string;
  report_name: string;
  kind: string;
  time: string;
}

export interface CustomViewItem {
  id: string;
  report: string;
  view: string;
  name: string;
  owner: string;
  public: boolean;
  state: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  mine: boolean;
}

export interface CommentItem {
  id: string;
  user: string;
  text: string;
  created_at: string;
  can_delete: boolean;
}

export interface UnderlyingData {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
  limit: number;
}

export type DownloadKind = "crosstab" | "summary" | "underlying";

const wb = (id: string) => `/api/workbooks/${encodeURIComponent(id)}`;
const viewPath = (id: string, view: string) => `${wb(id)}/views/${encodeURIComponent(view)}`;
const json = (method: string, body: unknown) => ({ method, body: JSON.stringify(body) });

export const viewerApi = {
  access: (workbook: string) => request<ViewerAccess>(`${wb(workbook)}/access`),
  underlying: (workbook: string, worksheet: string, state: QueryState, limit = 1000) =>
    request<UnderlyingData>(
      `${wb(workbook)}/worksheets/${encodeURIComponent(worksheet)}/underlying`,
      json("POST", { state, limit }),
    ),
  favorites: () => request<FavoriteItem[]>("/api/favorites"),
  setFavorite: (report: string, view: string | null, on: boolean) =>
    request<FavoriteItem[]>("/api/favorites", json("PUT", { report, view, on })),
  recents: () => request<RecentItem[]>("/api/recents"),
  customViews: (workbook: string, view: string) =>
    request<{ views: CustomViewItem[]; default: string | null; can_share: boolean }>(
      `${viewPath(workbook, view)}/custom-views`,
    ),
  saveView: (workbook: string, view: string, body: Record<string, unknown>) =>
    request<CustomViewItem>(`${viewPath(workbook, view)}/custom-views`, json("POST", body)),
  updateView: (id: string, body: Record<string, unknown>) =>
    request<CustomViewItem>(`/api/custom-views/${encodeURIComponent(id)}`, json("PATCH", body)),
  deleteView: (id: string) => request<void>(`/api/custom-views/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setDefault: (workbook: string, view: string, customView: string | null) =>
    request<{ default: string | null }>(
      `${viewPath(workbook, view)}/default-view`,
      json("PUT", { custom_view: customView }),
    ),
  comments: (workbook: string, view: string) =>
    request<{ can_add: boolean; comments: CommentItem[] }>(`${viewPath(workbook, view)}/comments`),
  addComment: (workbook: string, view: string, text: string) =>
    request<CommentItem>(`${viewPath(workbook, view)}/comments`, json("POST", { text })),
  deleteComment: (id: string) => request<void>(`/api/comments/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/** 下載工作表的交叉資料表、摘要資料或完整資料（檔案）。 */
export async function downloadSheet(
  workbook: string,
  worksheet: string,
  state: QueryState,
  kind: DownloadKind,
  format: "csv" | "xlsx",
): Promise<{ blob: Blob; name: string }> {
  const response = await fetch(`${wb(workbook)}/worksheets/${encodeURIComponent(worksheet)}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state, kind, format }),
  });
  if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!response.ok) {
    let text = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") text = body.detail;
    } catch {
      // 回應不是 JSON 時沿用狀態文字
    }
    throw new ApiError(response.status, text, `${response.status} ${text}`);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
  const name = match ? decodeURIComponent(match[1]) : `${worksheet}.${format}`;
  return { blob: await response.blob(), name };
}
