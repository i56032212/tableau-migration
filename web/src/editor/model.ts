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
  source_ref: string;
}

export interface SortIR {
  field: string;
  kind: "by_field" | "alphabetic" | "manual" | "data_source";
  order: "asc" | "desc";
  by: FieldUse | null;
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
  return text;
}

export function useKey(use: FieldUse): string {
  let key = use.field;
  if (use.date) key = `${use.date.kind === "part" ? use.date.unit : `trunc_${use.date.unit}`}(${key})`;
  if (use.agg) key = `${use.agg}(${key})`;
  if (use.table_calc) key = `${key}#${use.table_calc}`;
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
  const cluster = sheet.cluster as ClusterIR | null | undefined;
  if (cluster) {
    const measures = cluster.measures.filter((u) => uses.some((v) => sameUse(u, v)));
    changes.cluster = present.has(cluster.field) && measures.length ? { ...cluster, measures } : null;
  }
  const forecast = sheet.forecast as ForecastIR | null | undefined;
  if (forecast) {
    const measures = axisMeasures(sheet, fields);
    changes.forecast = measures.length ? { ...forecast, measures } : null;
  }
  return refreshTableCalcs({ ...sheet, ...changes }, fields);
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
  return { ...sheet, panes, subtotals, measure_values: measureValues, measure_aliases: aliases };
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

export const isNumeric = (field: CatalogField | undefined) =>
  field !== undefined && (field.datatype === "integer" || field.datatype === "real");

/** Tableau 公式中的欄位參照（[Sales]）。 */
export function formulaRef(field: CatalogField): string {
  return field.name.startsWith("[") ? field.name : `[${field.name}]`;
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
