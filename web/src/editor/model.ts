// 工作表編輯器的資料模型：IR 的工作表（只列出編輯器會改的部分，其餘欄位原樣保留）與資料窗格。
// 欄列架、標記卡上的每一個「膠囊」是一個 FieldUse；編輯時產生新的工作表物件（不改動舊的，方便復原）。

import type { FieldUse, Mark, MemberAlias, Scalar } from "../types";

export interface CatalogField {
  id: string;
  name: string;
  caption: string;
  role: "dimension" | "measure";
  datatype: string;
  kind: "column" | "calc" | "set";
  formula: string | null;
  level: "row" | "aggregate" | "lod" | "table_calc" | null;
  default_agg: string | null;
  hidden: boolean;
  /** 地理角色（country、state、county、city、zip、latitude、longitude） */
  geo_role?: GeoRole | null;
  /** 編輯位置中指定的地名 */
  geo_aliases?: GeoAlias[];
  /** 成員的別名 */
  aliases?: MemberAlias[];
  /** 群組的定義（來源欄位、各組與「其他」） */
  group?: { source: string; groups: { name: Scalar; members: Scalar[] }[]; other: Scalar } | null;
  /** 數值分組的定義（大小為數字或參數） */
  bin?: { source: string; size: number | { parameter: string }; peg: number } | null;
  /** 集合的定義（層級與規則） */
  set?: { levels: FieldUse[]; rule: SetRuleIR } | null;
  /** 預設的數字或日期格式字串 */
  format?: string | null;
  /** 註解（預設屬性） */
  comment?: string | null;
  /** 資料表中的欄名（計算欄位、集合為 null） */
  column?: string | null;
  /** 日期欄位自訂的會計年度起始月（null 沿用資料源） */
  fiscal_year_start?: number | null;
  /** 合併欄位的組成欄位 */
  combined?: string[] | null;
  /** 臨時計算（直接在架上輸入的公式）：資料窗格不顯示，名稱就是公式 */
  ad_hoc?: boolean;
  /** 所屬的資料表（萃取為 null） */
  table?: string | null;
  /** 預設屬性：色彩、形狀、排序、總計使用 */
  default_color?: Record<string, unknown> | null;
  default_shapes?: { value: Scalar; shape: string; image?: string | null }[];
  default_sort?: { kind: "alphabetic" | "manual" | "by_field"; order: "asc" | "desc"; manual_values: Scalar[]; by?: FieldUse | null } | null;
  total_using?: "sum" | "avg" | "min" | "max" | null;
}

/** 資料窗格的資料夾（role 為 null 時不分維度、量值）。 */
export interface CatalogFolder {
  name: string;
  fields: string[];
  role: "dimension" | "measure" | null;
}

/** 集合的規則：固定成員（包含或排除）、條件或 Top／Bottom N。 */
export type SetRuleIR =
  | { kind: "members"; mode: "include" | "exclude"; members: Scalar[][] }
  | { kind: "condition"; condition: string; mode?: string; values?: Scalar[] }
  | { kind: "top_n"; direction: "top" | "bottom"; count: number | { parameter: string }; by: string; mode?: string; values?: Scalar[] }
  /** 合併集合：同一個維度上兩個集合的聯集、交集或差集 */
  | { kind: "combined"; left: string; right: string; op: "union" | "intersect" | "left_only" | "right_only" };

export type GeoRole = "country" | "state" | "county" | "city" | "zip" | "latitude" | "longitude";

export interface GeoAlias {
  value: string;
  location: string;
}

export const GEO_ROLES: [GeoRole, string][] = [
  ["country", "國家/地區"],
  ["state", "州/省/直轄市/縣市"],
  ["county", "郡（美國）"],
  ["city", "城市"],
  ["zip", "郵遞區號（美國）"],
  ["latitude", "緯度"],
  ["longitude", "經度"],
];

/** 有區域形狀（填色地圖）的地理角色；郵遞區號在平台上以中心點的圓點呈現。 */
const SHAPED_ROLES = new Set<string>(["country", "state", "county", "zip"]);

/** 階層（Tableau 的 drill-path）：由上到下的維度 */
export interface CatalogHierarchy {
  name: string;
  fields: string[];
}

export interface CatalogDatasource {
  id: string;
  caption: string;
  fields: CatalogField[];
  hierarchies?: CatalogHierarchy[];
  /** 資料窗格的資料夾 */
  folders?: CatalogFolder[];
  /** 日期屬性：週起始日、會計年度起始月 */
  settings?: { week_start: string; fiscal_year_start: number };
  /** 編輯位置的預設國家、州 */
  geo?: { country: string | null; state: string | null } | null;
}

export interface CatalogParameter {
  id: string;
  caption: string;
  name: string;
  datatype: string;
  value: Scalar;
  allowed: { kind: "all" | "list" | "range"; values?: Scalar[]; min?: Scalar; max?: Scalar; step?: number | null };
  aliases: { value: Scalar; label: string }[];
  /** 動態參數：開啟報表時目前值取自這個欄位 */
  value_field?: string | null;
  /** 動態參數：開啟報表時允許值的清單或範圍取自這個欄位 */
  domain_field?: string | null;
}

export interface CatalogPalette {
  name: string;
  kind: "regular" | "sequential" | "diverging" | string;
  colors: string[];
}

export interface Catalog {
  datasources: CatalogDatasource[];
  parameters: CatalogParameter[];
  palettes: CatalogPalette[];
}

export interface TableCalcIR {
  id: string;
  type: string;
  preset: string;
  addressing: string[];
  instances: FieldUse[];
  level: FieldUse | null;
  restart: FieldUse | null;
  inferred: boolean;
  options: Record<string, Scalar>;
  nested: unknown[];
  /** 次要計算：主要計算（累計、移動計算）的結果再做一次表計算 */
  secondary?: { type: string; addressing: string[]; options: Record<string, Scalar>; level?: FieldUse | null; restart?: FieldUse | null } | null;
  /** 自訂排序順序：沿著計算方向的標記依這個彙總值排列 */
  sort?: { by: FieldUse; order: "asc" | "desc" } | null;
  source_ref: string;
}

export interface SortIR {
  field: string;
  kind: "by_field" | "alphabetic" | "manual" | "data_source";
  order: "asc" | "desc";
  by: FieldUse | null;
  /** 巢狀排序：在同一架上外層維度的每個值之內各自排序 */
  nested?: boolean;
  manual_values: Scalar[];
}

/** 工具提示中的工作表（Viz in Tooltip） */
export interface TooltipSheetIR {
  worksheet: string;
  max_width: number;
  max_height: number;
  /** 以標記的哪些欄位篩選；null 為所有欄位 */
  fields: FieldUse[] | null;
}

export interface MarksIR {
  type: string;
  color: FieldUse | null;
  size: FieldUse | null;
  label: FieldUse[];
  detail: FieldUse[];
  tooltip: FieldUse[];
  shape: FieldUse | null;
  angle: FieldUse | null;
  path: FieldUse | null;
  /** 地圖上標記的形狀：Geometry (generated)，或空間資料的 COLLECT（Shapefile 的 Geometry、MAKEPOINT 等） */
  geometry?: FieldUse | null;
  tooltip_sheets?: TooltipSheetIR[];
  /** 色彩上的其他維度（以組合著色）：同時在詳細資料中 */
  color_more?: FieldUse[];
  /** 形狀欄位中指定給各成員的形狀（內建的形狀或自訂的圖片） */
  shape_assignments?: { value: Scalar; shape: string; image?: string | null }[];
  /** 形狀上沒有欄位時所有標記的形狀 */
  mark_shape?: { shape: string; image?: string | null } | null;
  [key: string]: unknown;
}

