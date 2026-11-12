// 桌面版編輯器的 API（/api/edit）：資料窗格、工作表、計算欄位與篩選的可選值。

import { request } from "../api";
import type { Action, Dashboard, Domain, FieldUse, Scalar, Story } from "../types";
import type { Catalog, CatalogField, CatalogHierarchy, GeoAlias, GeoRole, SheetIR } from "./model";

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
    /** 可以加入醒目提示控制項的欄位（欄列架與標記卡上的維度） */
    highlighters?: { field: FieldUse; label: string }[];
    legends: ("color" | "size" | "shape")[];
    /** 頁面架上的欄位（顯示名稱）；沒有頁面架為 null */
    pages: string | null;
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
  /** 分析的欄位：新的叢集欄位，或資料源的 Forecast Indicator（已經有時回傳既有的） */
  addAnalyticsField: (workbook: string, datasource: string, kind: "cluster" | "forecast_indicator") =>
    request<{ field: CatalogField }>(`${edit(workbook)}/analytics-fields`, {
      method: "POST",
      body: JSON.stringify({ datasource, kind }),
    }).then((r) => r.field),
  removeCalc: (workbook: string, datasource: string, field: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/calcs/${part(field)}`, {
      method: "DELETE",
    }),
  /** 建立階層，或改變 original 這個階層的名稱與欄位（沒有欄位時刪除，回傳 null） */
  saveHierarchy: (workbook: string, datasource: string, name: string, fields: string[], original?: string | null) =>
    request<{ hierarchy: CatalogHierarchy | null }>(`${edit(workbook)}/datasources/${part(datasource)}/hierarchies`, {
      method: "PUT",
      body: JSON.stringify({ name, fields, original: original ?? null }),
    }).then((r) => r.hierarchy),
  removeHierarchy: (workbook: string, datasource: string, name: string) =>
    request<{ ok: boolean }>(
      `${edit(workbook)}/datasources/${part(datasource)}/hierarchies?name=${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  /** 欄位的地理角色（null 取消）與編輯位置中指定的地名（省略時不變） */
  saveGeography: (workbook: string, datasource: string, field: string, role: GeoRole | null, aliases?: GeoAlias[]) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/geography`, {
      method: "PUT",
      body: JSON.stringify({ role, ...(aliases ? { aliases } : {}) }),
    }),
  /** 編輯位置的預設國家、州 */
  saveGeoDefaults: (workbook: string, datasource: string, country: string | null, state: string | null) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/geography`, {
      method: "PUT",
      body: JSON.stringify({ country, state }),
    }),
  /** 欄位目前的不重複值（含 LOD 的計算欄位），轉成參數的型別：動態參數的目前值與允許值 */
  fieldValues: (workbook: string, field: string, datatype?: string | null) =>
    request<{ values: Scalar[]; more: boolean }>(`${edit(workbook)}/field-values`, {
      method: "POST",
      body: JSON.stringify({ field, datatype: datatype ?? null }),
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
