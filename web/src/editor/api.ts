// 桌面版編輯器的 API（/api/edit）：資料窗格、工作表、計算欄位與篩選的可選值。

import { request } from "../api";
import type { Action, Dashboard, Domain, FieldUse, Story } from "../types";
import type { Catalog, CatalogField, SheetIR } from "./model";

const edit = (workbook: string) => `/api/edit/${encodeURIComponent(workbook)}`;
const part = encodeURIComponent;

export interface FormulaCheck {
  ok: boolean;
  datatype?: string;
  level?: string;
  role?: "dimension" | "measure";
  message?: string;
}

/** dashboard 與 Story 編輯器的概要。 */
export interface Outline {
  worksheets: {
    id: string;
    name: string;
    datasource: string;
    filters: { id: string; kind: string; field: FieldUse; label: string }[];
    dimensions: FieldUse[];
    legends: ("color" | "size" | "shape")[];
  }[];
  dashboards: { id: string; name: string }[];
  stories: { id: string; name: string }[];
  parameters: { id: string; caption: string; datatype: string; allowed: "all" | "list" | "range" }[];
  actions: Action[];
  images: string[];
}

export const editApi = {
  outline: (workbook: string) => request<Outline>(`${edit(workbook)}/outline`),
  dashboard: (workbook: string, dashboard: string) =>
    request<{ dashboard: Dashboard }>(`${edit(workbook)}/dashboards/${part(dashboard)}`).then((r) => r.dashboard),
  saveDashboard: (workbook: string, dashboard: Dashboard) =>
    request<{ dashboard: Dashboard }>(`${edit(workbook)}/dashboards/${part(dashboard.id)}`, {
      method: "PUT",
      body: JSON.stringify({ dashboard }),
    }).then((r) => r.dashboard),
  addDashboard: (workbook: string, name?: string, width?: number, height?: number) =>
    request<{ dashboard: Dashboard }>(`${edit(workbook)}/dashboards`, {
      method: "POST",
      body: JSON.stringify({ name: name ?? null, width: width ?? 1000, height: height ?? 800 }),
    }).then((r) => r.dashboard),
  duplicateDashboard: (workbook: string, dashboard: string) =>
    request<{ dashboard: Dashboard }>(`${edit(workbook)}/dashboards/${part(dashboard)}/duplicate`, {
      method: "POST",
      body: "{}",
    }).then((r) => r.dashboard),
  removeDashboard: (workbook: string, dashboard: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/dashboards/${part(dashboard)}`, { method: "DELETE" }),
  story: (workbook: string, story: string) =>
    request<{ story: Story }>(`${edit(workbook)}/stories/${part(story)}`).then((r) => r.story),
  saveStory: (workbook: string, story: Story) =>
    request<{ story: Story }>(`${edit(workbook)}/stories/${part(story.id)}`, {
      method: "PUT",
      body: JSON.stringify({ story }),
    }).then((r) => r.story),
  addStory: (workbook: string, name?: string) =>
    request<{ story: Story }>(`${edit(workbook)}/stories`, {
      method: "POST",
      body: JSON.stringify({ name: name ?? null }),
    }).then((r) => r.story),
  removeStory: (workbook: string, story: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/stories/${part(story)}`, { method: "DELETE" }),
  saveAction: (workbook: string, action: Record<string, unknown>) =>
    request<{ action: Action }>(`${edit(workbook)}/actions`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }).then((r) => r.action),
  removeAction: (workbook: string, action: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/actions/${part(action)}`, { method: "DELETE" }),
  catalog: (workbook: string) => request<Catalog>(`${edit(workbook)}/catalog`),
  worksheet: (workbook: string, worksheet: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}`).then((r) => r.worksheet),
  saveWorksheet: (workbook: string, sheet: SheetIR) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(sheet.id)}`, {
      method: "PUT",
      body: JSON.stringify({ worksheet: sheet }),
    }).then((r) => r.worksheet),
  addWorksheet: (workbook: string, name?: string, datasource?: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets`, {
      method: "POST",
      body: JSON.stringify({ name: name ?? null, datasource: datasource ?? null }),
    }).then((r) => r.worksheet),
  duplicateWorksheet: (workbook: string, worksheet: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}/duplicate`, {
      method: "POST",
      body: "{}",
    }).then((r) => r.worksheet),
  removeWorksheet: (workbook: string, worksheet: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/worksheets/${part(worksheet)}`, { method: "DELETE" }),
  checkFormula: (workbook: string, datasource: string, formula: string, field?: string | null) =>
    request<FormulaCheck>(`${edit(workbook)}/formula`, {
      method: "POST",
      body: JSON.stringify({ datasource, formula, field: field ?? null }),
    }),
  saveCalc: (workbook: string, datasource: string, caption: string, formula: string, field?: string | null) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/calcs`, {
      method: "POST",
      body: JSON.stringify({ datasource, caption, formula, field: field ?? null }),
    }).then((r) => r.field),
  removeCalc: (workbook: string, datasource: string, field: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/calcs/${part(field)}`, {
      method: "DELETE",
    }),
  saveParameter: (workbook: string, parameter: Record<string, unknown>) =>
    request<{ parameter: Record<string, unknown> }>(`${edit(workbook)}/parameters`, {
      method: "POST",
      body: JSON.stringify({ parameter }),
    }),
  removeParameter: (workbook: string, parameter: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/parameters/${part(parameter)}`, { method: "DELETE" }),
  newId: (workbook: string, prefix: string, text: string) =>
    request<{ id: string }>(`${edit(workbook)}/ids`, {
      method: "POST",
      body: JSON.stringify({ prefix, text }),
    }).then((r) => r.id),
  /** 篩選對話框：維度的值或範圍；量值為工作表上各標記的範圍（不套用 excludeFilter 這個篩選） */
  domain: (
    workbook: string,
    use: FieldUse,
    options: { kind?: "values" | "range"; worksheet?: string; excludeFilter?: string | null } = {},
  ) =>
    request<Domain>(`${edit(workbook)}/domain`, {
      method: "POST",
      body: JSON.stringify({
        use,
        kind: options.kind ?? null,
        worksheet: options.worksheet ?? null,
        exclude_filter: options.excludeFilter ?? null,
      }),
    }),
};