export interface FilterIR {
  kind: string;
  id: string;
  field: FieldUse;
  stage: string;
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  ranges?: unknown[];
  min?: Scalar;
  max?: Scalar;
  include_null?: boolean;
  /** 範圍篩選：只保留 Null 值（特殊值分頁） */
  only_null?: boolean;
  group?: string | null;
  source_ref: string;
  [key: string]: unknown;
}

export interface PaneIR {
  measure: FieldUse;
  axis: "rows" | "columns";
  dual: boolean;
  marks: MarksIR;
}

export interface SheetIR {
  id: string;
  name: string;
  datasource: string;
  rows: FieldUse[];
  columns: FieldUse[];
  marks: MarksIR;
  panes: PaneIR[];
  measure_values: FieldUse[];
  subtotals: FieldUse[];
  filters: FilterIR[];
  sorts: SortIR[];
  table_calcs: TableCalcIR[];
  totals: ("rows" | "columns")[];
  fit: string;
  [key: string]: unknown;
}

export const EDITOR_REF = "/editor";

/** 標記卡上的位置：單一欄位或多個欄位。 */
export type MarkSlot = "color" | "size" | "shape" | "angle" | "path" | "label" | "detail" | "tooltip" | "geometry";
export const LIST_SLOTS: MarkSlot[] = ["label", "detail", "tooltip"];
export type ShelfName = "rows" | "columns" | "filters" | "pages" | "measure_values" | `marks.${MarkSlot}`;

export const MARK_TYPES: [string, string][] = [
  ["automatic", "自動"],
  ["bar", "長條"],
  ["line", "線"],
  ["area", "區域"],
  ["square", "方形"],
  ["circle", "圓形"],
  ["shape", "形狀"],
  ["text", "文字"],
  ["pie", "圓餅"],
  ["gantt", "甘特條"],
  ["polygon", "多邊形"],
  ["map", "地圖"],
  ["density", "密度"],
];

export const AGGREGATIONS: [string, string][] = [
  ["sum", "總和"],
  ["avg", "平均"],
  ["median", "中位數"],
  ["count", "計數"],
  ["countd", "相異計數"],
  ["min", "最小值"],
  ["max", "最大值"],
  ["pct5", "第 5 百分位數"],
  ["pct10", "第 10 百分位數"],
  ["pct25", "第 25 百分位數"],
  ["pct50", "第 50 百分位數"],
  ["pct75", "第 75 百分位數"],
  ["pct90", "第 90 百分位數"],
  ["pct95", "第 95 百分位數"],
  ["stdev", "標準差"],
  ["stdevp", "母體標準差"],
  ["var", "變異數"],
  ["varp", "母體變異數"],
  ["attr", "屬性"],
];

const AGG_NAMES: Record<string, string> = {
  sum: "SUM",
  avg: "AVG",
  median: "MEDIAN",
  count: "CNT",
  countd: "CNTD",
  min: "MIN",
  max: "MAX",
  stdev: "STDEV",
  stdevp: "STDEVP",
  var: "VAR",
  varp: "VARP",
  attr: "ATTR",
};

/** 日期的層級：離散的日期部分與連續的日期截斷（Tableau 膠囊選單上半、下半）。 */
export const DATE_PARTS: [string, string][] = [
  ["year", "年"],
  ["quarter", "季"],
  ["month", "月"],
  ["week", "週"],
  ["weekday", "星期"],
  ["day", "日"],
  ["hour", "時"],
  ["minute", "分"],
];
export const DATE_TRUNCS: [string, string][] = [
  ["year", "年"],
  ["quarter", "季"],
  ["month", "月"],
  ["week", "週"],
  ["day", "日"],
  ["hour", "時"],
  ["minute", "分"],
];
const DATE_NAMES: Record<string, string> = {
  year: "YEAR",
  quarter: "QUARTER",
  month: "MONTH",
  week: "WEEK",
  weekday: "WEEKDAY",
  day: "DAY",
  hour: "HOUR",
  minute: "MINUTE",
  second: "SECOND",
};

/** 快速 Table Calculation（膠囊選單）。 */
export const QUICK_CALCS: [string, string, Record<string, Scalar>][] = [
  ["running_total", "累計", { aggregation: "sum" }],
  ["difference", "差異", { offset: -1 }],
  ["percent_difference", "差異百分比", { offset: -1 }],
  ["percent_of_total", "合計百分比", {}],
  ["rank", "排名", { rank_type: "competition", direction: "desc" }],
  ["percentile", "百分位數", { direction: "asc" }],
  ["moving", "移動平均", { aggregation: "avg", start: -2, end: 0, null_if_incomplete: false }],
];

export const DIRECTIONS: [string, string][] = [
  ["table_across", "表格（橫向）"],
  ["table_down", "表格（縱向）"],
  ["table_across_then_down", "表格（橫向再縱向）"],
  ["table_down_then_across", "表格（縱向再橫向）"],
  ["pane_across", "窗格（橫向）"],
  ["pane_down", "窗格（縱向）"],
  ["pane_across_then_down", "窗格（橫向再縱向）"],
  ["pane_down_then_across", "窗格（縱向再橫向）"],
  ["cell", "儲存格"],
];

/** 欄位 id → 欄位：工作表的資料源在前，其他資料源（Data Blending 的次要資料源）在後；欄位 id 全報表唯一。 */
export function fieldIndex(catalog: Catalog, datasource: string): Map<string, CatalogField> {
  const ordered = [...catalog.datasources].sort((a, b) => Number(b.id === datasource) - Number(a.id === datasource));
  return new Map(ordered.flatMap((d) => d.fields.map((f) => [f.id, f] as [string, CatalogField])));
}

/** 欄位所屬的資料源。 */
export function fieldDatasource(catalog: Catalog, field: string): string | undefined {
  return catalog.datasources.find((d) => d.fields.some((f) => f.id === field))?.id;
}

/** 工作表的次要資料源（Data Blending）。 */
export function blendSources(sheet: SheetIR): string[] {
  return ((sheet.blends as { datasource: string }[] | undefined) ?? []).map((b) => b.datasource);
}

export const isDate = (field: CatalogField | undefined) =>
  field !== undefined && (field.datatype === "date" || field.datatype === "datetime");

/** 欄位放到架上的預設：量值為彙總（預設的彙總或 SUM）且連續；日期維度為離散的年；其他維度為離散；
 * 空間資料為 COLLECT。 */
export function defaultUse(field: CatalogField): FieldUse {
  const base: FieldUse = { field: field.id, agg: null, date: null, continuous: false, table_calc: null };
  if (field.datatype === "spatial") return { ...base, agg: "collect" };
  if (field.level === "aggregate" || field.level === "table_calc") return { ...base, continuous: true };
  if (field.role === "measure") return { ...base, agg: field.default_agg ?? "sum", continuous: true };
  if (isDate(field)) return { ...base, date: { kind: "part", unit: "year" } };
  return base;
}

/** 量值（彙總、Table Calculation、彙總的計算欄位）或維度。 */
export function isMeasure(use: FieldUse, fields: Map<string, CatalogField>): boolean {
  if (use.agg !== null || use.table_calc !== null) return true;
  const field = fields.get(use.field);
  return field?.level === "aggregate" || field?.level === "table_calc";
}

