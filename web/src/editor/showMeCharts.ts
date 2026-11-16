// 智慧顯示（Tableau 的 Show Me）：依檢視中的欄位（維度、日期、量值、地理欄位）列出 24 種圖表，
// 不能用的列出需要的欄位；點選後與 Tableau 相同重新排列欄位（篩選、頁面與排序不變）。

import type { FieldUse } from "../types";
import {
  buildMap,
  type CatalogField,
  EDITOR_REF,
  type GeoRole,
  isDate,
  isMeasure,
  isSpecial,
  type MarksIR,
  markUses,
  MEASURE_NAMES,
  MEASURE_VALUES,
  type PaneIR,
  setAggregated,
  type SheetIR,
  useKey,
} from "./model";

/** 檢視中的欄位：一般的維度（含地理欄位）、日期、量值與有地名角色的維度。 */
export interface Parts {
  dims: FieldUse[];
  dates: FieldUse[];
  measures: FieldUse[];
  geo: { use: FieldUse; role: GeoRole }[];
}

/** 建立圖表時需要的資料源操作（直方圖要建立數值分組）。 */
export interface ShowMeContext {
  fields: Map<string, CatalogField>;
  /** 量值的數值分組欄位：已有時回傳它，沒有時建立 */
  bins: (measure: FieldUse) => Promise<string>;
}

export interface Chart {
  id: string;
  label: string;
  icon: string;
  /** 需要的欄位（與 Tableau 的提示相同） */
  needs: string;
  ok: (p: Parts) => boolean;
  build: (sheet: SheetIR, p: Parts, ctx: ShowMeContext) => SheetIR | Promise<SheetIR>;
}

const PLAIN: Pick<FieldUse, "agg" | "date" | "table_calc"> = { agg: null, date: null, table_calc: null };
const NAMES: FieldUse = { field: MEASURE_NAMES, ...PLAIN, continuous: false };
const VALUES: FieldUse = { field: MEASURE_VALUES, ...PLAIN, continuous: true };

/** 檢視中的欄位（欄列架、標記卡與 Measure Values；相同的欄位只算一次）。 */
export function viewParts(sheet: SheetIR, fields: Map<string, CatalogField>): Parts {
  const panes = (sheet.panes as PaneIR[]).flatMap((p) => [p.measure, ...markUses(p.marks)]);
  const uses = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks), ...panes, ...sheet.measure_values];
  const parts: Parts = { dims: [], dates: [], measures: [], geo: [] };
  const seen = new Set<string>();
  for (const use of uses) {
    if (isSpecial(use)) continue;
    const field = fields.get(use.field);
    if (!field || field.datatype === "spatial") continue;
    const measure = isMeasure(use, fields);
    const key = measure ? useKey(use) : use.field;
    if (seen.has(key)) continue;
    seen.add(key);
    if (measure) parts.measures.push(use);
    else if (isDate(field)) parts.dates.push(use);
    else {
      parts.dims.push(use);
      const role = field.geo_role;
      if (role && role !== "latitude" && role !== "longitude") parts.geo.push({ use, role });
    }
  }
  return parts;
}

const discrete = (u: FieldUse): FieldUse => ({ ...u, agg: null, table_calc: null, continuous: false });
const dateDiscrete = (u: FieldUse): FieldUse => ({
  ...discrete(u),
  date: u.date?.kind === "part" ? u.date : { kind: "part", unit: u.date?.unit ?? "year" },
});
const dateContinuous = (u: FieldUse): FieldUse => {
  const unit = u.date?.unit === "weekday" || !u.date ? (u.date ? "day" : "month") : u.date.unit;
  return { ...u, agg: null, table_calc: null, continuous: true, date: { kind: "trunc", unit } };
};
const measure = (u: FieldUse): FieldUse => ({ ...u, continuous: true });

function emptyMarks(marks: MarksIR, type: string): MarksIR {
  return {
    ...marks,
    type,
    mark_size: null,
    color: null,
    size: null,
    label: [],
    detail: [],
    tooltip: [],
    shape: null,
    angle: null,
    path: null,
    geometry: null,
  };
}

/** 清空欄列架與標記卡（篩選、頁面、排序與格式不變）。 */
function cleared(sheet: SheetIR, type: string): SheetIR {
  const base: SheetIR = {
    ...sheet,
    rows: [],
    columns: [],
    panes: [],
    measure_values: [],
    marks: emptyMarks(sheet.marks, type),
    reference_lines: [],
  };
  return sheet.disaggregated ? { ...base, disaggregated: false } : base;
}

/** 維度（含日期，離散）：最後一個放到欄，其餘放到列（只有一個時放到列）。 */
function splitDims(p: Parts): { rows: FieldUse[]; columns: FieldUse[] } {
  const all = [...p.dims.map(discrete), ...p.dates.map(dateDiscrete)];
  if (all.length < 2) return { rows: all, columns: [] };
  return { rows: all.slice(0, -1), columns: all.slice(-1) };
}

