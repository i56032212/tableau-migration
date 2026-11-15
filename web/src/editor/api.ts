// 桌面版編輯器的 API（/api/edit）：資料窗格、工作表、計算欄位與篩選的可選值。

import { request } from "../api";
import type { Action, Dashboard, Domain, FieldUse, MemberAlias, Scalar, Story } from "../types";
import type { Catalog, CatalogField, CatalogHierarchy, FilterIR, GeoAlias, GeoRole, SetRuleIR, SheetIR } from "./model";

const edit = (workbook: string) => `/api/edit/${encodeURIComponent(workbook)}`;
const part = encodeURIComponent;

/** 建立或修改集合時送出的規則（固定成員、條件或 Top／Bottom N）。 */
export type SetRuleBody = SetRuleIR;

/** 資料源的萃取設定（資料來源分頁的「即時／萃取」） */
export interface ExtractInfo {
  mode: "live" | "extract" | "file";
  switchable: boolean;
  reason: string | null;
  /** 可以從來源更新（知道來源資料庫與各欄的位置） */
  refreshable: boolean;
  increment: string | null;
  row_limit: number | null;
  /** 只取前 N 筆（編輯器的設定）；彙總可見維度的資料 */
  top_rows: number | null;
  aggregate: boolean;
  can_aggregate: boolean;
  filters: FilterIR[];
  increment_fields: { id: string; caption: string }[];
  /** 上次從來源更新的時間（ISO 8601） */
  refreshed: string | null;
}

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
  /** 可以放集合控制項的集合（固定成員、單一維度） */
  sets?: { id: string; caption: string }[];
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
  /** 篩選可以套用到的其他工作表（同一個資料源）與目前是否已套用 */
  filterTargets: (workbook: string, worksheet: string, filter: string) =>
    request<{ worksheets: { id: string; name: string; applied: boolean }[] }>(
      `${edit(workbook)}/worksheets/${part(worksheet)}/filters/${part(filter)}/apply`,
    ).then((r) => r.worksheets),
  /** 把篩選套用到其他工作表（空的表示只套用到這張工作表）；回傳這張工作表 */
  applyFilter: (workbook: string, worksheet: string, filter: string, targets: string[]) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}/filters/${part(filter)}/apply`, {
      method: "POST",
      body: JSON.stringify({ targets }),
    }).then((r) => r.worksheet),
  /** 資料源篩選（資料來源分頁的「篩選條件」） */
  datasourceFilters: (workbook: string, datasource: string, stage: "datasource" | "extract" = "datasource") =>
    request<{ filters: FilterIR[] }>(`${edit(workbook)}/datasources/${part(datasource)}/filters?stage=${stage}`),
  saveDatasourceFilters: (workbook: string, datasource: string, filters: FilterIR[], stage: "datasource" | "extract" = "datasource") =>
    request<{ filters: FilterIR[] }>(`${edit(workbook)}/datasources/${part(datasource)}/filters?stage=${stage}`, {
      method: "PUT",
      body: JSON.stringify({ filters }),
    }),
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
  /** 複製成交叉資料表：新的工作表是這個檢視的文字表 */
  duplicateAsCrosstab: (workbook: string, worksheet: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}/crosstab`, {
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
  /** 臨時計算（直接在架上輸入的公式）：名稱就是公式，公式相同的只建立一個；回傳欄位的 id */
  saveAdHoc: (workbook: string, datasource: string, formula: string) =>
    request<{ field: { id: string } }>(`${edit(workbook)}/calcs`, {
      method: "POST",
      body: JSON.stringify({ datasource, formula, ad_hoc: true }),
    }).then((r) => r.field.id),
  /** 分割、自訂分割：建立「<名稱> - Split N」的計算欄位 */
  splitField: (workbook: string, datasource: string, field: string, separator: string, count: number, fromEnd = false) =>
    request<{ fields: { id: string; caption: string }[] }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/split`, {
      method: "POST",
      body: JSON.stringify({ separator, count, from_end: fromEnd }),
    }).then((r) => r.fields),
  /** 取代參照：用到 field 的地方改用 replacement */
  replaceReferences: (workbook: string, datasource: string, field: string, replacement: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/replace`, {
      method: "POST",
      body: JSON.stringify({ replacement }),
    }),
  /** 分析的欄位：新的叢集欄位，或資料源的 Forecast Indicator（已經有時回傳既有的） */
  addAnalyticsField: (workbook: string, datasource: string, kind: "cluster" | "forecast_indicator") =>
    request<{ field: CatalogField }>(`${edit(workbook)}/analytics-fields`, {
      method: "POST",
      body: JSON.stringify({ datasource, kind }),
    }).then((r) => r.field),
  /** 複製欄位（「<名稱> (copy)」） */
  duplicateField: (workbook: string, datasource: string, field: string) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/duplicate`, {
      method: "POST",
    }).then((r) => r.field),
  saveAliases: (workbook: string, datasource: string, field: string, aliases: MemberAlias[]) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/aliases`, {
      method: "PUT",
      body: JSON.stringify({ aliases }),
    }),
  /** 預設的數字或日期格式、註解（只改送出的項目；null 為清除） */
  saveFieldProperties: (
    workbook: string,
    datasource: string,
    field: string,
    body: {
      format?: string | null;
      comment?: string | null;
      fiscal_year_start?: number | null;
      default_color?: Record<string, unknown> | null;
      default_shapes?: { value: Scalar; shape: string }[];
      default_sort?: { kind: "alphabetic" | "manual"; order: "asc" | "desc"; manual_values: Scalar[] } | null;
      total_using?: "sum" | "avg" | "min" | "max" | null;
    },
  ) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}/properties`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 資料源的日期屬性：週起始日、會計年度起始月 */
  saveDateProperties: (workbook: string, datasource: string, body: { week_start?: string; fiscal_year_start?: number }) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/date-properties`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 合併欄位 */
  createCombined: (workbook: string, datasource: string, fields: string[]) =>
    request<{ field: { id: string; caption: string } }>(`${edit(workbook)}/datasources/${part(datasource)}/combined`, {
      method: "POST",
      body: JSON.stringify({ fields }),
    }),
  /** 資料夾：建立、改名、刪除、移入或移出欄位 */
  createFolder: (workbook: string, datasource: string, name: string, fields: string[]) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/folders`, {
      method: "POST",
      body: JSON.stringify({ name, fields }),
    }),
  renameFolder: (workbook: string, datasource: string, folder: string, name: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/folders/${part(folder)}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    }),
  removeFolder: (workbook: string, datasource: string, folder: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/folders/${part(folder)}`, { method: "DELETE" }),
  moveToFolder: (workbook: string, datasource: string, fields: string[], folder: string | null) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/folder-items`, {
      method: "PUT",
      body: JSON.stringify({ fields, folder }),
    }),
  /** 刪除計算欄位、群組、數值分組或集合 */
  removeField: (workbook: string, datasource: string, field: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}`, {
      method: "DELETE",
    }),
  saveGroup: (
    workbook: string,
    datasource: string,
    body: { source: string; caption: string; groups: { name: string; members: Scalar[] }[]; other: string | null; field: string | null },
  ) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/datasources/${part(datasource)}/groups`, {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.field),
  saveBins: (
    workbook: string,
    datasource: string,
    body: { source: string; caption: string; size: number | null; parameter: string | null; field: string | null },
  ) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/datasources/${part(datasource)}/bins`, {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.field),
  saveSet: (workbook: string, datasource: string, body: { levels: string[]; caption: string; rule: SetRuleBody; field: string | null }) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/datasources/${part(datasource)}/sets`, {
      method: "POST",
      body: JSON.stringify(body),
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
    options: { kind?: "values" | "range"; worksheet?: string; excludeFilter?: string | null; fromSource?: boolean } = {},
  ) =>
    request<Domain>(`${edit(workbook)}/domain`, {
      method: "POST",
      body: JSON.stringify({
        use,
        kind: options.kind ?? null,
        worksheet: options.worksheet ?? null,
        exclude_filter: options.excludeFilter ?? null,
        from_source: options.fromSource === true,
      }),
    }),
  /** 匯入另一份報表的工作表；worksheets 為 null 時整份匯入（含 dashboard、Story 與動作） */
  importSheets: (workbook: string, source: string, worksheets: string[] | null) =>
    request<{ worksheets: string[]; dashboards: string[]; stories: string[]; datasources: Record<string, string>; shared_parameters: string[] }>(
      `${edit(workbook)}/import`,
      { method: "POST", body: JSON.stringify(worksheets === null ? { source } : { source, worksheets }) },
    ),
  /** 萃取設定：模式、能不能切換、增量更新的欄位、萃取篩選與上次更新的時間 */
  extract: (workbook: string, datasource: string) => request<ExtractInfo>(`${edit(workbook)}/datasources/${part(datasource)}/extract`),
  saveExtract: (workbook: string, datasource: string, settings: { increment: string | null; top_rows: number | null; aggregate: boolean }) =>
    request<ExtractInfo>(`${edit(workbook)}/datasources/${part(datasource)}/extract`, {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  /** 即時與萃取的切換（改成萃取時立即從來源建立資料） */
  extractMode: (workbook: string, datasource: string, mode: "live" | "extract") =>
    request<ExtractInfo>(`${edit(workbook)}/datasources/${part(datasource)}/extract/mode`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  refreshExtract: (workbook: string, datasource: string, incremental: boolean) =>
    request<ExtractInfo>(`${edit(workbook)}/datasources/${part(datasource)}/extract/refresh`, {
      method: "POST",
      body: JSON.stringify({ incremental }),
    }),
};