/** 膠囊上的文字（與 Tableau 相同：SUM(Sales)、YEAR(Order Date)）。 */
export function pillLabel(use: FieldUse, fields: Map<string, CatalogField>, calcs: TableCalcIR[]): string {
  const field = fields.get(use.field);
  let text = field?.caption ?? GENERATED_LABELS[use.field] ?? use.field;
  if (use.date) {
    const name = DATE_NAMES[use.date.unit] ?? use.date.unit.toUpperCase();
    text = `${name}(${text})`;
  }
  if (use.agg) text = `${AGG_NAMES[use.agg] ?? use.agg.toUpperCase()}(${text})`;
  if (use.table_calc) {
    const tc = calcs.find((c) => c.id === use.table_calc);
    if (tc && tc.type !== "formula") text += " △";
  }
  if (use.forecast && use.forecast !== "none") text += `（${FORECAST_RESULT_NAMES[use.forecast] ?? use.forecast}）`;
  return text;
}

/** 預測結果（量值膠囊選單的「預測結果」，Tableau 繁體中文版的寫法）；值是 FieldUse.forecast，實際值與預測值為
 * null。與 tmig.render.names 的 FORECAST_RESULT_NAMES 相同。 */
export const FORECAST_RESULTS: [string | null, string][] = [
  [null, "實際值與預測值"],
  ["trend", "趨勢"],
  ["precision", "精度"],
  ["precision_percent", "精度 %"],
  ["quality", "品質"],
  ["upper", "上層預測區間"],
  ["lower", "較低預測區間"],
  ["none", "無"],
];
const FORECAST_RESULT_NAMES: Record<string, string> = Object.fromEntries(
  FORECAST_RESULTS.filter(([value]) => value !== null) as [string, string][],
);

/** 膠囊的公式（在架上編輯時的文字）：臨時計算是它的公式，其他欄位是 [名稱] 加上彙總或日期衍生。 */
export function pillFormula(use: FieldUse, fields: Map<string, CatalogField>): string {
  const field = fields.get(use.field);
  if (!field) return "";
  let text = field.ad_hoc && field.formula ? field.formula : `[${field.caption.replace(/]/g, "]]")}]`;
  if (use.date) {
    text =
      use.date.kind === "part"
        ? `${use.date.unit === "weekday" ? "DATENAME('weekday', " : `DATEPART('${use.date.unit}', `}${text})`
        : `DATETRUNC('${use.date.unit}', ${text})`;
  }
  if (use.agg) {
    const percentile = /^pct(\d+)$/.exec(use.agg);
    text = percentile ? `PERCENTILE(${text}, ${Number(percentile[1]) / 100})` : `${AGG_FUNCTIONS[use.agg] ?? use.agg.toUpperCase()}(${text})`;
  }
  return text;
}

/** 架上輸入的文字只是一個欄位（[Sales]、Sales）或欄位的彙總（SUM([Sales])）時，直接用這個欄位，
 * 不必建立臨時計算；其他公式回傳 null。 */
export function plainFormulaUse(text: string, fields: Map<string, CatalogField>): FieldUse | null {
  const trimmed = text.trim();
  const byCaption = (name: string) => [...fields.values()].find((f) => !f.ad_hoc && f.caption === name.replace(/]]/g, "]"));
  const single = /^\[((?:[^\]]|\]\])+)\]$/.exec(trimmed);
  const bare = byCaption(trimmed);
  const field = single ? byCaption(single[1]) : bare;
  if (field) return defaultUse(field);
  const wrapped = /^([A-Za-z]+)\(\s*\[((?:[^\]]|\]\])+)\]\s*\)$/.exec(trimmed);
  if (!wrapped) return null;
  const agg = Object.entries(AGG_FUNCTIONS).find(([, name]) => name === wrapped[1].toUpperCase())?.[0];
  const target = byCaption(wrapped[2]);
  if (!agg || !target || target.level === "aggregate" || target.level === "table_calc") return null;
  return { field: target.id, agg, date: null, continuous: agg !== "attr", table_calc: null };
}

export function useKey(use: FieldUse): string {
  let key = use.field;
  if (use.date) key = `${use.date.kind === "part" ? use.date.unit : `trunc_${use.date.unit}`}(${key})`;
  if (use.agg) key = `${use.agg}(${key})`;
  if (use.table_calc) key = `${key}#${use.table_calc}`;
  if (use.forecast) key = `${key}~${use.forecast}`;
  return key;
}

/** 標記卡上的所有膠囊。 */
export function markUses(marks: MarksIR): FieldUse[] {
  const singles = [marks.color, marks.size, marks.shape, marks.angle, marks.path].filter(
    (u): u is FieldUse => u !== null && u !== undefined,
  );
  return [...singles, ...marks.label, ...marks.detail, ...marks.tooltip];
}

/** 工作表中所有的膠囊（判斷 Table Calculation、排序是否還有人用）。 */
/** 彙總量值（分析＞彙總量值）：關閉時欄列架與標記卡上的量值不彙總（每一列資料是一個標記），
 * 開啟時改回預設的彙總。日期、Table Calculation 與彙總的計算欄位不變。 */
export function setAggregated(sheet: SheetIR, aggregated: boolean, fields: Map<string, CatalogField>): SheetIR {
  const convert = (use: FieldUse): FieldUse => {
    const field = fields.get(use.field);
    if (!field || field.role !== "measure" || use.date || use.table_calc || field.level === "aggregate" || field.level === "table_calc") return use;
    if (!aggregated && use.agg !== null) return { ...use, agg: null, continuous: true };
    if (aggregated && use.agg === null) return { ...use, agg: field.default_agg ?? "sum", continuous: true };
    return use;
  };
  const card = (marks: MarksIR): MarksIR => {
    const updated: MarksIR = { ...marks };
    for (const slot of ["color", "size", "shape", "angle", "path"] as const) {
      const use = marks[slot] as FieldUse | null | undefined;
      if (use) (updated as Record<string, unknown>)[slot] = convert(use);
    }
    for (const slot of LIST_SLOTS) {
      (updated as Record<string, unknown>)[slot] = ((marks[slot] as FieldUse[] | undefined) ?? []).map(convert);
    }
    return updated;
  };
  return {
    ...sheet,
    disaggregated: !aggregated,
    rows: sheet.rows.map(convert),
    columns: sheet.columns.map(convert),
    marks: card(sheet.marks),
    panes: sheet.panes.map((p) => ({ ...p, measure: convert(p.measure), marks: card(p.marks) })),
    measure_values: sheet.measure_values.map(convert),
  };
}

export function allUses(sheet: SheetIR): FieldUse[] {
  const panes = (sheet.panes as PaneIR[]).flatMap((p) => [p.measure, ...markUses(p.marks)]);
  return [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks), ...panes, ...sheet.measure_values];
}

export const MEASURE_NAMES = "measure_names";
export const MEASURE_VALUES = "measure_values";
export const LATITUDE_GENERATED = "latitude_generated";
export const LONGITUDE_GENERATED = "longitude_generated";
export const GEOMETRY_GENERATED = "geometry_generated";
/** Tableau 依地理角色產生的欄位（資料窗格的 Latitude／Longitude (generated)）。 */
export const GENERATED_LABELS: Record<string, string> = {
  [LATITUDE_GENERATED]: "Latitude (generated)",
  [LONGITUDE_GENERATED]: "Longitude (generated)",
  [GEOMETRY_GENERATED]: "Geometry (generated)",
};
export const isGenerated = (use: FieldUse) => use.field in GENERATED_LABELS;
export const isSpecial = (use: FieldUse) =>
  use.field === MEASURE_NAMES || use.field === MEASURE_VALUES || isGenerated(use);

