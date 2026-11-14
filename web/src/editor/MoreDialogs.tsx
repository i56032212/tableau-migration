// 排序、Table Calculation 與註解的對話框（Tableau 的「排序」「表計算」「註解」）。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi } from "./api";
import { useEditor } from "./context";
import { type RichRun, RichTextEditor, type RichToken, plainText } from "./RichText";
import {
  AGGREGATIONS,
  allUses,
  type Card,
  cardMarks,
  type Catalog,
  type CatalogField,
  DIRECTIONS,
  isMeasure,
  isSpecial,
  markUses,
  pillLabel,
  sameUse,
  setSort,
  type SheetIR,
  type SortIR,
  type TableCalcIR,
  updateCard,
  useKey,
} from "./model";

/** 格式文字中可以插入的欄位與參數：檢視中的欄位（欄列架、標記卡，不重複）與參數。 */
export function textTokens(
  sheet: SheetIR,
  fields: Map<string, CatalogField>,
  catalog: Catalog,
  options: { tableCalcs?: boolean; uses?: boolean } = {},
): RichToken[] {
  const tokens: RichToken[] = [];
  if (options.uses !== false) {
    const seen = new Set<string>();
    for (const use of allUses(sheet)) {
      if (isSpecial(use) || seen.has(useKey(use))) continue;
      if (use.table_calc !== null && !options.tableCalcs) continue;
      if (fields.get(use.field)?.level === "table_calc" && !options.tableCalcs) continue;
      seen.add(useKey(use));
      tokens.push({ label: pillLabel(use, fields, sheet.table_calcs), run: { use } });
    }
  }
  for (const parameter of catalog.parameters) tokens.push({ label: `參數.${parameter.caption}`, run: { parameter: parameter.id } });
  return tokens;
}

function display(value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "boolean") return value ? "真" : "假";
  return String(value);
}

// ---------------------------------------------------------------- 排序

type SortKind = SortIR["kind"];

const SORT_KINDS: [SortKind, string][] = [
  ["data_source", "資料來源順序"],
  ["alphabetic", "字母"],
  ["by_field", "欄位"],
  ["manual", "手動"],
];