const allDims = (p: Parts) => [...p.dims.map(discrete), ...p.dates.map(dateDiscrete)];
const nDims = (p: Parts) => p.dims.length + p.dates.length;

function withMarks(sheet: SheetIR, change: Partial<MarksIR>): SheetIR {
  return { ...sheet, marks: { ...sheet.marks, ...change } };
}

/** 量值放到欄或列；多個量值時與 Tableau 相同各自一個座標軸。 */
function onAxis(sheet: SheetIR, shelf: "rows" | "columns", measures: FieldUse[]): SheetIR {
  return { ...sheet, [shelf]: [...(sheet[shelf] as FieldUse[]), ...measures.map(measure)] };
}

function line(axis: FieldUse, value: FieldUse, extra: Record<string, unknown>): Record<string, unknown> {
  return {
    id: `showme${Math.random().toString(36).slice(2, 9)}`,
    axis,
    value,
    constant: null,
    percentiles: [],
    factors: [],
    scope: "cell",
    label: "none",
    label_text: null,
    source_ref: EDITOR_REF,
    ...extra,
  };
}

/** 依日期的圖：日期在欄，量值在列，第一個維度在色彩，其餘在詳細資料。 */
function overTime(sheet: SheetIR, p: Parts, type: string, continuous: boolean): SheetIR {
  const date = continuous ? dateContinuous(p.dates[0]) : dateDiscrete(p.dates[0]);
  const base = cleared(sheet, type);
  const dims = [...p.dims.map(discrete), ...p.dates.slice(1).map(dateDiscrete)];
  return withMarks(onAxis({ ...base, columns: [date] }, "rows", p.measures), {
    color: dims[0] ?? null,
    detail: dims.slice(1),
  });
}

/** 兩個量值共用一個座標軸位置（雙軸）：第二個量值有自己的標記卡並重疊。 */
function dualAxis(sheet: SheetIR, first: string, second: string): SheetIR {
  const [m0, m1] = sheet.rows;
  const color = sheet.marks.color ?? NAMES;
  const marks0 = { ...sheet.marks, type: first, color };
  const marks1 = { ...sheet.marks, type: second, color };
  return {
    ...sheet,
    marks: { ...sheet.marks, color },
    panes: [
      { measure: m0, axis: "rows", dual: false, marks: marks0 },
      { measure: m1, axis: "rows", dual: true, marks: marks1 },
    ],
  };
}