export function generatedUse(field: string): FieldUse {
  return { field, agg: null, date: null, continuous: field !== GEOMETRY_GENERATED, table_calc: null };
}

/** 地圖：Latitude／Longitude (generated) 在欄列架上。 */
export function isMapSheet(sheet: SheetIR): boolean {
  return [...sheet.rows, ...sheet.columns].some(
    (u) => u.field === LATITUDE_GENERATED || u.field === LONGITUDE_GENERATED,
  );
}

/**
 * 連按兩下地理角色的欄位（與 Tableau 相同）：建立地圖——Latitude／Longitude (generated) 放到列與欄，
 * 欄位放到詳細資料；有區域形狀的角色（國家、州、郡、郵遞區號）畫成填色地圖。已經是地圖時只加到詳細資料。
 */
export function buildMap(sheet: SheetIR, use: FieldUse, role: GeoRole): SheetIR {
  const detail = sheet.marks.detail.some((u) => sameUse(u, use)) ? sheet.marks.detail : [...sheet.marks.detail, use];
  if (isMapSheet(sheet)) return { ...sheet, marks: { ...sheet.marks, detail } };
  const marks: MarksIR = { ...sheet.marks, detail };
  if (SHAPED_ROLES.has(role) && (marks.type === "automatic" || marks.type === "map"))
    marks.geometry = generatedUse(GEOMETRY_GENERATED);
  return {
    ...sheet,
    rows: [generatedUse(LATITUDE_GENERATED)],
    columns: [generatedUse(LONGITUDE_GENERATED)],
    marks,
    panes: [],
  };
}

/**
 * 地圖的標記圖層（Tableau 2020.4 的「新增標記圖層」）：每個圖層是一張個別的標記卡（panes，由下往上畫），
 * 共用的標記卡沒有欄位。第一次新增時，原本的標記卡成為第一個圖層。
 */
export function addMapLayer(sheet: SheetIR): SheetIR {
  const latitude = sheet.rows.find((u) => u.field === LATITUDE_GENERATED);
  if (!latitude) return sheet;
  const blank: MarksIR = { type: "automatic", color: null, size: null, label: [], detail: [], tooltip: [], shape: null, angle: null, path: null };
  const layers: PaneIR[] = sheet.panes.length > 0 ? sheet.panes : [{ measure: latitude, axis: "rows", dual: false, marks: sheet.marks }];
  return {
    ...sheet,
    marks: sheet.panes.length > 0 ? sheet.marks : { ...blank, type: sheet.marks.type },
    panes: [...layers, { measure: latitude, axis: "rows", dual: true, marks: blank }],
  };
}

/** 移除地圖的一個標記圖層；只剩一個圖層時改回單一張標記卡。 */
export function removeMapLayer(sheet: SheetIR, index: number): SheetIR {
  const panes = sheet.panes.filter((_, i) => i !== index).map((p, i) => ({ ...p, dual: i > 0 }));
  if (panes.length === 1) return { ...sheet, marks: panes[0].marks, panes: [] };
  return { ...sheet, panes };
}

/** 地圖的標記圖層往上（delta 為 1，畫在後面的圖層上方）或往下移。 */
export function moveMapLayer(sheet: SheetIR, index: number, delta: number): SheetIR {
  const target = index + delta;
  if (target < 0 || target >= sheet.panes.length) return sheet;
  const panes = [...sheet.panes];
  [panes[index], panes[target]] = [panes[target], panes[index]];
  return { ...sheet, panes: panes.map((p, i) => ({ ...p, dual: i > 0 })) };
}

/** 架上的維度（Table Calculation 的定址依它們；Measure Names／Values 不算）。 */
function isDim(use: FieldUse, fields: Map<string, CatalogField>): boolean {
  return !isSpecial(use) && !isMeasure(use, fields);
}

function dims(uses: FieldUse[], fields: Map<string, CatalogField>): FieldUse[] {
  return uses.filter((u) => isDim(u, fields));
}

function innerDims(uses: FieldUse[], fields: Map<string, CatalogField>): FieldUse[] {
  // 與 kernel 的 _inner 相同：最後是離散維度時為它，最後是連續的維度時為結尾連續的維度
  const last = uses[uses.length - 1];
  if (!last || !isDim(last, fields)) return [];
  if (!last.continuous) return [last];
  const inner: FieldUse[] = [];
  for (let i = uses.length - 1; i >= 0; i--) {
    if (!isDim(uses[i], fields) || !uses[i].continuous) break;
    inner.unshift(uses[i]);
  }
  return inner;
}