/** 排序對話框：資料來源順序、字母、依欄位（欄位與彙總）或手動排列成員（Tableau 的「排序」）。 */
export function SortDialog({ use, onClose }: { use: FieldUse; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields, workbook } = editor;
  const current = sheet.sorts.find((s) => s.field === use.field) ?? null;
  const field = fields.get(use.field);
  const [kind, setKind] = useState<SortKind>(current?.kind ?? "alphabetic");
  const [order, setOrder] = useState<"asc" | "desc">(current?.order ?? "asc");
  // 依欄位：資料窗格中可以彙總的欄位（彙總的計算欄位直接使用）
  const candidates = useMemo(
    () => [...fields.values()].filter((f) => !f.hidden && f.level !== "table_calc" && f.datatype !== "spatial"),
    [fields],
  );
  const initialBy = current?.by ?? null;
  const [byField, setByField] = useState<string>(initialBy?.field ?? candidates.find((f) => f.role === "measure")?.id ?? candidates[0]?.id ?? "");
  const [byAgg, setByAgg] = useState<string>(initialBy?.agg ?? "sum");
  // 巢狀排序：這個維度在欄或列架上、前面還有離散的維度時才有意義
  const [nested, setNested] = useState(current?.nested === true);
  const hasOuter = [sheet.rows, sheet.columns].some((shelf) => {
    const discrete = shelf.filter((u) => !u.continuous);
    return discrete.findIndex((u) => u.field === use.field) > 0;
  });
  const target = fields.get(byField);
  const numeric = target?.datatype === "integer" || target?.datatype === "real";
  const aggs = numeric ? AGGREGATIONS : AGGREGATIONS.filter(([a]) => ["count", "countd", "min", "max"].includes(a));
  // 手動：成員的順序
  const [members, setMembers] = useState<Scalar[] | null>(null);
  const [picked, setPicked] = useState(0);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (kind !== "manual" || members !== null) return;
    let cancelled = false;
    editApi
      .domain(workbook, { ...use, table_calc: null }, { kind: "values" })
      .then((d) => {
        if (cancelled || d.kind !== "values") return;
        // 已經手動排過的成員在前（依原本的順序），其餘依資料的順序
        const saved = current?.kind === "manual" ? current.manual_values : [];
        const keys = new Set(saved.map((v) => JSON.stringify(v)));
        const present = new Set(d.values.map((v) => JSON.stringify(v)));
        setMembers([...saved.filter((v) => present.has(JSON.stringify(v))), ...d.values.filter((v) => !keys.has(JSON.stringify(v)))]);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, workbook]);
  const move = (delta: number) => {
    if (!members) return;
    const to = Math.max(0, Math.min(members.length - 1, picked + delta));
    if (to === picked) return;
    const next = [...members];
    const [item] = next.splice(picked, 1);
    next.splice(to, 0, item);
    setMembers(next);
    setPicked(to);
  };
  const save = () => {
    if (kind === "by_field" && !target) {
      setError("請選擇排序依據的欄位");
      return;
    }
    if (kind === "manual" && !members?.length) {
      setError("沒有可以排列的成員");
      return;
    }
    const by: FieldUse | null =
      kind === "by_field" && target
        ? { field: target.id, agg: target.level === "aggregate" ? null : (aggs.some(([a]) => a === byAgg) ? byAgg : aggs[0][0]), date: null, continuous: true, table_calc: null }
        : null;
    editor.apply((s: SheetIR) =>
      setSort(s, use.field, { kind, order: kind === "manual" ? "asc" : order, by, nested: kind === "by_field" && nested && hasOuter, manual_values: kind === "manual" ? (members ?? []) : [] }),
    );
    onClose();
  };
  return (
    <Modal title={`排序［${field?.caption ?? use.field}］`} onClose={onClose}>
      <div className="action-form sort-dialog">
        <label className="inline">
          排序依據
          <select aria-label="排序依據" value={kind} onChange={(e) => setKind(e.target.value as SortKind)}>
            {SORT_KINDS.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {kind !== "manual" && (
          <label className="inline">
            排序順序
            <select aria-label="排序順序" value={order} onChange={(e) => setOrder(e.target.value as "asc" | "desc")}>
              <option value="asc">遞增</option>
              <option value="desc">遞減</option>
            </select>
          </label>
        )}
        {kind === "by_field" && (
          <>
            <label className="inline">
              欄位名稱
              <select aria-label="排序的欄位" value={byField} onChange={(e) => setByField(e.target.value)}>
                {candidates.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.caption}
                  </option>
                ))}
              </select>
            </label>
            {target && target.level !== "aggregate" && (
              <label className="inline">
                彙總
                <select aria-label="排序的彙總" value={aggs.some(([a]) => a === byAgg) ? byAgg : aggs[0][0]} onChange={(e) => setByAgg(e.target.value)}>
                  {aggs.map(([agg, label]) => (
                    <option key={agg} value={agg}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {hasOuter && (
              <label className="inline" title="在外層維度的每個值之內各自排序（各窗格的順序可以不同）；不勾選時以所有資料的值排序">
                <input type="checkbox" checked={nested} onChange={(e) => setNested(e.target.checked)} /> 巢狀（各窗格各自排序）
              </label>
            )}
          </>
        )}
        {kind === "manual" && (
          <div className="manual-sort">
            {members === null && !error && <p className="hint">載入中…</p>}
            <ul className="value-list" aria-label="成員的順序">
              {(members ?? []).map((value, i) => (
                <li key={JSON.stringify(value)} className={i === picked ? "picked" : ""} onClick={() => setPicked(i)}>
                  {display(value)}
                </li>
              ))}
            </ul>
            <span className="manual-sort-buttons">
              <button type="button" aria-label="移到最上面" onClick={() => move(-(members?.length ?? 0))}>
                ⤒
              </button>
              <button type="button" aria-label="上移" onClick={() => move(-1)}>
                ▲
              </button>
              <button type="button" aria-label="下移" onClick={() => move(1)}>
                ▼
              </button>
              <button type="button" aria-label="移到最下面" onClick={() => move(members?.length ?? 0)}>
                ⤓
              </button>
            </span>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save}>
            確定
          </button>
          {current && (
            <button
              type="button"
              onClick={() => {
                editor.apply((s: SheetIR) => setSort(s, use.field, null));
                onClose();
              }}
            >
              清除
            </button>
          )}
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 表計算

const CALC_TYPES: [string, string][] = [
  ["difference", "差異"],
  ["percent_difference", "差異百分比"],
  ["percent_from", "百分比"],
  ["percent_of_total", "合計百分比"],
  ["rank", "排名"],
  ["percentile", "百分位數"],
  ["running_total", "累計"],
  ["moving", "移動計算"],
];
const RUNNING_AGGS: [string, string][] = [
  ["sum", "總和"],
  ["avg", "平均"],
  ["min", "最小值"],
  ["max", "最大值"],
];
const MOVING_AGGS: [string, string][] = [...RUNNING_AGGS];
const RANK_TYPES: [string, string][] = [
  ["competition", "競爭（1、2、2、4）"],
  ["modified", "修正的競爭（1、3、3、4）"],
  ["dense", "密集（1、2、2、3）"],
  ["unique", "唯一（1、2、3、4）"],
];
const RELATIVE: [string, string][] = [
  ["previous", "上一個"],
  ["next", "下一個"],
  ["first", "第一個"],
  ["last", "最後一個"],
];

/** 各類型的預設選項（換類型時使用）。 */
export function defaultCalcOptions(type: string): Record<string, Scalar> {
  if (type === "running_total") return { aggregation: "sum" };
  if (type === "rank") return { rank_type: "competition", direction: "desc" };
  if (type === "percentile") return { direction: "asc" };
  if (type === "moving") return { aggregation: "avg", start: -2, end: 0, null_if_incomplete: false };
  if (type === "difference" || type === "percent_difference" || type === "percent_from") return { offset: -1 };
  return {};
}

function relativeOf(options: Record<string, Scalar>): string {
  if (options.relative_to === "first" || options.relative_to === "last") return String(options.relative_to);
  return Number(options.offset ?? -1) > 0 ? "next" : "previous";
}

/**
 * 表計算對話框（Tableau 的「編輯表計算」）：計算類型與選項、計算方向（表格、窗格、儲存格或指定維度）、
 * 計算層級與重新開始間隔。Table Calculation 計算欄位只有計算方向。
 */
export function TableCalcDialog({ tableCalc, onClose }: { tableCalc: string; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const tc = sheet.table_calcs.find((t) => t.id === tableCalc);
  // 檢視中的維度（欄列架、標記卡），不重複
  const view = useMemo(() => {
    const all = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks)].filter((u) => !isSpecial(u) && !isMeasure(u, fields));
    const unique: FieldUse[] = [];
    for (const u of all) if (!unique.some((v) => sameUse(u, v))) unique.push(u);
    return unique;
  }, [sheet, fields]);
  const initial = useMemo(() => {
    if (!tc) return [] as FieldUse[];
    if (tc.instances.length > 0) return tc.instances.filter((u) => view.some((v) => sameUse(u, v)));
    return view.filter((u) => tc.addressing.includes(u.field));
  }, [tc, view]);
  const [type, setType] = useState(tc?.type ?? "running_total");
  const [options, setOptions] = useState<Record<string, Scalar>>(tc?.options ?? {});
  const [preset, setPreset] = useState(tc?.preset ?? "table_across");
  const [order, setOrder] = useState<FieldUse[]>(() => [...initial, ...view.filter((u) => !initial.some((v) => sameUse(u, v)))]);
  const [chosen, setChosen] = useState<Set<string>>(() => new Set(initial.map(useKey)));
  const [level, setLevel] = useState<string>(tc?.level ? useKey(tc.level) : "");
  const [restart, setRestart] = useState<string>(tc?.restart ? useKey(tc.restart) : "");
  if (!tc) return null;
  const formula = tc.type === "formula";
  const set = (patch: Record<string, Scalar>) => setOptions((o) => ({ ...o, ...patch }));
  const move = (i: number, delta: number) => {
    const next = [...order];
    const [item] = next.splice(i, 1);
    next.splice(Math.max(0, Math.min(next.length, i + delta)), 0, item);
    setOrder(next);
  };
  const addressed = order.filter((u) => chosen.has(useKey(u)));
  const difference = type === "difference" || type === "percent_difference" || type === "percent_from";
  const save = () => {
    const specific = preset === "specific";
    const ids = [...new Set(addressed.map((u) => u.field))];
    // 同一欄位只勾選部分的實例（例如年而不是月）時記下實例
    const partial = view.some((u) => ids.includes(u.field) && !chosen.has(useKey(u)));
    const cleaned: Record<string, Scalar> = { ...options };
    if (!difference) {
      delete cleaned.offset;
      delete cleaned.relative_to;
      delete cleaned.compounded;
    }
    editor.apply((s: SheetIR) => ({
      ...s,
      table_calcs: s.table_calcs.map((t) =>
        t.id === tc.id
          ? {
              ...t,
              type: formula ? t.type : type,
              options: formula ? t.options : cleaned,
              preset,
              ...(specific ? { addressing: ids, instances: partial ? addressed : [] } : {}),
              level: specific ? (addressed.find((u) => useKey(u) === level) ?? null) : null,
              restart: specific ? (addressed.find((u) => useKey(u) === restart) ?? null) : null,
              inferred: false,
            }
          : t,
      ),
    }));
    onClose();
  };
  const number = (key: string, fallback: number) => Number(options[key] ?? fallback);
  return (
    <Modal title="表計算" onClose={onClose}>
      <div className="action-form table-calc-dialog">
        {!formula && (
          <>
            <label className="inline">
              計算類型
              <select
                aria-label="計算類型"
                value={type}
                onChange={(e) => {
                  setType(e.target.value);
                  setOptions(defaultCalcOptions(e.target.value));
                }}
              >
                {CALC_TYPES.map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {difference && (
              <>
                <label className="inline">
                  相對於
                  <select
                    aria-label="相對於"
                    value={relativeOf(options)}
                    onChange={(e) => {
                      const value = e.target.value;
                      const { offset: _offset, relative_to: _relative, ...rest } = options;
                      setOptions(value === "first" || value === "last" ? { ...rest, relative_to: value } : { ...rest, offset: value === "next" ? 1 : -1 });
                    }}
                  >
                    {RELATIVE.map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                {type !== "difference" && (
                  <label className="inline" title="POWER(目前的值 / 比較的值, 1 / 相隔的標記數) - 1">
                    <input type="checkbox" checked={options.compounded === true} onChange={(e) => set({ compounded: e.target.checked })} /> 複合成長率
                  </label>
                )}
              </>
            )}
            {type === "running_total" && (
              <label className="inline">
                累計的彙總
                <select aria-label="累計的彙總" value={String(options.aggregation ?? "sum")} onChange={(e) => set({ aggregation: e.target.value })}>
                  {RUNNING_AGGS.map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {type === "moving" && (
              <>
                <label className="inline">
                  移動計算的彙總
                  <select aria-label="移動計算的彙總" value={String(options.aggregation ?? "avg")} onChange={(e) => set({ aggregation: e.target.value })}>
                    {MOVING_AGGS.map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="inline">
                  前面的值
                  <input
                    aria-label="前面的值"
                    className="narrow"
                    type="number"
                    min={0}
                    value={-number("start", -2)}
                    onChange={(e) => set({ start: -Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
                  />
                </label>
                <label className="inline">
                  後面的值
                  <input
                    aria-label="後面的值"
                    className="narrow"
                    type="number"
                    min={0}
                    value={number("end", 0)}
                    onChange={(e) => set({ end: Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
                  />
                </label>
                <label className="inline">
                  <input type="checkbox" checked={options.null_if_incomplete === true} onChange={(e) => set({ null_if_incomplete: e.target.checked })} />{" "}
                  值不足時為 Null
                </label>
              </>
            )}
            {type === "rank" && (
              <label className="inline">
                排名方式
                <select aria-label="排名方式" value={String(options.rank_type ?? "competition")} onChange={(e) => set({ rank_type: e.target.value })}>
                  {RANK_TYPES.map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {(type === "rank" || type === "percentile") && (
              <label className="inline">
                排名順序
                <select aria-label="排名順序" value={String(options.direction ?? (type === "rank" ? "desc" : "asc"))} onChange={(e) => set({ direction: e.target.value })}>
                  <option value="desc">遞減</option>
                  <option value="asc">遞增</option>
                </select>
              </label>
            )}
          </>
        )}
        <label className="inline">
          計算方向
          <select aria-label="計算方向" value={preset} onChange={(e) => setPreset(e.target.value)}>
            {DIRECTIONS.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
            <option value="specific">指定維度</option>
          </select>
        </label>
        {preset === "specific" && (
          <>
            <ul className="direction-list">
              {order.map((u, i) => (
                <li key={useKey(u)}>
                  <label>
                    <input
                      type="checkbox"
                      checked={chosen.has(useKey(u))}
                      onChange={(e) => {
                        const next = new Set(chosen);
                        if (e.target.checked) next.add(useKey(u));
                        else next.delete(useKey(u));
                        setChosen(next);
                      }}
                    />{" "}
                    {pillLabel(u, fields, sheet.table_calcs)}
                  </label>
                  <span>
                    <button type="button" className="link" aria-label="上移" onClick={() => move(i, -1)}>
                      ▲
                    </button>
                    <button type="button" className="link" aria-label="下移" onClick={() => move(i, 1)}>
                      ▼
                    </button>
                  </span>
                </li>
              ))}
            </ul>
            <p className="hint">勾選的維度為計算的方向（依序）；沒有勾選的維度把資料分成各自計算的區段。</p>
            <label className="inline" title="例如年增率：計算方向是年與季、計算層級是年時，每一季各自與前一年的同一季比較">
              計算層級
              <select aria-label="計算層級" value={level} onChange={(e) => setLevel(e.target.value)}>
                <option value="">最深</option>
                {addressed.map((u) => (
                  <option key={useKey(u)} value={useKey(u)}>
                    {pillLabel(u, fields, sheet.table_calcs)}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline" title="例如年初至今：計算方向是年與月、重新開始間隔是年時，每一年重新累計">
              重新開始間隔
              <select aria-label="重新開始間隔" value={restart} onChange={(e) => setRestart(e.target.value)}>
                <option value="">無</option>
                {addressed.map((u) => (
                  <option key={useKey(u)} value={useKey(u)}>
                    {pillLabel(u, fields, sheet.table_calcs)}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save} disabled={preset === "specific" && chosen.size === 0}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 檢視中日期欄位的年（離散的年或年的截斷）與比它細的日期層級：年初至今、年成長率用。 */
export function dateLevels(sheet: SheetIR, fields: Map<string, CatalogField>): { year: FieldUse; finer: FieldUse[] } | null {
  const rank: Record<string, number> = { year: 0, quarter: 1, month: 2, week: 3, day: 4 };
  const dates = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks)].filter(
    (u) => !isSpecial(u) && !isMeasure(u, fields) && u.date !== null && u.date.unit in rank,
  );
  const year = dates.find((u) => u.date?.unit === "year");
  if (!year) return null;
  const finer: FieldUse[] = [];
  for (const u of dates) {
    if (u.field === year.field && rank[u.date!.unit] > 0 && !finer.some((v) => sameUse(u, v))) finer.push(u);
  }
  return { year, finer };
}

/** 以日期層級定址的快速表計算：年初至今總計（每年重新累計）與年成長率（每個期間與前一年同期比較）。 */
export function dateCalc(base: TableCalcIR, kind: "ytd" | "yoy", levels: { year: FieldUse; finer: FieldUse[] }): TableCalcIR {
  const addressed = [levels.year, ...levels.finer];
  return {
    ...base,
    type: kind === "ytd" ? "running_total" : "percent_difference",
    options: kind === "ytd" ? { aggregation: "sum" } : { offset: -1 },
    preset: "specific",
    addressing: [...new Set(addressed.map((u) => u.field))],
    instances: addressed,
    level: kind === "yoy" ? levels.year : null,
    restart: kind === "ytd" ? levels.year : null,
    inferred: false,
  };
}

// ---------------------------------------------------------------- 註解

export interface AnnotationIR {
  kind: "mark" | "point" | "area";
  text: RichRun[];
  point?: AnnotationPointIR | null;
  top_left?: AnnotationPointIR | null;
  bottom_right?: AnnotationPointIR | null;
  offset: [number, number];
}

interface AnnotationPointIR {
  x: Scalar;
  x_axis: FieldUse | null;
  y: Scalar;
  y_axis: FieldUse | null;
  mark: Record<string, Scalar> | null;
}

/** 欄、列架上第一個連續的量值（註解的點與區域以它的座標軸定位）。 */
function axisOf(uses: FieldUse[], fields: Map<string, CatalogField>): FieldUse | null {
  return uses.find((u) => !isSpecial(u) && u.continuous && isMeasure(u, fields)) ?? null;
}


function numberOrNull(text: string): number | null {
  const value = Number(text);
  return text.trim() !== "" && Number.isFinite(value) ? value : null;
}

/**
 * 註解（Tableau 的「加上註解＞標記、點、區域」）：標記的註解跟著選取的標記；點與區域以座標軸上的值定位。
 * mark 為檢視上選取的標記（維度的欄位鍵 → 值），沒有選取時不能加標記的註解。
 */
export function AnnotationsDialog({ mark, onClose }: { mark: Record<string, Scalar> | null; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const annotations = (sheet.annotations as AnnotationIR[] | undefined) ?? [];
  const xAxis = axisOf(sheet.columns, fields);
  const yAxis = axisOf(sheet.rows, fields);
  const [kind, setKind] = useState<AnnotationIR["kind"]>(mark ? "mark" : "point");
  const [text, setText] = useState<RichRun[]>([]);
  // 加上一個註解後換一個新的編輯區（清空）
  const [round, setRound] = useState(0);
  // 標記的註解可以插入這個標記的欄位值；點與區域只有參數
  const tokens = textTokens(sheet, fields, editor.catalog, { tableCalcs: true, uses: kind === "mark" });
  const [coords, setCoords] = useState({ x: "", y: "", x2: "", y2: "" });
  const [error, setError] = useState<string | null>(null);
  const point = (x: string, y: string): AnnotationPointIR => ({
    x: xAxis ? numberOrNull(x) : null,
    x_axis: xAxis,
    y: yAxis ? numberOrNull(y) : null,
    y_axis: yAxis,
    mark: null,
  });
  const complete = (p: AnnotationPointIR) => (!xAxis || p.x !== null) && (!yAxis || p.y !== null);
  const add = () => {
    if (text.length === 0) {
      setError("請輸入註解的文字");
      return;
    }
    let annotation: AnnotationIR;
    if (kind === "mark") {
      if (!mark) return;
      annotation = { kind, text, point: { x: null, x_axis: null, y: null, y_axis: null, mark }, offset: [40, -40] };
    } else if (kind === "point") {
      const at = point(coords.x, coords.y);
      if (!complete(at)) {
        setError("請輸入座標軸上的值");
        return;
      }
      annotation = { kind, text, point: at, offset: [40, -40] };
    } else {
      const [a, b] = [point(coords.x, coords.y), point(coords.x2, coords.y2)];
      if (!complete(a) || !complete(b)) {
        setError("請輸入區域兩個角落在座標軸上的值");
        return;
      }
      annotation = { kind, text, top_left: a, bottom_right: b, offset: [0, 0] };
    }
    editor.apply((s: SheetIR) => ({ ...s, annotations: [...((s.annotations as AnnotationIR[] | undefined) ?? []), annotation] }));
    setText([]);
    setRound((n) => n + 1);
    setError(null);
  };
  const noAxis = !xAxis && !yAxis;
  const coordinate = (key: "x" | "y" | "x2" | "y2", label: string) => (
    <label className="inline">
      {label}
      <input aria-label={label} className="narrow-number" value={coords[key]} onChange={(e) => setCoords({ ...coords, [key]: e.target.value })} />
    </label>
  );
  return (
    <Modal title="註解" onClose={onClose}>
      <div className="action-form annotations-dialog">
        {annotations.length > 0 && (
          <table className="list">
            <tbody>
              {annotations.map((annotation, i) => (
                <tr key={i}>
                  <td>{annotation.kind === "mark" ? "標記" : annotation.kind === "point" ? "點" : "區域"}</td>
                  <td>{plainText(annotation.text, textTokens(sheet, fields, editor.catalog, { tableCalcs: true }))}</td>
                  <td>
                    <button
                      type="button"
                      className="link"
                      aria-label={`移除註解 ${i + 1}`}
                      onClick={() => editor.apply((s: SheetIR) => ({ ...s, annotations: ((s.annotations as AnnotationIR[] | undefined) ?? []).filter((_, n) => n !== i) }))}
                    >
                      移除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <label className="inline">
          註解的類型
          <select aria-label="註解的類型" value={kind} onChange={(e) => setKind(e.target.value as AnnotationIR["kind"])}>
            <option value="mark" disabled={!mark}>
              標記{mark ? "" : "（先在檢視上選取一個標記）"}
            </option>
            <option value="point" disabled={noAxis}>
              點
            </option>
            <option value="area" disabled={noAxis}>
              區域
            </option>
          </select>
        </label>
        <RichTextEditor key={`${round}:${kind}`} label="註解的文字" value={text} tokens={tokens} onChange={setText} rows={3} />
        {kind !== "mark" && noAxis && <p className="hint">檢視中沒有連續的量值座標軸，只能加標記的註解。</p>}
        {kind === "point" && (
          <div className="row">
            {xAxis && coordinate("x", `${pillLabel(xAxis, fields, sheet.table_calcs)} 的值`)}
            {yAxis && coordinate("y", `${pillLabel(yAxis, fields, sheet.table_calcs)} 的值`)}
          </div>
        )}
        {kind === "area" && (
          <>
            <div className="row">
              {xAxis && coordinate("x", `${pillLabel(xAxis, fields, sheet.table_calcs)} 的起點`)}
              {yAxis && coordinate("y", `${pillLabel(yAxis, fields, sheet.table_calcs)} 的起點`)}
            </div>
            <div className="row">
              {xAxis && coordinate("x2", `${pillLabel(xAxis, fields, sheet.table_calcs)} 的終點`)}
              {yAxis && coordinate("y2", `${pillLabel(yAxis, fields, sheet.table_calcs)} 的終點`)}
            </div>
          </>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={add} disabled={kind === "mark" ? !mark : noAxis}>
            加上註解
          </button>
          <button type="button" onClick={onClose}>
            關閉
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 套用到工作表

/**
 * 篩選套用到其他工作表（Tableau 的「套用到工作表」）：僅此工作表、選取的工作表，或使用此資料源的所有工作表。
 * 套用的工作表上各有一個相同的篩選，檢視器中一起變更。
 */
export function ApplyFilterDialog({ filter, onClose }: { filter: string; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, workbook } = editor;
  const [sheets, setSheets] = useState<{ id: string; name: string; applied: boolean }[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let cancelled = false;
    editApi
      .filterTargets(workbook, sheet.id, filter)
      .then((found) => {
        if (cancelled) return;
        setSheets(found);
        setChosen(new Set(found.filter((w) => w.applied).map((w) => w.id)));
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook, sheet.id, filter]);
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await editApi.applyFilter(workbook, sheet.id, filter, [...chosen]);
      // 這張工作表上的篩選記下群組（其他工作表已由伺服器更新）
      editor.apply((s: SheetIR) => ({ ...s, filters: updated.filters }));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal title="將篩選套用到工作表" onClose={onClose}>
      <div className="action-form apply-filter-dialog">
        {sheets === null && !error && <p className="hint">載入中…</p>}
        {sheets !== null && sheets.length === 0 && <p className="hint">沒有其他使用同一個資料源的工作表。</p>}
        {sheets !== null && sheets.length > 0 && (
          <>
            <div className="row">
              <button type="button" onClick={() => setChosen(new Set(sheets.map((w) => w.id)))}>
                使用此資料源的所有工作表
              </button>
              <button type="button" onClick={() => setChosen(new Set())}>
                僅此工作表
              </button>
            </div>
            <div className="check-list" role="group" aria-label="套用的工作表">
              {sheets.map((w) => (
                <label key={w.id}>
                  <input
                    type="checkbox"
                    checked={chosen.has(w.id)}
                    onChange={(e) => {
                      const next = new Set(chosen);
                      if (e.target.checked) next.add(w.id);
                      else next.delete(w.id);
                      setChosen(next);
                    }}
                  />{" "}
                  {w.name}
                </label>
              ))}
            </div>
            <p className="hint">套用的工作表上原本同欄位的篩選由這個篩選取代；取消勾選的工作表上移除這個篩選。</p>
          </>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void save()} disabled={saving || sheets === null}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 標籤

interface MarkLabelsIR {
  show: boolean | null;
  marks: "all" | "min_max" | "line_ends" | "selected" | "highlighted";
  scope: "pane" | "cell" | "table" | "line";
  field: FieldUse | null;
  minimum: boolean;
  maximum: boolean;
  first: boolean;
  last: boolean;
  overlap: boolean;
}

const DEFAULT_LABELS: MarkLabelsIR = { show: null, marks: "all", scope: "pane", field: null, minimum: true, maximum: true, first: true, last: true, overlap: false };

/**
 * 標籤（Tableau 標記卡的「標籤」）：是否顯示標記標籤、自訂的標籤文字（文字、欄位值與參數值，可以設定格式）、
 * 要標示的標記（全部、最小值／最大值、線條的兩端、選取的、醒目提示的）與是否允許重疊。
 */
export function LabelDialog({ card, onClose }: { card: Card; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const marks = cardMarks(sheet, card);
  const [text, setText] = useState<RichRun[]>((marks.label_text as RichRun[] | undefined) ?? []);
  const [labels, setLabels] = useState<MarkLabelsIR>({ ...DEFAULT_LABELS, ...((marks.labels as Partial<MarkLabelsIR> | null | undefined) ?? {}) });
  const set = (patch: Partial<MarkLabelsIR>) => setLabels((current) => ({ ...current, ...patch }));
  // 對齊：水平（標籤在標記的左、中、右）與垂直（上、中、下）；空的為自動
  const [align, setAlign] = useState<string>((marks.cell_align as string | null | undefined) ?? "");
  const [valign, setValign] = useState<string>((marks.label_valign as string | null | undefined) ?? "");
  const tokens = textTokens(sheet, fields, editor.catalog, { tableCalcs: true });
  const save = () => {
    const changed = JSON.stringify(labels) !== JSON.stringify(DEFAULT_LABELS);
    editor.apply((s: SheetIR) =>
      updateCard(s, card, (m) => ({ ...m, label_text: text, labels: changed ? labels : null, cell_align: align || null, label_valign: valign || null })),
    );
    onClose();
  };
  return (
    <Modal title="標籤" onClose={onClose}>
      <div className="action-form label-dialog">
        <label className="inline">
          顯示標記標籤
          <select
            aria-label="顯示標記標籤"
            value={labels.show === null ? "" : labels.show ? "on" : "off"}
            onChange={(e) => set({ show: e.target.value === "" ? null : e.target.value === "on" })}
          >
            <option value="">自動（標籤上有欄位時顯示）</option>
            <option value="on">顯示</option>
            <option value="off">不顯示</option>
          </select>
        </label>
        <p className="hint">標籤的文字：留空白時依標籤上的欄位顯示。</p>
        <RichTextEditor label="標籤的文字" value={text} tokens={tokens} onChange={setText} rows={3} />
        <label className="inline">
          要標示的標記
          <select aria-label="要標示的標記" value={labels.marks} onChange={(e) => set({ marks: e.target.value as MarkLabelsIR["marks"] })}>
            <option value="all">全部</option>
            <option value="min_max">最小值／最大值</option>
            <option value="line_ends">線條的兩端</option>
            <option value="selected">選取的</option>
            <option value="highlighted">醒目提示的</option>
          </select>
        </label>
        {labels.marks === "min_max" && (
          <>
            <label className="inline">
              範圍
              <select aria-label="最小值與最大值的範圍" value={labels.scope} onChange={(e) => set({ scope: e.target.value as MarkLabelsIR["scope"] })}>
                <option value="pane">窗格</option>
                <option value="cell">儲存格</option>
                <option value="table">整個表格</option>
                <option value="line">每條線</option>
              </select>
            </label>
            <label className="inline">
              <input type="checkbox" checked={labels.minimum} onChange={(e) => set({ minimum: e.target.checked })} /> 標示最小值
            </label>
            <label className="inline">
              <input type="checkbox" checked={labels.maximum} onChange={(e) => set({ maximum: e.target.checked })} /> 標示最大值
            </label>
          </>
        )}
        {labels.marks === "line_ends" && (
          <>
            <label className="inline">
              <input type="checkbox" checked={labels.first} onChange={(e) => set({ first: e.target.checked })} /> 標示線條的起點
            </label>
            <label className="inline">
              <input type="checkbox" checked={labels.last} onChange={(e) => set({ last: e.target.checked })} /> 標示線條的終點
            </label>
          </>
        )}
        <label className="inline">
          <input type="checkbox" checked={labels.overlap} onChange={(e) => set({ overlap: e.target.checked })} /> 允許標籤與其他標記重疊
        </label>
        <label className="inline" title="標籤相對於標記的位置（Tableau 標籤的「對齊」）；文字表是儲存格中的對齊">
          水平對齊
          <select aria-label="標籤的水平對齊" value={align} onChange={(e) => setAlign(e.target.value)}>
            <option value="">自動</option>
            <option value="left">靠左</option>
            <option value="center">置中</option>
            <option value="right">靠右</option>
          </select>
        </label>
        <label className="inline">
          垂直對齊
          <select aria-label="標籤的垂直對齊" value={valign} onChange={(e) => setValign(e.target.value)}>
            <option value="">自動</option>
            <option value="top">上</option>
            <option value="center">中</option>
            <option value="bottom">下</option>
          </select>
        </label>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 編輯混合關係

interface BlendIR {
  datasource: string;
  links: { primary: FieldUse; secondary: FieldUse }[];
}

const plainUse = (field: string): FieldUse => ({ field, agg: null, date: null, continuous: false, table_calc: null });

/**
 * 編輯混合關係（Tableau 的「資料＞編輯混合關係」）：各次要資料源與主要資料源以哪些欄位連結。預設以名稱相同的
 * 維度自動連結；這裡可以改成自訂的對應（例如主要資料源的 Region 對應次要資料源的 Area）。檢視中有主要資料源的
 * 連結欄位時，那個連結才作用。
 */
export function BlendsDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const { sheet, catalog } = editor;
  const [blends, setBlends] = useState<BlendIR[]>(() => JSON.parse(JSON.stringify((sheet.blends as BlendIR[] | undefined) ?? [])) as BlendIR[]);
  const [error, setError] = useState<string | null>(null);
  const dimensions = (datasource: string) =>
    (catalog.datasources.find((d) => d.id === datasource)?.fields ?? []).filter(
      (f) => !f.hidden && f.role === "dimension" && f.level !== "aggregate" && f.level !== "table_calc" && f.datatype !== "spatial",
    );
  const primary = dimensions(sheet.datasource);
  const change = (index: number, links: BlendIR["links"]) => setBlends(blends.map((b, i) => (i === index ? { ...b, links } : b)));
  const save = () => {
    if (blends.some((b) => b.links.length === 0)) {
      setError("每個次要資料源至少要有一個連結欄位");
      return;
    }
    editor.apply((s: SheetIR) => ({ ...s, blends }));
    onClose();
  };
  return (
    <Modal title="編輯混合關係" onClose={onClose}>
      <div className="action-form blends-dialog">
        {blends.length === 0 && <p className="hint">這張工作表沒有混合其他資料源；把另一個資料源的欄位放到檢視上即混合。</p>}
        {blends.map((blend, index) => {
          const secondary = dimensions(blend.datasource);
          const caption = catalog.datasources.find((d) => d.id === blend.datasource)?.caption ?? blend.datasource;
          return (
            <fieldset key={blend.datasource} data-blend={blend.datasource}>
              <legend>次要資料源：{caption}</legend>
              {blend.links.map((link, n) => (
                <div className="row" key={n}>
                  <select
                    aria-label={`連結 ${n + 1} 的主要資料源欄位`}
                    value={link.primary.field}
                    onChange={(e) => change(index, blend.links.map((l, i) => (i === n ? { ...l, primary: plainUse(e.target.value) } : l)))}
                  >
                    {primary.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.caption}
                      </option>
                    ))}
                  </select>
                  <span>＝</span>
                  <select
                    aria-label={`連結 ${n + 1} 的次要資料源欄位`}
                    value={link.secondary.field}
                    onChange={(e) => change(index, blend.links.map((l, i) => (i === n ? { ...l, secondary: plainUse(e.target.value) } : l)))}
                  >
                    {secondary.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.caption}
                      </option>
                    ))}
                  </select>
                  <button type="button" className="link" aria-label={`移除連結 ${n + 1}`} onClick={() => change(index, blend.links.filter((_, i) => i !== n))}>
                    移除
                  </button>
                </div>
              ))}
              <button
                type="button"
                disabled={primary.length === 0 || secondary.length === 0}
                onClick={() => change(index, [...blend.links, { primary: plainUse(primary[0].id), secondary: plainUse(secondary[0].id) }])}
              >
                新增連結
              </button>
            </fieldset>
          );
        })}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save} disabled={blends.length === 0}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