export const CHARTS: Chart[] = [
  {
    id: "text",
    label: "文字表",
    icon: "▤",
    needs: "1 個以上的維度、1 個以上的量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1,
    build: (sheet, p) => {
      const { rows, columns } = splitDims(p);
      const base = { ...cleared(sheet, "text"), rows, columns };
      if (p.measures.length === 1) return withMarks(base, { label: [measure(p.measures[0])] });
      return { ...withMarks(base, { label: [VALUES] }), columns: [...columns, NAMES], measure_values: p.measures.map(measure) };
    },
  },
  {
    id: "heatmap",
    label: "熱度圖",
    icon: "▦",
    needs: "1 個以上的維度、1 到 2 個量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const { rows, columns } = splitDims(p);
      const [m0, m1] = p.measures.map(measure);
      return withMarks({ ...cleared(sheet, "square"), rows, columns }, { color: m1 ?? m0, size: m0 });
    },
  },
  {
    id: "highlight",
    label: "醒目提示表",
    icon: "▩",
    needs: "1 個以上的維度、1 個量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length === 1,
    build: (sheet, p) => {
      const { rows, columns } = splitDims(p);
      const m = measure(p.measures[0]);
      return withMarks({ ...cleared(sheet, "square"), rows, columns }, { color: m, label: [m] });
    },
  },
  {
    id: "symbol_map",
    label: "符號地圖",
    icon: "◉",
    needs: "1 個地理維度、0 到 2 個量值",
    ok: (p) => p.geo.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const [m0, m1] = p.measures.map(measure);
      const mapped = buildMap(cleared(sheet, "circle"), p.geo[0].use, p.geo[0].role);
      const others = p.dims.filter((d) => d.field !== p.geo[0].use.field).map(discrete);
      return withMarks(mapped, { geometry: null, size: m0 ?? null, color: m1 ?? null, detail: [...mapped.marks.detail, ...others] });
    },
  },
  {
    id: "filled_map",
    label: "填色地圖",
    icon: "▰",
    needs: "1 個地理維度（國家、州、郡或郵遞區號）、0 到 1 個量值",
    ok: (p) => p.geo.some((g) => ["country", "state", "county", "zip"].includes(g.role)) && p.measures.length <= 1,
    build: (sheet, p) => {
      const geo = p.geo.find((g) => ["country", "state", "county", "zip"].includes(g.role)) ?? p.geo[0];
      const mapped = buildMap(cleared(sheet, "automatic"), geo.use, geo.role);
      return withMarks(mapped, { color: p.measures[0] ? measure(p.measures[0]) : null });
    },
  },
  {
    id: "pie",
    label: "圓餅圖",
    icon: "◔",
    needs: "1 個以上的維度、1 到 2 個量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const dims = allDims(p);
      const [m0, m1] = p.measures.map(measure);
      return withMarks({ ...cleared(sheet, "pie"), rows: dims.slice(1) }, { color: dims[0], angle: m0, size: m1 ?? null });
    },
  },
  {
    id: "hbar",
    label: "水平長條",
    icon: "☰",
    needs: "0 個以上的維度、1 個以上的量值",
    ok: (p) => p.measures.length >= 1,
    build: (sheet, p) => onAxis({ ...cleared(sheet, "bar"), rows: allDims(p) }, "columns", p.measures),
  },
  {
    id: "stacked",
    label: "堆疊長條",
    icon: "▥",
    needs: "1 個以上的維度、1 個以上的量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1,
    build: (sheet, p) => {
      const dims = allDims(p);
      const base = onAxis({ ...cleared(sheet, "bar"), rows: dims.length > 1 ? dims.slice(0, -1) : [] }, "columns", p.measures);
      return withMarks(base, { color: dims[dims.length - 1] });
    },
  },
  {
    id: "side_bars",
    label: "並排長條",
    icon: "⫴",
    needs: "1 個以上的維度、1 個以上的量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1,
    build: (sheet, p) => {
      const dims = allDims(p);
      return withMarks(onAxis({ ...cleared(sheet, "bar"), columns: dims }, "rows", p.measures), { color: dims[dims.length - 1] });
    },
  },
  {
    id: "treemap",
    label: "矩形式樹狀結構圖",
    icon: "▧",
    needs: "1 個以上的維度、1 到 2 個量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const [m0, m1] = p.measures.map(measure);
      return withMarks(cleared(sheet, "square"), { size: m0, color: m1 ?? m0, label: [...allDims(p), m0] });
    },
  },
  {
    id: "circles",
    label: "圓形檢視",
    icon: "⁘",
    needs: "1 個以上的維度、1 個以上的量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1,
    build: (sheet, p) => {
      const dims = allDims(p);
      const base = onAxis({ ...cleared(sheet, "circle"), rows: dims.slice(0, 1) }, "columns", p.measures);
      return withMarks(base, { color: dims[1] ?? null, detail: dims.slice(2) });
    },
  },
  {
    id: "side_circles",
    label: "並排圓形",
    icon: "⁙",
    needs: "1 個以上的維度、1 個以上的量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1,
    build: (sheet, p) => {
      const dims = allDims(p);
      return withMarks(onAxis({ ...cleared(sheet, "circle"), columns: dims }, "rows", p.measures), { color: dims[dims.length - 1] });
    },
  },
  {
    id: "lines",
    label: "線（連續）",
    icon: "⟋",
    needs: "1 個日期、0 個以上的維度、1 個以上的量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length >= 1,
    build: (sheet, p) => overTime(sheet, p, "line", true),
  },
  {
    id: "lines_discrete",
    label: "線（離散）",
    icon: "⌇",
    needs: "1 個日期、0 個以上的維度、1 個以上的量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length >= 1,
    build: (sheet, p) => overTime(sheet, p, "line", false),
  },
  {
    id: "dual_lines",
    label: "雙線",
    icon: "⫽",
    needs: "1 個日期、0 個以上的維度、2 個量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length === 2,
    build: (sheet, p) => dualAxis(overTime(sheet, p, "line", true), "line", "line"),
  },
  {
    id: "area",
    label: "區域圖（連續）",
    icon: "◭",
    needs: "1 個日期、0 個以上的維度、1 個以上的量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length >= 1,
    build: (sheet, p) => overTime(sheet, p, "area", true),
  },
  {
    id: "area_discrete",
    label: "區域圖（離散）",
    icon: "◮",
    needs: "1 個日期、0 個以上的維度、1 個以上的量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length >= 1,
    build: (sheet, p) => overTime(sheet, p, "area", false),
  },
  {
    id: "dual_combo",
    label: "雙重組合",
    icon: "⫾",
    needs: "1 個日期、0 個以上的維度、2 個量值",
    ok: (p) => p.dates.length >= 1 && p.measures.length === 2,
    build: (sheet, p) => dualAxis(overTime(sheet, p, "bar", true), "bar", "line"),
  },
  {
    id: "scatter",
    label: "散佈圖",
    icon: "⁖",
    needs: "0 個以上的維度、2 到 4 個量值",
    ok: (p) => p.measures.length >= 2 && p.measures.length <= 4,
    build: (sheet, p) => {
      const [m0, m1, m2, m3] = p.measures.map(measure);
      const dims = allDims(p);
      const base = { ...cleared(sheet, "circle"), columns: [m0], rows: [m1] };
      return withMarks(base, { color: m2 ?? dims[0] ?? null, size: m3 ?? null, detail: m2 ? dims : dims.slice(1) });
    },
  },
  {
    id: "histogram",
    label: "直方圖",
    icon: "▙",
    needs: "1 個量值（沒有維度）",
    ok: (p) => p.measures.length === 1 && nDims(p) === 0 && p.measures[0].table_calc === null,
    build: async (sheet, p, ctx) => {
      const m = p.measures[0];
      const bins = await ctx.bins(m);
      const count: FieldUse = { ...m, agg: "count", continuous: true, table_calc: null };
      return { ...cleared(sheet, "bar"), columns: [{ field: bins, ...PLAIN, continuous: false }], rows: [count] };
    },
  },
  {
    id: "boxplot",
    label: "盒鬚圖",
    icon: "⧮",
    needs: "0 個以上的維度、1 個以上的量值",
    ok: (p) => p.measures.length >= 1,
    build: (sheet, p, ctx) => {
      const dims = allDims(p);
      const base = onAxis({ ...cleared(sheet, "circle"), columns: dims.length > 1 ? dims.slice(0, -1) : [] }, "rows", p.measures);
      const marks = withMarks(base, { detail: dims.slice(-1) });
      const boxes = marks.rows.map((axis) => line(axis, axis, { kind: "boxplot", formula: "quantiles", scope: "pane", whiskers: "standard" }));
      const sheetWithBoxes = { ...marks, reference_lines: boxes };
      // 沒有維度時每一列資料一個標記（與 Tableau 相同關閉彙總量值）
      return dims.length === 0 ? setAggregated(sheetWithBoxes, false, ctx.fields) : sheetWithBoxes;
    },
  },
  {
    id: "gantt",
    label: "甘特圖",
    icon: "≡",
    needs: "1 個日期、1 個以上的維度、0 到 2 個量值",
    ok: (p) => p.dates.length >= 1 && p.dims.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const [m0, m1] = p.measures.map(measure);
      const day: FieldUse = { ...dateContinuous(p.dates[0]), date: { kind: "trunc", unit: "day" } };
      return withMarks({ ...cleared(sheet, "gantt"), columns: [day], rows: p.dims.map(discrete) }, { size: m0 ?? null, color: m1 ?? null });
    },
  },
  {
    id: "bullet",
    label: "子彈圖",
    icon: "⊢",
    needs: "0 個以上的維度、2 個量值",
    ok: (p) => p.measures.length === 2,
    build: (sheet, p) => {
      const [m0, m1] = p.measures.map(measure);
      // 長條較細（大小 0.5），分布的底色露在長條上下（與 Tableau 的子彈圖相同）
      const base = withMarks({ ...cleared(sheet, "bar"), rows: allDims(p), columns: [m0] }, { detail: [m1], mark_size: 0.5 });
      // 第二個量值：平均的 60%、80% 為分布、平均為參考線（與 Tableau 的子彈圖相同，每一格各自計算）
      const band = line(m0, m1, { kind: "distribution", formula: "average", percentages: [60, 80] });
      const target = line(m0, m1, { kind: "line", formula: "average" });
      return { ...base, reference_lines: [band, target] };
    },
  },
  {
    id: "bubbles",
    label: "填滿的泡泡圖",
    icon: "❂",
    needs: "1 個以上的維度、1 到 2 個量值",
    ok: (p) => nDims(p) >= 1 && p.measures.length >= 1 && p.measures.length <= 2,
    build: (sheet, p) => {
      const dims = allDims(p);
      const [m0, m1] = p.measures.map(measure);
      return withMarks(cleared(sheet, "circle"), { size: m0, color: m1 ?? dims[0], label: [dims[0]], detail: dims.slice(1) });
    },
  },
];

/** Tableau 建議的圖表（智慧顯示中外框醒目的那一個）。 */
export function recommended(p: Parts): string | null {
  const usable = CHARTS.filter((c) => c.ok(p)).map((c) => c.id);
  const pick = (id: string) => (usable.includes(id) ? id : null);
  if (p.geo.length && p.measures.length <= 2) return pick("symbol_map");
  if (p.dates.length && p.measures.length) return pick("lines");
  if (!nDims(p) && p.measures.length >= 2) return pick("scatter");
  if (!nDims(p) && p.measures.length === 1) return pick("histogram");
  if (nDims(p) && p.measures.length) return pick(nDims(p) > 1 ? "stacked" : "hbar");
  return usable[0] ?? null;
}