function uniqueUses(uses: FieldUse[]): FieldUse[] {
  const seen = new Set<string>();
  return uses.filter((u) => {
    const key = useKey(u);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * 依目前的欄列架重新算出 Table Calculation 的定址（與解析時相同的規則，Tableau 的計算方向隨架上的維度而變）；
 * 指定維度（specific）不變。沒有膠囊使用的 Table Calculation 一併移除。
 */
export function refreshTableCalcs(sheet: SheetIR, fields: Map<string, CatalogField>): SheetIR {
  const used = new Set(allUses(sheet).map((u) => u.table_calc).filter(Boolean));
  // 標記卡上的維度（與解析時相同：色彩、大小、形狀等單一位置，再來是標籤、詳細資料、工具提示）
  const markDims = dims(markUses(sheet.marks), fields);
  const cards = [sheet.marks, ...(sheet.panes as PaneIR[]).map((p) => p.marks)];
  const rowDims = dims(sheet.rows, fields);
  const colDims = dims(sheet.columns, fields);
  const innerRows = innerDims(sheet.rows, fields);
  const innerCols = innerDims(sheet.columns, fields);
  const shelves: Record<string, FieldUse[]> = {
    table_across: colDims,
    table_down: rowDims,
    table_across_then_down: [...colDims, ...rowDims],
    table_down_then_across: [...rowDims, ...colDims],
    pane_across: innerCols,
    pane_down: innerRows,
    pane_across_then_down: [...innerCols, ...innerRows],
    pane_down_then_across: [...innerRows, ...innerCols],
    cell: [],
  };
  const calcs = sheet.table_calcs
    .filter((tc) => used.has(tc.id))
    .map((tc) => {
      const shelf = shelves[tc.preset];
      if (!shelf) return tc;
      // 標記卡上的維度是分割；計算方向上沒有維度時才定址它們，圓餅的標記卡上的計算一併定址它們
      // （與 kernel 的 _direction 相同）
      const pie = cards.some((m) => m.type === "pie" && markUses(m).some((u) => u.table_calc === tc.id));
      const addressed = uniqueUses(pie || !shelf.length ? [...shelf, ...markDims] : shelf);
      const exact = tc.preset.startsWith("pane") || tc.preset === "cell";
      return {
        ...tc,
        addressing: [...new Set(addressed.map((u) => u.field))],
        instances: exact ? addressed : [],
      };
    });
  return { ...sheet, table_calcs: calcs };
}

/** 新的 Table Calculation（快速 Table Calculation 或 Table Calculation 計算欄位）。 */
export function newTableCalc(id: string, type: string, options: Record<string, Scalar>): TableCalcIR {
  return {
    id,
    type,
    preset: "table_across",
    addressing: [],
    instances: [],
    level: null,
    restart: null,
    inferred: type === "formula",
    options,
    nested: [],
    source_ref: EDITOR_REF,
  };
}

/** 叢集（分析＞叢集）：叢集欄位與分析的量值（都在檢視中）。 */
export interface ClusterIR {
  field: string;
  measures: FieldUse[];
  count: number | null;
}

/** 預測（分析＞預測）的設定；measures 為欄列架上的量值。 */
export interface ForecastIR {
  measures: FieldUse[];
  indicator: string | null;
  model: "auto" | "auto_no_season" | "custom";
  trend: "none" | "additive" | "multiplicative" | null;
  season: "none" | "additive" | "multiplicative" | null;
  length: "auto" | "next" | "until";
  size: number | null;
  unit: string | null;
  ignore_last: number;
  fill_zeros: boolean;
  confidence: number;
  bands: boolean;
}

/** 欄列架上的量值（預測的對象）。 */
export function axisMeasures(sheet: SheetIR, fields: Map<string, CatalogField>): FieldUse[] {
  return [...sheet.rows, ...sheet.columns].filter((u) => !isSpecial(u) && isMeasure(u, fields));
}

/** 檢視中的量值（叢集的變數），相同的膠囊只列一次。 */
export function viewMeasures(sheet: SheetIR, fields: Map<string, CatalogField>): FieldUse[] {
  const found: FieldUse[] = [];
  for (const use of [...sheet.columns, ...sheet.rows, ...markUses(sheet.marks)]) {
    if (!isSpecial(use) && isMeasure(use, fields) && !found.some((u) => sameUse(u, use))) found.push(use);
  }
  return found;
}

/** 移除不再使用的排序與 Table Calculation（膠囊被移走之後）；叢集與預測跟著檢視調整（叢集欄位不在檢視中
 * 時移除叢集；預測的量值為欄列架上目前的量值，沒有量值時移除預測）。 */
export function tidy(sheet: SheetIR, fields: Map<string, CatalogField>): SheetIR {
  const uses = allUses(sheet);
  const present = new Set(uses.map((u) => u.field));
  const sorts = sheet.sorts.filter((s) => present.has(s.field));
  const changes: Partial<SheetIR> = { sorts };
  // 顯示遺漏的值（欄列架上的膠囊）與不包含在工具提示中的欄位：膠囊移走之後一併拿掉
  const missing = (sheet.show_missing as FieldUse[] | undefined) ?? [];
  if (missing.length) changes.show_missing = missing.filter((u) => [...sheet.rows, ...sheet.columns].some((v) => sameUse(u, v)));
  const excluded = (sheet.tooltip_excluded as FieldUse[] | undefined) ?? [];
  if (excluded.length) changes.tooltip_excluded = excluded.filter((u) => uses.some((v) => sameUse(u, v)));
  const cluster = sheet.cluster as ClusterIR | null | undefined;
  if (cluster) {
    const measures = cluster.measures.filter((u) => uses.some((v) => sameUse(u, v)));
    changes.cluster = present.has(cluster.field) && measures.length ? { ...cluster, measures } : null;
  }
  const forecast = sheet.forecast as ForecastIR | null | undefined;
  if (forecast) {
    // 預測結果都選「無」時預測仍開著（與 Tableau 相同，只是沒有預測的量值）；欄列架上沒有量值時才移除
    const measures = forecastMeasures(sheet, fields);
    changes.forecast = measures.length || axisMeasures(sheet, fields).length ? { ...forecast, measures } : null;
    // 預測移除時，膠囊上選的預測結果一併清除
    if (changes.forecast === null) return refreshTableCalcs(clearForecast({ ...sheet, ...changes }), fields);
  }
  return refreshTableCalcs({ ...sheet, ...changes }, fields);
}

/** 預測的量值：欄列架上的量值（預測結果為「無」的除外），加上標記卡上選了預測結果（趨勢、精度等）的量值。 */
export function forecastMeasures(sheet: SheetIR, fields: Map<string, CatalogField>): FieldUse[] {
  const found = axisMeasures(sheet, fields).filter((u) => u.forecast !== "none");
  for (const use of [sheet.marks, ...sheet.panes.map((p) => p.marks)].flatMap(markUses)) {
    if (use.forecast && use.forecast !== "none" && !found.some((u) => sameUse(u, use))) found.push(use);
  }
  return found;
}

/** 移除預測：膠囊上選的預測結果一併清除（量值回到一般的量值）。 */
export function clearForecast(sheet: SheetIR): SheetIR {
  const plain = (u: FieldUse): FieldUse => (u.forecast ? { ...u, forecast: null } : u);
  const single = (u: FieldUse | null | undefined) => (u ? plain(u) : u);
  const marks = (m: MarksIR): MarksIR => ({
    ...m,
    color: single(m.color) ?? null,
    size: single(m.size) ?? null,
    shape: single(m.shape) ?? null,
    angle: single(m.angle) ?? null,
    path: single(m.path) ?? null,
    label: m.label.map(plain),
    detail: m.detail.map(plain),
    tooltip: m.tooltip.map(plain),
  });
  return {
    ...sheet,
    forecast: null,
    rows: sheet.rows.map(plain),
    columns: sheet.columns.map(plain),
    marks: marks(sheet.marks),
    panes: sheet.panes.map((p) => ({ ...p, measure: plain(p.measure), marks: marks(p.marks) })),
  };
}

// ---------------------------------------------------------------- 架上的變更

/** 標記卡：全部（共用的標記卡，同時套用到各量值的標記卡）或某個量值的標記卡（ws.panes 的索引）。 */
export type Card = "all" | number;

export function sameUse(a: FieldUse | null | undefined, b: FieldUse | null | undefined): boolean {
  if (!a || !b) return a === b;
  return (
    a.field === b.field &&
    a.agg === b.agg &&
    a.continuous === b.continuous &&
    a.table_calc === b.table_calc &&
    (a.forecast ?? null) === (b.forecast ?? null) &&
    (a.date?.kind ?? null) === (b.date?.kind ?? null) &&
    (a.date?.unit ?? null) === (b.date?.unit ?? null)
  );
}

export function cardMarks(sheet: SheetIR, card: Card): MarksIR {
  return card === "all" ? sheet.marks : (sheet.panes[card]?.marks ?? sheet.marks);
}

/** 改變標記卡：全部時同時改共用的與各量值的標記卡。 */
export function updateCard(sheet: SheetIR, card: Card, change: (marks: MarksIR) => MarksIR): SheetIR {
  if (card === "all") {
    return { ...sheet, marks: change(sheet.marks), panes: sheet.panes.map((p) => ({ ...p, marks: change(p.marks) })) };
  }
  return { ...sheet, panes: sheet.panes.map((p, i) => (i === card ? { ...p, marks: change(p.marks) } : p)) };
}

/** 架上的膠囊（欄、列、篩選、Measure Values、標記卡的各個位置）。 */
export function usesOn(sheet: SheetIR, shelf: ShelfName, card: Card = "all"): FieldUse[] {
  if (shelf === "rows") return sheet.rows;
  if (shelf === "columns") return sheet.columns;
  if (shelf === "measure_values") return sheet.measure_values;
  if (shelf === "filters") return sheet.filters.map((f) => f.field).filter((u): u is FieldUse => Boolean(u));
  if (shelf === "pages") return sheet.pages ? [sheet.pages as FieldUse] : [];
  const slot = shelf.slice("marks.".length) as MarkSlot;
  const value = cardMarks(sheet, card)[slot];
  if (Array.isArray(value)) return value as FieldUse[];
  return value ? [value as FieldUse] : [];
}

/** 換掉架上的膠囊（標記卡的單一位置只留最後一個）。欄列架上的量值換掉時，各量值的標記卡跟著換。 */
export function setUses(sheet: SheetIR, shelf: ShelfName, uses: FieldUse[], card: Card = "all"): SheetIR {
  if (shelf === "rows" || shelf === "columns") return { ...sheet, [shelf]: uses };
  if (shelf === "measure_values") return { ...sheet, measure_values: uses };
  if (shelf === "filters") return sheet;
  if (shelf === "pages") {
    // 頁面架只有一個離散的維度；換了欄位就從第一頁開始
    const last = uses[uses.length - 1];
    return { ...sheet, pages: last ? { ...last, agg: null, continuous: false, table_calc: null } : null, page: null };
  }
  const slot = shelf.slice("marks.".length) as MarkSlot;
  const value = LIST_SLOTS.includes(slot) ? uses : (uses[uses.length - 1] ?? null);
  return updateCard(sheet, card, (marks) => ({ ...marks, [slot]: value }));
}

/** 改變架上第 index 個膠囊（彙總、日期、連續或離散、Table Calculation）。 */
export function changeUse(
  sheet: SheetIR,
  shelf: ShelfName,
  index: number,
  next: FieldUse,
  card: Card = "all",
): SheetIR {
  const uses = usesOn(sheet, shelf, card);
  const old = uses[index];
  if (!old) return sheet;
  let updated = setUses(sheet, shelf, uses.map((u, i) => (i === index ? next : u)), card);
  if (shelf === "rows" || shelf === "columns") {
    updated = {
      ...updated,
      panes: updated.panes.map((p) => (sameUse(p.measure, old) && p.axis === shelf ? { ...p, measure: next } : p)),
      sorts: updated.sorts.map((s) => (sameUse(s.by, old) ? { ...s, by: next } : s)),
      subtotals: updated.subtotals.map((u) => (sameUse(u, old) ? next : u)),
    };
  }
  return updated;
}

/** 在架上第 index 個位置插入膠囊（index 省略時放在最後）；標記卡的單一位置（色彩、大小等）換成它。 */
export function insertUse(sheet: SheetIR, shelf: ShelfName, use: FieldUse, index?: number, card: Card = "all"): SheetIR {
  if (shelf === "pages" || (shelf.startsWith("marks.") && !LIST_SLOTS.includes(shelf.slice("marks.".length) as MarkSlot))) {
    return setUses(sheet, shelf, [use], card);
  }
  const uses = [...usesOn(sheet, shelf, card)];
  uses.splice(index ?? uses.length, 0, use);
  return setUses(sheet, shelf, uses, card);
}

export function removeUse(sheet: SheetIR, shelf: ShelfName, index: number, card: Card = "all"): SheetIR {
  if (shelf === "filters") return { ...sheet, filters: sheet.filters.filter((_, i) => i !== index) };
  const uses = usesOn(sheet, shelf, card);
  return setUses(sheet, shelf, uses.filter((_, i) => i !== index), card);
}

/** 量值的標記卡：只留量值還在欄列架上的；Measure Values 不在檢視中時清空它的量值與別名；去掉不再使用的小計。 */
function tidyCards(sheet: SheetIR): SheetIR {
  const panes = sheet.panes.filter((p) => (p.axis === "rows" ? sheet.rows : sheet.columns).some((u) => sameUse(u, p.measure)));
  const shelves = [...sheet.rows, ...sheet.columns];
  const subtotals = sheet.subtotals.filter((u) => shelves.some((s) => sameUse(s, u)));
  const views = [...shelves, ...markUses(sheet.marks), ...panes.flatMap((p) => markUses(p.marks))];
  const valuesUsed = views.some((u) => u.field === MEASURE_VALUES);
  const measureValues = valuesUsed ? sheet.measure_values : [];
  const aliases = ((sheet.measure_aliases as { use: FieldUse }[] | undefined) ?? []).filter((a) =>
    measureValues.some((u) => sameUse(u, a.use)),
  );
  // 色彩上的其他維度：色彩是離散的維度、它們還在詳細資料中才留著
  const colorMore = (marks: MarksIR): MarksIR => {
    const more = marks.color_more ?? [];
    if (more.length === 0) return marks;
    const color = marks.color;
    const kept = color && !color.continuous && color.agg === null ? more.filter((u) => marks.detail.some((d) => sameUse(d, u))) : [];
    return kept.length === more.length ? marks : { ...marks, color_more: kept };
  };
  return {
    ...sheet,
    marks: colorMore(sheet.marks),
    panes: panes.map((p) => ({ ...p, marks: colorMore(p.marks) })),
    subtotals,
    measure_values: measureValues,
    measure_aliases: aliases,
  };
}

/**
 * Measure Values 放進檢視、檢視中卻沒有 Measure Names 時自動加上 Measure Names（Tableau 也會自動加上）：沒有它把
 * 各量值分開，每個量值的標記會畫在同一個位置（文字表的數字疊在同一格）。加在架的最後，位置：
 * - Measure Values 在標記卡上（文字表）：列架上有標題（維度、離散的量值）而欄架上沒有時加到欄架，其餘加到列架
 *   （欄、列架上都有維度時加到列架，依 Tableau 說明文件〈Measure Values and Measure Names〉的例子——文件沒有寫明
 *   各維度在哪個架上；其餘是推定的，見 README「已知限制與待驗證」。與 tmig.render.vegalite 補上 Measure Names
 *   時的規則相同）
 * - Measure Values 在列（欄）架上當座標軸：加到另一架（推定）
 * 地圖不加。
 */
export function ensureMeasureNames(sheet: SheetIR, fields: Map<string, CatalogField>): SheetIR {
  const views = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks), ...sheet.panes.flatMap((p) => markUses(p.marks))];
  if (!views.some((u) => u.field === MEASURE_VALUES) || views.some((u) => u.field === MEASURE_NAMES)) return sheet;
  if (isMapSheet(sheet)) return sheet;
  const header = (u: FieldUse) => !isSpecial(u) && !(isMeasure(u, fields) && u.continuous);
  const axis = (uses: FieldUse[]) => uses.some((u) => u.field === MEASURE_VALUES);
  const toColumns = axis(sheet.rows) || (!axis(sheet.columns) && sheet.rows.some(header) && !sheet.columns.some(header));
  const names: FieldUse = { field: MEASURE_NAMES, agg: null, date: null, continuous: false, table_calc: null };
  return toColumns ? { ...sheet, columns: [...sheet.columns, names] } : { ...sheet, rows: [...sheet.rows, names] };
}

/** 把詳細資料上的維度加到色彩（Tableau 色彩上的多個欄位：以欄位值的組合著色），或拿掉。 */
export function toggleColorMore(sheet: SheetIR, card: Card, use: FieldUse): SheetIR {
  return updateCard(sheet, card, (m) => {
    const more = m.color_more ?? [];
    const present = more.some((u) => sameUse(u, use));
    return { ...m, color_more: present ? more.filter((u) => !sameUse(u, use)) : [...more, use] };
  });
}

/**
 * 空間資料（Shapefile 等空間檔案的 Geometry、MAKEPOINT 等計算欄位）：與 Tableau 相同，放進檢視就畫成地圖——
 * COLLECT 的膠囊成為標記卡上的形狀（不在其他位置），欄列架換成 Latitude／Longitude (generated)。
 * 形狀的位置上不是空間資料的膠囊去掉（Geometry (generated) 除外）。
 */
export function spatialMap(sheet: SheetIR, fields: Map<string, CatalogField>): SheetIR {
  const spatial = (u: FieldUse | null | undefined): u is FieldUse =>
    Boolean(u) && fields.get((u as FieldUse).field)?.datatype === "spatial";
  const marks = sheet.marks;
  const current = marks.geometry ?? null;
  const placed = [...sheet.rows, ...sheet.columns, ...markUses(marks)].find(spatial);
  const invalid = current !== null && current.field !== GEOMETRY_GENERATED && !spatial(current);
  if (!placed && !invalid) return sheet;
  if (!placed) return { ...sheet, marks: { ...marks, geometry: null } };
  const geometry: FieldUse = { ...placed, agg: "collect", date: null, continuous: false, table_calc: null };
  const strip = (uses: FieldUse[]) => uses.filter((u) => !spatial(u));
  const single = (u: FieldUse | null) => (spatial(u) ? null : u);
  const map = isMapSheet(sheet);
  return {
    ...sheet,
    rows: map ? strip(sheet.rows) : [generatedUse(LATITUDE_GENERATED)],
    columns: map ? strip(sheet.columns) : [generatedUse(LONGITUDE_GENERATED)],
    marks: {
      ...marks,
      color: single(marks.color),
      size: single(marks.size),
      shape: single(marks.shape),
      angle: single(marks.angle),
      path: single(marks.path),
      label: strip(marks.label),
      detail: strip(marks.detail),
      tooltip: strip(marks.tooltip),
      geometry,
    },
    panes: map ? sheet.panes : [],
  };
}

/** 每次變更後整理：空間資料、標記卡、排序、Table Calculation 的定址。 */
export function tidySheet(sheet: SheetIR, fields: Map<string, CatalogField>): SheetIR {
  return tidy(tidyCards(spatialMap(sheet, fields)), fields);
}

/** 交換欄與列（Tableau 工具列的「交換」）。 */
export function swapAxes(sheet: SheetIR): SheetIR {
  const flip = (axis: "rows" | "columns") => (axis === "rows" ? "columns" : "rows");
  const axes = ((sheet.axes as { shelf: "rows" | "columns" }[] | undefined) ?? []).map((a) => ({ ...a, shelf: flip(a.shelf) }));
  const totals = sheet.totals.map((t) => flip(t));
  return {
    ...sheet,
    rows: sheet.columns,
    columns: sheet.rows,
    panes: sheet.panes.map((p) => ({ ...p, axis: flip(p.axis) })),
    axes,
    totals,
    row_style: sheet.column_style,
    column_style: sheet.row_style,
  };
}

/** 依量值排序的依據：檢視中的第一個量值（Tableau 工具列排序按鈕的規則）。 */
export function firstMeasure(sheet: SheetIR, fields: Map<string, CatalogField>): FieldUse | null {
  return [...sheet.columns, ...sheet.rows, ...markUses(sheet.marks)].find((u) => !isSpecial(u) && isMeasure(u, fields)) ?? null;
}

/** 設定（或清除）一個維度的排序；同一個欄位只有一個排序。 */
export function setSort(sheet: SheetIR, field: string, sort: Omit<SortIR, "field"> | null): SheetIR {
  const others = sheet.sorts.filter((s) => s.field !== field);
  return { ...sheet, sorts: sort ? [...others, { field, ...sort }] : others };
}

/**
 * 在檢視上拖曳標題或圖例的項目（Tableau 的手動排序：把標題拖到另一個位置）：這個欄位（key 是檢視上的欄位鍵）的
 * 排序改成手動，順序是拖曳之後畫面上的順序；原本手動排過、但現在不在畫面上的成員（被篩選掉的）接在後面。
 */
export function moveMember(sheet: SheetIR, key: string, values: Scalar[]): SheetIR {
  const use = useOfKey(sheet, key);
  if (!use) return sheet;
  const current = sheet.sorts.find((s) => s.field === use.field);
  const saved = current?.kind === "manual" ? current.manual_values : [];
  const rest = saved.filter((v) => !values.some((w) => w === v));
  return setSort(sheet, use.field, { kind: "manual", order: "asc", by: null, nested: false, manual_values: [...values, ...rest] });
}

export const isNumeric = (field: CatalogField | undefined) =>
  field !== undefined && (field.datatype === "integer" || field.datatype === "real");

/** Tableau 公式中的欄位參照（[Sales]）。 */
export function formulaRef(field: CatalogField): string {
  return field.name.startsWith("[") ? field.name : `[${field.name}]`;
}

/** 公式中的欄位參照換成欄位顯示的名稱（給人看的寫法；存檔的公式用內部名稱）：計算欄位的內部名稱是
 * [Calculation_1368…]，顯示成 [利潤率]。 */
export function formulaCaptions(formula: string, fields: Map<string, CatalogField>): string {
  let text = formula;
  for (const field of fields.values()) {
    const ref = formulaRef(field);
    const shown = `[${field.caption.replace(/]/g, "]]")}]`;
    if (ref !== shown && text.includes(ref)) text = text.split(ref).join(shown);
  }
  return text;
}

/** 量值的 Tableau 公式（Top N 篩選的依據）：SUM([Sales])；彙總的計算欄位直接參照。 */
export function measureFormula(use: FieldUse, fields: Map<string, CatalogField>): string | null {
  const field = fields.get(use.field);
  if (!field) return null;
  if (use.agg) {
    // 百分位數：PERCENTILE([Sales], 0.9)
    const percentile = /^pct(\d+)$/.exec(use.agg);
    if (percentile) return `PERCENTILE(${formulaRef(field)}, ${Number(percentile[1]) / 100})`;
    return `${(AGG_FUNCTIONS[use.agg] ?? use.agg.toUpperCase())}(${formulaRef(field)})`;
  }
  if (field.level === "aggregate") return formulaRef(field);
  return null;
}

const AGG_FUNCTIONS: Record<string, string> = {
  sum: "SUM",
  avg: "AVG",
  median: "MEDIAN",
  count: "COUNT",
  countd: "COUNTD",
  min: "MIN",
  max: "MAX",
  stdev: "STDEV",
  stdevp: "STDEVP",
  var: "VAR",
  varp: "VARP",
  attr: "ATTR",
};

// ---------------------------------------------------------------- 階層的切入

const DATE_LEVELS = ["year", "quarter", "month", "day"];
const TIME_LEVELS = ["hour", "minute", "second"];

const plainUse = (field: string): FieldUse => ({ field, agg: null, date: null, continuous: false, table_calc: null });

/**
 * 膠囊所在的階層（由上到下的各層）與它的位置（與 tmig.planner.drill 相同）：工作表資料源的階層，或日期的
 * 年 → 季 → 月 → 日（日期時間再到時、分、秒）。彙總、Table Calculation 與週、星期等不在階層中。
 */
export function hierarchyOf(
  use: FieldUse,
  catalog: Catalog,
  datasource: string,
  fields: Map<string, CatalogField>,
): { levels: FieldUse[]; position: number } | null {
  if (use.agg !== null || use.table_calc !== null || isSpecial(use)) return null;
  if (use.date) {
    const { kind } = use.date;
    const units = [...DATE_LEVELS, ...(fields.get(use.field)?.datatype === "datetime" ? TIME_LEVELS : [])];
    const position = units.indexOf(use.date.unit);
    if (position < 0) return null;
    return { levels: units.map((unit) => ({ ...use, date: { kind, unit } })), position };
  }
  const ds = catalog.datasources.find((d) => d.id === datasource);
  for (const hierarchy of ds?.hierarchies ?? []) {
    const position = hierarchy.fields.indexOf(use.field);
    if (position >= 0) return { levels: hierarchy.fields.map(plainUse), position };
  }
  return null;
}

/** 欄、列架上膠囊的切入按鈕（Tableau 的 ⊞／⊟）：下一層緊接在後面時可以收合，否則可以向下切入。 */
export interface DrillToggle {
  kind: "down" | "up";
  /** 向下切入時加入的下一層；收合時移除的第一層 */
  next: FieldUse;
}

const sameLevel = (a: FieldUse | undefined, b: FieldUse | undefined) => a !== undefined && b !== undefined && useKey(a) === useKey(b);

export function drillToggle(
  sheet: SheetIR,
  shelf: "rows" | "columns",
  index: number,
  catalog: Catalog,
  fields: Map<string, CatalogField>,
): DrillToggle | null {
  const uses = usesOn(sheet, shelf);
  const use = uses[index];
  const found = use ? hierarchyOf(use, catalog, sheet.datasource, fields) : null;
  if (!use || !found) return null;
  const next = found.levels[found.position + 1];
  if (!next) return null;
  // 日期值（截斷）只有一個膠囊：向下切入改成下一層的截斷
  if (use.date?.kind === "trunc") return { kind: "down", next };
  if (sameLevel(uses[index + 1], next)) return { kind: "up", next };
  // 下一層已經在欄、列架的其他位置：不重複加入
  if ([...sheet.rows, ...sheet.columns].some((u) => sameLevel(u, next))) return null;
  return { kind: "down", next };
}

/** 按下膠囊的切入按鈕：向下切入時在後面加入下一層（日期值改成下一層）；收合時移除後面緊接著的各個下層。 */
export function drillPill(
  sheet: SheetIR,
  shelf: "rows" | "columns",
  index: number,
  catalog: Catalog,
  fields: Map<string, CatalogField>,
): SheetIR {
  const toggle = drillToggle(sheet, shelf, index, catalog, fields);
  const uses = usesOn(sheet, shelf);
  const use = uses[index];
  const found = use ? hierarchyOf(use, catalog, sheet.datasource, fields) : null;
  if (!toggle || !use || !found) return sheet;
  if (toggle.kind === "down") {
    if (use.date?.kind === "trunc") return changeUse(sheet, shelf, index, toggle.next);
    return insertUse(sheet, shelf, toggle.next, index + 1);
  }
  let end = index + 1;
  while (end < uses.length && sameLevel(uses[end], found.levels[found.position + 1 + (end - index - 1)])) end += 1;
  return setUses(sheet, shelf, [...uses.slice(0, index + 1), ...uses.slice(end)]);
}

/** 檢視上的標記的欄位鍵（例如 f_region、year(f_order_date)）→ 工作表上的那個維度。 */
export function useOfKey(sheet: SheetIR, key: string): FieldUse | null {
  const uses = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks), ...sheet.panes.flatMap((p) => markUses(p.marks))];
  return uses.find((u) => u.agg === null && u.table_calc === null && useKey(u) === key) ?? null;
}

/** 選取的標記的維度（依第一個標記的欄位鍵）：每個鍵的欄位使用與各標記的值。 */
export function selectionLevels(sheet: SheetIR, marks: Mark[]): { key: string; use: FieldUse }[] | null {
  if (!marks.length) return null;
  const keys = Object.keys(marks[0]);
  const levels = keys.map((key) => ({ key, use: useOfKey(sheet, key) }));
  if (!levels.length || levels.some((l) => l.use === null)) return null;
  return levels as { key: string; use: FieldUse }[];
}

/**
 * 選取的標記「只保留」或「排除」（Tableau 工具提示的 Keep Only／Exclude）：只有一個維度時是這個維度的篩選
 * （取代同一個維度原有的一般篩選），多個維度時是依值組合篩選（tuple）。
 */
export function selectionFilter(sheet: SheetIR, marks: Mark[], mode: "include" | "exclude", id: string): SheetIR | null {
  const levels = selectionLevels(sheet, marks);
  if (!levels) return null;
  if (levels.length === 1) {
    const [{ key, use }] = levels;
    const values: Scalar[] = [];
    for (const mark of marks) if (!values.some((v) => v === mark[key])) values.push(mark[key] ?? null);
    const field: FieldUse = { ...use, continuous: false };
    const filter: FilterIR = {
      kind: "categorical",
      id,
      field,
      stage: "dimension",
      mode,
      values,
      ranges: [],
      group: null,
      source_ref: EDITOR_REF,
    };
    const existing = sheet.filters.findIndex((f) => f.kind === "categorical" && f.stage === "dimension" && f.field && useKey(f.field) === key);
    const filters = existing >= 0 ? sheet.filters.map((f, i) => (i === existing ? filter : f)) : [...sheet.filters, filter];
    return { ...sheet, filters };
  }
  const tuples = marks.map((m) => levels.map((l) => m[l.key] ?? null));
  const filter = {
    kind: "tuple",
    id,
    fields: levels.map((l) => ({ ...l.use, continuous: false })),
    stage: "dimension",
    mode,
    tuples,
    action: null,
    source_fields: null,
    group: null,
    tooltip: false,
    source_ref: EDITOR_REF,
  } as unknown as FilterIR;
  return { ...sheet, filters: [...sheet.filters, filter] };
}

/**
 * 「N 個 Null」指示器的「篩選資料」要篩選的欄位：欄、列架上連續的欄位（座標軸）中，畫圖的查詢結果裡有 Null 值的
 * （與指示器的個數相同的算法：Measure Values、地圖的經緯度不算）。table 是 render 回應的 table（欄位鍵與未格式化
 * 的值，Null 是空字串）；回應裡找不到這個欄位時當成有 Null（指示器出現就表示某個座標軸有 Null）。
 */
export function nullAxisUses(sheet: SheetIR, table: { keys: string[]; raw: string[][] } | undefined): FieldUse[] {
  const axes: FieldUse[] = [];
  for (const use of [...sheet.columns, ...sheet.rows])
    if (use.continuous && !isSpecial(use) && !axes.some((u) => useKey(u) === useKey(use))) axes.push(use);
  const withNulls = axes.filter((use) => {
    const at = table?.keys.indexOf(useKey(use)) ?? -1;
    return at < 0 || (table?.raw ?? []).some((row) => row[at] === "" || row[at] === null || row[at] === undefined);
  });
  return withNulls.length > 0 ? withNulls : axes;
}

/**
 * 「篩選資料」（Tableau 指示器的 Filter Data）：每個有 Null 的座標軸欄位加一個只留非 Null 值的篩選（篩選對話框
 * 「特殊值」的「非 Null 值」：沒有上下限、不含 Null）。量值是量值篩選（表計算是表計算的篩選），連續的日期與維度
 * 篩選欄位本身的值（不分日期的層級）。篩選架上已經有同一個欄位的範圍篩選時改成不含 Null，上下限不變。
 * ids：新篩選的代號，與 uses 一一對應。
 */
export function excludeNulls(sheet: SheetIR, uses: FieldUse[], ids: string[], fields: Map<string, CatalogField>): SheetIR {
  let filters = sheet.filters;
  uses.forEach((use, i) => {
    const measure = isMeasure(use, fields);
    const field: FieldUse = measure ? use : { ...use, date: null, continuous: true };
    const existing = filters.findIndex((f) => f.kind === "range" && f.field && useKey(f.field) === useKey(field));
    if (existing >= 0) {
      filters = filters.map((f, j) => (j === existing ? { ...f, include_null: false, only_null: false } : f));
      return;
    }
    const table = use.table_calc !== null || fields.get(use.field)?.level === "table_calc";
    filters = [
      ...filters,
      {
        kind: "range",
        id: ids[i],
        field,
        stage: measure ? (table ? "table_calc" : "measure") : "dimension",
        min: null,
        max: null,
        include_null: false,
        only_null: false,
        group: null,
        source_ref: EDITOR_REF,
      },
    ];
  });
  return { ...sheet, filters };
}
