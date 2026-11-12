// 編輯器的對話框：計算欄位、計算方向（指定維度）、數字格式、標題、座標軸、參考線、趨勢線、參數、階層與色彩。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi, type FormulaCheck, type Outline } from "./api";
import { useEditor } from "./context";
import {
  axisMeasures,
  type Card,
  cardMarks,
  type CatalogField,
  type ClusterIR,
  EDITOR_REF,
  type ForecastIR,
  GEO_ROLES,
  type GeoAlias,
  type GeoRole,
  isMeasure,
  isSpecial,
  type MarksIR,
  markUses,
  pillLabel,
  sameUse,
  type SheetIR,
  type TooltipSheetIR,
  updateCard,
  useKey,
  viewMeasures,
} from "./model";

const LEVELS: Record<string, string> = { row: "列層級", aggregate: "彙總", lod: "LOD 運算式", table_calc: "Table Calculation" };
const TYPES: Record<string, string> = {
  string: "字串",
  integer: "整數",
  real: "實數",
  boolean: "布林",
  date: "日期",
  datetime: "日期時間",
};

// ---------------------------------------------------------------- 計算欄位

export function CalcDialog({
  field,
  datasource,
  onClose,
  onSaved,
}: {
  field: CatalogField | null;
  datasource?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const ds = datasource ?? editor.sheet.datasource;
  const [caption, setCaption] = useState(field?.caption ?? "計算 1");
  const [formula, setFormula] = useState(field?.formula ?? "");
  const [check, setCheck] = useState<FormulaCheck | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!formula.trim()) {
      setCheck(null);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      editApi
        .checkFormula(editor.workbook, ds, formula, field?.id)
        .then((result) => !cancelled && setCheck(result))
        .catch((e: Error) => !cancelled && setCheck({ ok: false, message: e.message }));
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [editor.workbook, ds, formula, field?.id]);
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await editApi.saveCalc(editor.workbook, ds, caption, formula, field?.id);
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const own = editor.catalog.datasources.find((d) => d.id === ds)?.fields ?? [];
  const fields = own.filter((f) => !f.hidden && f.id !== field?.id);
  return (
    <Modal title={field ? `編輯計算欄位：${field.caption}` : "建立計算欄位"} onClose={onClose} wide>
      <div className="calc-dialog">
        <label>
          名稱
          <input aria-label="計算欄位名稱" value={caption} onChange={(e) => setCaption(e.target.value)} />
        </label>
        <textarea
          aria-label="公式"
          className="formula"
          rows={8}
          spellCheck={false}
          value={formula}
          placeholder="例如 SUM([Profit]) / SUM([Sales])"
          onChange={(e) => setFormula(e.target.value)}
        />
        <div className="calc-status" aria-live="polite">
          {check === null ? (
            <span className="hint">以 [欄位名稱] 參照欄位、[參數名稱] 參照參數</span>
          ) : check.ok ? (
            <span className="success">
              計算有效：{TYPES[check.datatype ?? ""] ?? check.datatype}、{LEVELS[check.level ?? ""] ?? check.level}（{check.role === "measure" ? "量值" : "維度"}）
            </span>
          ) : (
            <span className="error">{check.message}</span>
          )}
        </div>
        <details>
          <summary>欄位</summary>
          <ul className="calc-fields">
            {fields.map((f) => (
              <li key={f.id}>
                <button type="button" className="link" onClick={() => setFormula((text) => `${text}[${f.caption}]`)}>
                  {f.caption}
                </button>
              </li>
            ))}
            {editor.catalog.parameters.map((p) => (
              <li key={p.id}>
                <button type="button" className="link" onClick={() => setFormula((text) => `${text}[${p.caption}]`)}>
                  {p.caption}（參數）
                </button>
              </li>
            ))}
          </ul>
        </details>
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={saving || !check?.ok || !caption.trim()} onClick={() => void save()}>
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

// ---------------------------------------------------------------- 計算方向：指定維度

export function DirectionDialog({ tableCalc, onClose }: { tableCalc: string; onClose: () => void }) {
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
  const [order, setOrder] = useState<FieldUse[]>(() => [...initial, ...view.filter((u) => !initial.some((v) => sameUse(u, v)))]);
  const [chosen, setChosen] = useState<Set<string>>(() => new Set(initial.map(useKey)));
  const [restart, setRestart] = useState<string>(tc?.restart ? useKey(tc.restart) : "");
  if (!tc) return null;
  const move = (i: number, delta: number) => {
    const next = [...order];
    const [item] = next.splice(i, 1);
    next.splice(Math.max(0, Math.min(next.length, i + delta)), 0, item);
    setOrder(next);
  };
  const save = () => {
    const addressed = order.filter((u) => chosen.has(useKey(u)));
    const ids = [...new Set(addressed.map((u) => u.field))];
    // 同一欄位只勾選部分的實例（例如年而不是月）時記下實例
    const partial = view.some((u) => ids.includes(u.field) && !chosen.has(useKey(u)));
    const restartUse = addressed.find((u) => useKey(u) === restart) ?? null;
    editor.apply((s: SheetIR) => ({
      ...s,
      table_calcs: s.table_calcs.map((t) =>
        t.id === tc.id ? { ...t, preset: "specific", addressing: ids, instances: partial ? addressed : [], restart: restartUse, inferred: false } : t,
      ),
    }));
    onClose();
  };
  return (
    <Modal title="計算方向：指定維度" onClose={onClose}>
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
      <label className="inline">
        重新開始間隔
        <select aria-label="重新開始間隔" value={restart} onChange={(e) => setRestart(e.target.value)}>
          <option value="">無</option>
          {order
            .filter((u) => chosen.has(useKey(u)))
            .map((u) => (
              <option key={useKey(u)} value={useKey(u)}>
                {pillLabel(u, fields, sheet.table_calcs)}
              </option>
            ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button type="button" className="primary" onClick={save} disabled={chosen.size === 0}>
          確定
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 數字格式

type FormatKind = "auto" | "number" | "currency" | "percent";

/** 數字格式的設定 → Tableau 的格式字串（與 tmig.render.format 認得的寫法相同）。 */
export function formatString(kind: FormatKind, decimals: number, grouping: boolean, units: number, symbol: string): string | null {
  if (kind === "auto") return null;
  const fraction = decimals > 0 ? `.${"0".repeat(decimals)}` : "";
  if (kind === "percent") return `p0${fraction}%`;
  const integer = grouping ? "#,##0" : "0";
  const scale = ",".repeat(units);
  const suffix = ["", '"K"', '"M"', '"B"'][units] ?? "";
  if (kind === "currency") return `c"${symbol.replace(/"/g, "")}"${integer}${fraction}${scale}${suffix}`;
  return `n${integer}${fraction}${scale}${suffix}`;
}

function parseFormat(text: string | undefined): { kind: FormatKind; decimals: number; grouping: boolean; units: number; symbol: string } {
  const result = { kind: "auto" as FormatKind, decimals: 2, grouping: true, units: 0, symbol: "$" };
  if (!text) return result;
  const head = text.split(";")[0];
  if (head.startsWith("p") || head.endsWith("%")) result.kind = "percent";
  else if (head.startsWith("c")) result.kind = "currency";
  else result.kind = "number";
  const symbol = /^c"([^"]*)"/.exec(head);
  if (symbol) result.symbol = symbol[1];
  const numeric = /([#0,]+)(?:\.([0#]+))?(,*)/.exec(head.replace(/"[^"]*"/g, ""));
  if (numeric) {
    result.grouping = numeric[1].includes(",") && numeric[1].replace(/,+$/, "").includes(",");
    result.decimals = numeric[2]?.length ?? 0;
    result.units = (numeric[3]?.length ?? 0) + (numeric[1].length - numeric[1].replace(/,+$/, "").length);
  }
  return result;
}

export function FormatDialog({ use, onClose }: { use: FieldUse; onClose: () => void }) {
  const editor = useEditor();
  const key = useKey(use);
  const current = parseFormat((editor.sheet.formats as Record<string, string> | undefined)?.[key]);
  const [kind, setKind] = useState<FormatKind>(current.kind);
  const [decimals, setDecimals] = useState(current.decimals);
  const [grouping, setGrouping] = useState(current.grouping);
  const [units, setUnits] = useState(current.units);
  const [symbol, setSymbol] = useState(current.symbol);
  const text = formatString(kind, decimals, grouping, units, symbol);
  const save = () => {
    editor.apply((s: SheetIR) => {
      const formats = { ...(s.formats as Record<string, string>) };
      if (text) formats[key] = text;
      else delete formats[key];
      return { ...s, formats };
    });
    onClose();
  };
  return (
    <Modal title={`格式：${pillLabel(use, editor.fields, editor.sheet.table_calcs)}`} onClose={onClose}>
      <div className="format-dialog">
        <label className="inline">
          格式
          <select aria-label="數字格式" value={kind} onChange={(e) => setKind(e.target.value as FormatKind)}>
            <option value="auto">自動</option>
            <option value="number">數字（自訂）</option>
            <option value="currency">貨幣（自訂）</option>
            <option value="percent">百分比</option>
          </select>
        </label>
        {kind !== "auto" && (
          <>
            <label className="inline">
              小數位數
              <input type="number" min={0} max={10} aria-label="小數位數" value={decimals} onChange={(e) => setDecimals(Math.max(0, Math.min(10, Number(e.target.value))))} />
            </label>
            {kind !== "percent" && (
              <>
                <label className="inline">
                  顯示單位
                  <select aria-label="顯示單位" value={units} onChange={(e) => setUnits(Number(e.target.value))}>
                    <option value={0}>無</option>
                    <option value={1}>千（K）</option>
                    <option value={2}>百萬（M）</option>
                    <option value={3}>十億（B）</option>
                  </select>
                </label>
                <label className="inline">
                  <input type="checkbox" checked={grouping} onChange={(e) => setGrouping(e.target.checked)} /> 千分位
                </label>
              </>
            )}
            {kind === "currency" && (
              <label className="inline">
                符號
                <input aria-label="貨幣符號" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
              </label>
            )}
          </>
        )}
        <p className="hint">格式字串：{text ?? "（自動）"}</p>
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

// ---------------------------------------------------------------- 標題

export function TitleDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const runs = (editor.sheet.title as { text?: string | null; use?: FieldUse | null; parameter?: string | null }[] | undefined) ?? [];
  const plain = runs.every((r) => r.text !== null && r.text !== undefined);
  const [text, setText] = useState(plain ? runs.map((r) => r.text ?? "").join("") : "");
  const save = (value: string) => {
    editor.apply((s: SheetIR) => ({ ...s, title: value.trim() ? [{ text: value }] : [] }));
    onClose();
  };
  return (
    <Modal title="編輯標題" onClose={onClose}>
      {!plain && <p className="hint">目前的標題含有欄位或參數的值；儲存後改成這裡的文字。</p>}
      <textarea aria-label="標題" rows={3} className="formula" value={text} placeholder={editor.sheet.name} onChange={(e) => setText(e.target.value)} />
      <p className="hint">留空時以工作表名稱為標題。</p>
      <div className="dialog-actions">
        <button type="button" className="primary" onClick={() => save(text)}>
          確定
        </button>
        <button type="button" onClick={() => save("")}>
          重設
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 座標軸

interface AxisIR {
  use: FieldUse;
  shelf: "rows" | "columns";
  show?: boolean;
  title?: string | null;
  start?: Scalar;
  end?: Scalar;
  reverse?: boolean;
  include_zero?: boolean;
  synchronized?: boolean;
  [key: string]: unknown;
}

export function AxisDialog({ use, shelf, onClose }: { use: FieldUse; shelf: "rows" | "columns"; onClose: () => void }) {
  const editor = useEditor();
  const axes = (editor.sheet.axes as AxisIR[] | undefined) ?? [];
  const current = axes.find((a) => sameUse(a.use, use) && a.shelf === shelf);
  const [title, setTitle] = useState(current?.title ?? "");
  const [custom, setCustom] = useState(current?.title !== null && current?.title !== undefined);
  const [fixed, setFixed] = useState(current?.start !== null && current?.start !== undefined);
  const [start, setStart] = useState(current?.start === null || current?.start === undefined ? "" : String(current.start));
  const [end, setEnd] = useState(current?.end === null || current?.end === undefined ? "" : String(current.end));
  const [reverse, setReverse] = useState(Boolean(current?.reverse));
  const [zero, setZero] = useState(current?.include_zero !== false);
  const [sync, setSync] = useState(Boolean(current?.synchronized));
  const number = (text: string): Scalar => {
    if (!text.trim()) return null;
    const n = Number(text);
    return Number.isFinite(n) ? n : text;
  };
  const save = () => {
    const axis: AxisIR = {
      ...(current ?? { use, shelf }),
      use,
      shelf,
      title: custom ? title : null,
      start: fixed ? number(start) : null,
      end: fixed ? number(end) : null,
      reverse,
      include_zero: zero,
      synchronized: sync,
    };
    editor.apply((s: SheetIR) => {
      const list = (s.axes as AxisIR[] | undefined) ?? [];
      const others = list.filter((a) => !(sameUse(a.use, use) && a.shelf === shelf));
      return { ...s, axes: [...others, axis] };
    });
    onClose();
  };
  return (
    <Modal title={`編輯座標軸：${pillLabel(use, editor.fields, editor.sheet.table_calcs)}`} onClose={onClose}>
      <div className="axis-dialog">
        <label className="inline">
          <input type="checkbox" checked={custom} onChange={(e) => setCustom(e.target.checked)} /> 自訂標題
          <input aria-label="座標軸標題" value={title} disabled={!custom} onChange={(e) => setTitle(e.target.value)} placeholder="空白為不顯示標題" />
        </label>
        <label className="inline">
          <input type="checkbox" checked={fixed} onChange={(e) => setFixed(e.target.checked)} /> 固定範圍
        </label>
        <div className="row">
          <label>
            起點
            <input aria-label="起點" value={start} disabled={!fixed} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>
            終點
            <input aria-label="終點" value={end} disabled={!fixed} onChange={(e) => setEnd(e.target.value)} />
          </label>
        </div>
        <label className="inline">
          <input type="checkbox" checked={zero} onChange={(e) => setZero(e.target.checked)} /> 自動範圍包含 0
        </label>
        <label className="inline">
          <input type="checkbox" checked={reverse} onChange={(e) => setReverse(e.target.checked)} /> 反轉
        </label>
        <label className="inline">
          <input type="checkbox" checked={sync} onChange={(e) => setSync(e.target.checked)} /> 與前一個軸同步（雙軸）
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

// ---------------------------------------------------------------- 參考線、區間、分布、盒鬚圖

interface ReferenceIR {
  id: string;
  axis: FieldUse;
  value: FieldUse | { parameter: string } | null;
  formula: string;
  kind: "line" | "distribution" | "boxplot";
  constant: Scalar;
  percentiles: number[];
  factors: number[];
  scope: "table" | "pane" | "cell";
  label: "none" | "automatic" | "value" | "computation" | "custom";
  label_text: string | null;
  source_ref: string;
  [key: string]: unknown;
}

const REFERENCE_FORMULAS: [string, string][] = [
  ["average", "平均"],
  ["median", "中位數"],
  ["sum", "總和"],
  ["total", "總計"],
  ["min", "最小值"],
  ["max", "最大值"],
  ["constant", "常數"],
];

export function ReferenceDialog({ index, axis, onClose }: { index: number | null; axis: FieldUse | null; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const lines = (sheet.reference_lines as ReferenceIR[] | undefined) ?? [];
  const existing = index !== null ? lines[index] : undefined;
  const axes = [...sheet.rows, ...sheet.columns].filter((u) => !isSpecial(u) && isMeasure(u, fields) && u.continuous);
  const [axisKey, setAxisKey] = useState(existing ? useKey(existing.axis) : axis ? useKey(axis) : axes[0] ? useKey(axes[0]) : "");
  const [kind, setKind] = useState<"line" | "band" | "distribution" | "boxplot">(
    existing?.kind === "boxplot" ? "boxplot" : existing?.kind === "distribution" ? "distribution" : "line",
  );
  const [formula, setFormula] = useState(existing?.kind === "line" ? existing.formula : "average");
  const [constant, setConstant] = useState(existing?.constant === null || existing?.constant === undefined ? "0" : String(existing.constant));
  const [scope, setScope] = useState<"table" | "pane" | "cell">(existing?.scope ?? "pane");
  const [label, setLabel] = useState<ReferenceIR["label"]>(existing?.label ?? "automatic");
  const [distribution, setDistribution] = useState<string>(existing?.kind === "distribution" ? existing.formula : "percentile");
  const [values, setValues] = useState<string>(
    existing?.kind === "distribution" ? (existing.formula === "stdev" ? existing.factors : existing.percentiles).join(", ") : "60, 80",
  );
  const [whiskers, setWhiskers] = useState<string>((existing?.whiskers as string | undefined) ?? "standard");
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    const axisUse = axes.find((u) => useKey(u) === axisKey);
    if (!axisUse) {
      setError("請選擇量值的座標軸");
      return;
    }
    const id = existing?.id ?? `refline${Date.now().toString(36)}`;
    const base = { id, axis: axisUse, scope, label, label_text: null, source_ref: EDITOR_REF };
    const numbers = values
      .split(/[,，\s]+/)
      .map(Number)
      .filter((n) => Number.isFinite(n));
    const created: ReferenceIR[] = [];
    if (kind === "line") {
      const num = Number(constant);
      created.push({
        ...base,
        kind: "line",
        formula,
        value: formula === "constant" ? null : axisUse,
        constant: formula === "constant" ? (Number.isFinite(num) ? num : constant) : null,
        percentiles: [],
        factors: [],
      });
    } else if (kind === "band") {
      // 參考區間：最小值到最大值兩條參考線，之間塗上底色
      created.push({ ...base, id: `${id}a`, kind: "line", formula: "min", value: axisUse, constant: null, percentiles: [], factors: [], paired: `${id}b` });
      created.push({ ...base, id: `${id}b`, kind: "line", formula: "max", value: axisUse, constant: null, percentiles: [], factors: [] });
    } else if (kind === "distribution") {
      if (numbers.length === 0) {
        setError("請輸入分布的值，例如 60, 80");
        return;
      }
      created.push({
        ...base,
        kind: "distribution",
        formula: distribution,
        value: axisUse,
        constant: null,
        percentiles: distribution === "percentile" ? numbers.map((n) => Math.max(0, Math.min(100, n))) : [],
        factors: distribution === "stdev" ? numbers : [],
        ...(distribution === "quantiles" ? { tiles: Math.max(2, Math.floor(numbers[0])) } : {}),
      });
    } else {
      created.push({ ...base, kind: "boxplot", formula: "quantiles", value: axisUse, constant: null, percentiles: [], factors: [], whiskers });
    }
    editor.apply((s: SheetIR) => {
      const list = (s.reference_lines as ReferenceIR[] | undefined) ?? [];
      const kept = index === null ? list : list.filter((_, i) => i !== index);
      return { ...s, reference_lines: [...kept, ...created] };
    });
    onClose();
  };
  return (
    <Modal title={existing ? "編輯參考線" : "新增參考線"} onClose={onClose}>
      <div className="reference-dialog">
        <div className="row" role="radiogroup" aria-label="類型">
          {(
            [
              ["line", "線"],
              ["band", "區間"],
              ["distribution", "分布"],
              ["boxplot", "盒鬚圖"],
            ] as const
          ).map(([value, text]) => (
            <label key={value}>
              <input type="radio" name="refkind" checked={kind === value} onChange={() => setKind(value)} /> {text}
            </label>
          ))}
        </div>
        <label className="inline">
          座標軸
          <select aria-label="參考線的座標軸" value={axisKey} onChange={(e) => setAxisKey(e.target.value)}>
            {axes.map((u) => (
              <option key={useKey(u)} value={useKey(u)}>
                {pillLabel(u, fields, sheet.table_calcs)}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          範圍
          <select aria-label="參考線的範圍" value={scope} onChange={(e) => setScope(e.target.value as "table" | "pane" | "cell")}>
            <option value="table">整個表格</option>
            <option value="pane">每個窗格</option>
            <option value="cell">每個儲存格</option>
          </select>
        </label>
        {kind === "line" && (
          <label className="inline">
            值
            <select aria-label="參考線的值" value={formula} onChange={(e) => setFormula(e.target.value)}>
              {REFERENCE_FORMULAS.map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </select>
            {formula === "constant" && <input aria-label="常數" value={constant} onChange={(e) => setConstant(e.target.value)} />}
          </label>
        )}
        {kind === "distribution" && (
          <label className="inline">
            計算
            <select aria-label="分布的計算" value={distribution} onChange={(e) => setDistribution(e.target.value)}>
              <option value="percentile">百分位數</option>
              <option value="stdev">標準差</option>
              <option value="quantiles">分位數</option>
            </select>
            <input aria-label="分布的值" value={values} onChange={(e) => setValues(e.target.value)} />
          </label>
        )}
        {kind === "boxplot" && (
          <label className="inline">
            鬚的範圍
            <select aria-label="鬚的範圍" value={whiskers} onChange={(e) => setWhiskers(e.target.value)}>
              <option value="standard">1.5 倍四分位距內的資料</option>
              <option value="minmax">資料的最小與最大值</option>
            </select>
          </label>
        )}
        {kind !== "boxplot" && (
          <label className="inline">
            標籤
            <select aria-label="參考線的標籤" value={label} onChange={(e) => setLabel(e.target.value as ReferenceIR["label"])}>
              <option value="automatic">自動</option>
              <option value="value">值</option>
              <option value="computation">計算</option>
              <option value="none">無</option>
            </select>
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void save()} disabled={axes.length === 0}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
        {axes.length === 0 && <p className="hint">檢視中沒有連續的量值座標軸。</p>}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 趨勢線

export function TrendDialog({ card, onClose }: { card: Card; onClose: () => void }) {
  const editor = useEditor();
  const trend = cardMarks(editor.sheet, card).trend as Record<string, unknown> | null | undefined;
  const [fit, setFit] = useState<string>((trend?.fit as string | undefined) ?? "linear");
  const [degree, setDegree] = useState<number>((trend?.degree as number | undefined) ?? 3);
  const [byColor, setByColor] = useState(trend?.by_color !== false);
  const [intercept, setIntercept] = useState(trend?.intercept !== false);
  const [bands, setBands] = useState(Boolean(trend?.confidence_bands));
  const save = (remove: boolean) => {
    editor.apply((s: SheetIR) =>
      updateCard(s, card, (m) => ({
        ...m,
        trend: remove
          ? null
          : { ...(trend ?? {}), fit, degree, by_color: byColor, intercept, confidence_bands: bands, tooltips: trend?.tooltips ?? true, excluded: trend?.excluded ?? [], width: trend?.width ?? 1, pattern: trend?.pattern ?? "solid", color: trend?.color ?? null },
      })),
    );
    onClose();
  };
  return (
    <Modal title="趨勢線" onClose={onClose}>
      <div className="trend-dialog">
        <div className="row" role="radiogroup" aria-label="模型">
          {(
            [
              ["linear", "線性"],
              ["log", "對數"],
              ["exp", "指數"],
              ["power", "冪"],
              ["polynomial", "多項式"],
            ] as const
          ).map(([value, text]) => (
            <label key={value}>
              <input type="radio" name="fit" checked={fit === value} onChange={() => setFit(value)} /> {text}
            </label>
          ))}
        </div>
        {fit === "polynomial" && (
          <label className="inline">
            次數
            <input type="number" min={2} max={8} aria-label="多項式的次數" value={degree} onChange={(e) => setDegree(Math.max(2, Math.min(8, Number(e.target.value))))} />
          </label>
        )}
        <label className="inline">
          <input type="checkbox" checked={byColor} onChange={(e) => setByColor(e.target.checked)} /> 依顏色分別計算
        </label>
        <label className="inline">
          <input type="checkbox" checked={bands} onChange={(e) => setBands(e.target.checked)} /> 顯示信賴區間
        </label>
        <label className="inline">
          <input type="checkbox" checked={!intercept} onChange={(e) => setIntercept(!e.target.checked)} /> 通過原點
        </label>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => save(false)}>
            確定
          </button>
          {trend && (
            <button type="button" className="danger" onClick={() => save(true)}>
              移除趨勢線
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

// ---------------------------------------------------------------- 預測

const FORECAST_UNITS: [string, string][] = [
  ["year", "年"],
  ["quarter", "季"],
  ["month", "月"],
  ["week", "週"],
  ["day", "日"],
  ["hour", "小時"],
];
const COMPONENTS: [ForecastIR["trend"] & string, string][] = [
  ["none", "無"],
  ["additive", "加法"],
  ["multiplicative", "乘法"],
];

/** 預測（分析＞預測＞預測選項）：預測的長度、來源資料（忽略最後幾期、以 0 填補缺少的值）、模型與預測區間。
 * 預測欄列架上的量值；時間是檢視中唯一的日期。開啟時資料源加上 Forecast Indicator（可以放到色彩上）。 */
export function ForecastDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const current = sheet.forecast as ForecastIR | null | undefined;
  const measures = axisMeasures(sheet, fields);
  const dates = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks)].filter((u) => u.date);
  const viewUnit = dates[0]?.date?.unit;
  const [length, setLength] = useState<ForecastIR["length"]>(current?.length ?? "auto");
  const [size, setSize] = useState(current?.size ?? 1);
  const [unit, setUnit] = useState(current?.unit ?? (FORECAST_UNITS.some(([u]) => u === viewUnit) ? viewUnit! : "year"));
  const [model, setModel] = useState<ForecastIR["model"]>(current?.model ?? "auto");
  const [trend, setTrend] = useState<NonNullable<ForecastIR["trend"]>>(current?.trend ?? "additive");
  const [season, setSeason] = useState<NonNullable<ForecastIR["season"]>>(current?.season ?? "none");
  const [ignore, setIgnore] = useState(current?.ignore_last ?? 1);
  const [zeros, setZeros] = useState(current?.fill_zeros ?? false);
  const [bands, setBands] = useState(current ? current.bands : true);
  const [confidence, setConfidence] = useState(current?.confidence ?? 0.95);
  const [error, setError] = useState<string | null>(null);
  const save = async (remove: boolean) => {
    if (remove) {
      editor.apply((s: SheetIR) => ({ ...s, forecast: null }));
      onClose();
      return;
    }
    try {
      const indicator = await editApi.addAnalyticsField(editor.workbook, sheet.datasource, "forecast_indicator");
      onSaved();
      const settings: ForecastIR = {
        measures,
        indicator: indicator.id,
        model,
        trend: model === "custom" ? trend : null,
        season: model === "custom" ? season : null,
        length,
        size: length === "auto" ? null : size,
        unit: length === "auto" ? null : unit,
        ignore_last: ignore,
        fill_zeros: zeros,
        confidence,
        bands,
      };
      editor.apply((s: SheetIR) => ({ ...s, forecast: settings }));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const ready = measures.length > 0 && dates.length === 1;
  return (
    <Modal title="預測選項" onClose={onClose}>
      <div className="forecast-dialog">
        <fieldset>
          <legend>預測長度</legend>
          <div className="row" role="radiogroup" aria-label="預測長度">
            {(
              [
                ["auto", "自動"],
                ["next", "正好"],
                ["until", "直到"],
              ] as const
            ).map(([value, text]) => (
              <label key={value}>
                <input type="radio" name="forecast-length" checked={length === value} onChange={() => setLength(value)} /> {text}
              </label>
            ))}
          </div>
          {length !== "auto" && (
            <div className="row">
              <input type="number" min={1} max={1000} aria-label="預測的期數" value={size} onChange={(e) => setSize(Math.max(1, Number(e.target.value) || 1))} />
              <select aria-label="預測的期間單位" value={unit} onChange={(e) => setUnit(e.target.value)}>
                {FORECAST_UNITS.map(([value, text]) => (
                  <option key={value} value={value}>
                    {text}
                  </option>
                ))}
              </select>
            </div>
          )}
        </fieldset>
        <fieldset>
          <legend>來源資料</legend>
          <label className="inline">
            忽略最後
            <input type="number" min={0} max={100} aria-label="忽略最後幾期" value={ignore} onChange={(e) => setIgnore(Math.max(0, Number(e.target.value) || 0))} />
            期
          </label>
          <label className="inline">
            <input type="checkbox" checked={zeros} onChange={(e) => setZeros(e.target.checked)} /> 以 0 填補缺少的值
          </label>
        </fieldset>
        <fieldset>
          <legend>預測模型</legend>
          <select aria-label="預測模型" value={model} onChange={(e) => setModel(e.target.value as ForecastIR["model"])}>
            <option value="auto">自動</option>
            <option value="auto_no_season">自動（不含季節性）</option>
            <option value="custom">自訂</option>
          </select>
          {model === "custom" && (
            <div className="row">
              <label className="inline">
                趨勢
                <select aria-label="趨勢" value={trend} onChange={(e) => setTrend(e.target.value as NonNullable<ForecastIR["trend"]>)}>
                  {COMPONENTS.map(([value, text]) => (
                    <option key={value} value={value}>
                      {text}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                季節
                <select aria-label="季節" value={season} onChange={(e) => setSeason(e.target.value as NonNullable<ForecastIR["season"]>)}>
                  {COMPONENTS.map(([value, text]) => (
                    <option key={value} value={value}>
                      {text}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </fieldset>
        <label className="inline">
          <input type="checkbox" checked={bands} onChange={(e) => setBands(e.target.checked)} /> 顯示預測區間
          <select aria-label="預測區間" value={confidence} disabled={!bands} onChange={(e) => setConfidence(Number(e.target.value))}>
            {[0.9, 0.95, 0.99].map((value) => (
              <option key={value} value={value}>
                {Math.round(value * 100)}%
              </option>
            ))}
          </select>
        </label>
        {!ready && <p className="hint">預測需要欄或列上有量值，檢視中只有一個日期。</p>}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={!ready} onClick={() => void save(false)}>
            確定
          </button>
          {current && (
            <button type="button" className="danger" onClick={() => void save(true)}>
              移除預測
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

// ---------------------------------------------------------------- 叢集

/** 叢集（分析窗格的「叢集」）：依檢視中的量值以 k-means 分組，叢集欄位放在色彩上（與 Tableau 相同）。
 * 叢集數自動時以 Calinski-Harabasz 指標決定。 */
export function ClusterDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const current = sheet.cluster as ClusterIR | null | undefined;
  const candidates = viewMeasures(sheet, fields);
  const [chosen, setChosen] = useState<string[]>(
    (current ? current.measures : candidates).map((u) => useKey(u)),
  );
  const [auto, setAuto] = useState(!current?.count);
  const [count, setCount] = useState(current?.count ?? 3);
  const [error, setError] = useState<string | null>(null);
  const toggle = (key: string, on: boolean) => setChosen((list) => (on ? [...list, key] : list.filter((k) => k !== key)));
  const save = async (remove: boolean) => {
    if (remove && current) {
      editor.apply((s: SheetIR) =>
        updateCard({ ...s, cluster: null }, "all", (m) => (m.color?.field === current.field ? { ...m, color: null } : m)),
      );
      onClose();
      return;
    }
    try {
      let field = current?.field;
      if (!field) {
        field = (await editApi.addAnalyticsField(editor.workbook, sheet.datasource, "cluster")).id;
        onSaved();
      }
      const measures = candidates.filter((u) => chosen.includes(useKey(u)));
      const cluster: ClusterIR = { field, measures, count: auto ? null : count };
      const color: FieldUse = { field, agg: null, date: null, continuous: false, table_calc: null };
      editor.apply((s: SheetIR) => updateCard({ ...s, cluster }, "all", (m) => ({ ...m, color })));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal title="叢集" onClose={onClose}>
      <div className="cluster-dialog">
        <fieldset>
          <legend>變數</legend>
          {candidates.map((use) => (
            <label key={useKey(use)} className="inline">
              <input type="checkbox" checked={chosen.includes(useKey(use))} onChange={(e) => toggle(useKey(use), e.target.checked)} />{" "}
              {pillLabel(use, fields, sheet.table_calcs)}
            </label>
          ))}
          {candidates.length === 0 && <p className="hint">檢視中沒有量值。</p>}
        </fieldset>
        <label className="inline">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> 自動決定叢集數
        </label>
        {!auto && (
          <label className="inline">
            叢集數
            <input type="number" min={2} max={50} aria-label="叢集數" value={count} onChange={(e) => setCount(Math.max(2, Math.min(50, Number(e.target.value) || 2)))} />
          </label>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={chosen.length === 0} onClick={() => void save(false)}>
            確定
          </button>
          {current && (
            <button type="button" className="danger" onClick={() => void save(true)}>
              移除叢集
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

// ---------------------------------------------------------------- 地理角色

/**
 * 欄位的地理角色（資料窗格的「地理角色」）與「編輯位置」：資料源的預設國家、州（檢視中沒有國家、州的
 * 欄位時比對的範圍），與比對不到的值指定的地名。
 */
export function GeoDialog({
  datasource,
  field,
  onClose,
  onSaved,
}: {
  datasource: string;
  field: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const ds = editor.catalog.datasources.find((d) => d.id === datasource);
  const target = ds?.fields.find((f) => f.id === field);
  const [role, setRole] = useState<GeoRole | "">(target?.geo_role ?? "");
  const [country, setCountry] = useState(ds?.geo?.country ?? "");
  const [state, setState] = useState(ds?.geo?.state ?? "");
  const [aliases, setAliases] = useState<GeoAlias[]>(target?.geo_aliases ?? []);
  const [error, setError] = useState<string | null>(null);
  if (!ds || !target) return null;
  const numeric = target.datatype === "integer" || target.datatype === "real";
  const choices = GEO_ROLES.filter(([value]) =>
    value === "latitude" || value === "longitude" ? numeric : target.datatype !== "real",
  );
  const place = role !== "" && role !== "latitude" && role !== "longitude";
  const setAlias = (i: number, change: Partial<GeoAlias>) =>
    setAliases((list) => list.map((a, n) => (n === i ? { ...a, ...change } : a)));
  const save = async () => {
    try {
      const kept = aliases.filter((a) => a.value.trim() && a.location.trim());
      await editApi.saveGeography(editor.workbook, datasource, field, role === "" ? null : role, place ? kept : []);
      if (place) await editApi.saveGeoDefaults(editor.workbook, datasource, country || null, state || null);
      onSaved();
      editor.notify(role === "" ? `已取消「${target.caption}」的地理角色` : `已設定「${target.caption}」的地理角色`);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal title={`地理角色：${target.caption}`} onClose={onClose}>
      <div className="geo-dialog">
        <label>
          地理角色
          <select aria-label="地理角色" value={role} onChange={(e) => setRole(e.target.value as GeoRole | "")}>
            <option value="">（無）</option>
            {choices.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {place && (
          <fieldset>
            <legend>編輯位置</legend>
            <p className="hint">檢視中沒有國家、州的欄位時，地名在這個範圍內比對（與 Tableau 的「編輯位置」相同）。</p>
            <label>
              國家/地區
              <input aria-label="預設國家" value={country} placeholder="例如 台灣、United States" onChange={(e) => setCountry(e.target.value)} />
            </label>
            <label>
              州/省/縣市
              <input aria-label="預設州" value={state} placeholder="（不指定）" onChange={(e) => setState(e.target.value)} />
            </label>
            <div className="hint">比對不到或比對錯誤的值，指定對應的地名：</div>
            <table className="geo-aliases">
              <thead>
                <tr>
                  <th>資料中的值</th>
                  <th>地名</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {aliases.map((alias, i) => (
                  <tr key={i}>
                    <td>
                      <input aria-label="資料中的值" value={alias.value} onChange={(e) => setAlias(i, { value: e.target.value })} />
                    </td>
                    <td>
                      <input aria-label="對應的地名" value={alias.location} onChange={(e) => setAlias(i, { location: e.target.value })} />
                    </td>
                    <td>
                      <button type="button" className="link" title="移除" onClick={() => setAliases((list) => list.filter((_, n) => n !== i))}>
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button type="button" onClick={() => setAliases((list) => [...list, { value: "", location: "" }])}>
              ＋ 指定地名
            </button>
          </fieldset>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void save()}>
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

// ---------------------------------------------------------------- 參數

export function ParameterDialog({ parameter, onClose, onSaved }: { parameter: string | null; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const existing = editor.catalog.parameters.find((p) => p.id === parameter);
  const [caption, setCaption] = useState(existing?.caption ?? "參數 1");
  const [datatype, setDatatype] = useState(existing?.datatype ?? "integer");
  const [allowed, setAllowed] = useState<"all" | "list" | "range">(existing?.allowed.kind ?? "all");
  const [value, setValue] = useState(existing?.value === null || existing?.value === undefined ? "" : String(existing.value));
  const [list, setList] = useState((existing?.allowed.values ?? []).map(String).join("\n"));
  const [min, setMin] = useState(existing?.allowed.min === null || existing?.allowed.min === undefined ? "" : String(existing.allowed.min));
  const [max, setMax] = useState(existing?.allowed.max === null || existing?.allowed.max === undefined ? "" : String(existing.allowed.max));
  const [step, setStep] = useState(existing?.allowed.step === null || existing?.allowed.step === undefined ? "" : String(existing.allowed.step));
  // 動態參數：開啟報表時的值、允許值取自欄位
  const [valueField, setValueField] = useState<string>(existing?.value_field ?? "");
  const [domainField, setDomainField] = useState<string>(existing?.domain_field ?? "");
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const compatible = (fieldType: string) =>
    fieldType === datatype ||
    (["integer", "real"].includes(fieldType) && ["integer", "real"].includes(datatype)) ||
    (["date", "datetime"].includes(fieldType) && ["date", "datetime"].includes(datatype));
  const sources = editor.catalog.datasources.flatMap((d) =>
    d.fields
      .filter((f) => f.kind !== "set" && f.level !== "aggregate" && f.level !== "table_calc" && compatible(f.datatype))
      .map((f) => ({ id: f.id, label: editor.catalog.datasources.length > 1 ? `${d.caption}：${f.caption}` : f.caption })),
  );
  const show = (v: Scalar) => (v === null || v === undefined ? "" : String(v));
  const chooseValueField = async (field: string) => {
    setValueField(field);
    setHint(null);
    if (!field) return;
    try {
      const { values } = await editApi.fieldValues(editor.workbook, field, datatype);
      if (values.length === 1) setValue(show(values[0]));
      else setHint(`這個欄位目前有 ${values.length} 個值：開啟報表時維持目前值（必須是單一值，例如 FIXED 的 LOD 運算式）`);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const chooseDomainField = async (field: string) => {
    setDomainField(field);
    if (!field) return;
    try {
      const { values, more } = await editApi.fieldValues(editor.workbook, field, datatype);
      if (allowed === "list") {
        setList(values.map(show).join("\n"));
        if (values.length > 0 && !values.some((v) => show(v) === value)) setValue(show(values[0]));
        if (more) setHint("值超過 1000 個，只列出前 1000 個");
      } else if (values.length > 0) {
        setMin(show(values[0]));
        setMax(show(values[values.length - 1]));
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const convert = (text: string): Scalar => {
    if (datatype === "integer") return text.trim() === "" ? null : Math.trunc(Number(text));
    if (datatype === "real") return text.trim() === "" ? null : Number(text);
    if (datatype === "boolean") return text === "true" || text === "真";
    return text;
  };
  const save = async () => {
    setError(null);
    const body: Record<string, unknown> = {
      id: existing?.id ?? null,
      caption,
      datatype,
      value: convert(value),
      aliases: existing?.aliases ?? [],
      value_field: valueField || null,
      domain_field: allowed !== "all" && domainField ? domainField : null,
    };
    if (allowed === "list") {
      const values = list
        .split("\n")
        .map((v) => v.trim())
        .filter((v) => v !== "")
        .map(convert);
      body.allowed = { kind: "list", values };
    } else if (allowed === "range") {
      body.allowed = { kind: "range", min: min.trim() ? convert(min) : null, max: max.trim() ? convert(max) : null, step: step.trim() ? Number(step) : null };
    } else body.allowed = { kind: "all" };
    try {
      await editApi.saveParameter(editor.workbook, body);
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const remove = async () => {
    if (!existing) return;
    try {
      await editApi.removeParameter(editor.workbook, existing.id);
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal title={existing ? `編輯參數：${existing.caption}` : "建立參數"} onClose={onClose}>
      <div className="parameter-dialog">
        <label className="inline">
          名稱
          <input aria-label="參數名稱" value={caption} onChange={(e) => setCaption(e.target.value)} />
        </label>
        <label className="inline">
          資料類型
          <select aria-label="參數的資料類型" value={datatype} onChange={(e) => setDatatype(e.target.value)}>
            {Object.entries(TYPES).map(([key, text]) => (
              <option key={key} value={key}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          目前值
          <input aria-label="參數的目前值" value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        <label className="inline">
          開啟報表時的值
          <select aria-label="開啟報表時的值" value={valueField} onChange={(e) => void chooseValueField(e.target.value)}>
            <option value="">目前值</option>
            {sources.map((f) => (
              <option key={f.id} value={f.id}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        <div className="row" role="radiogroup" aria-label="允許的值">
          {(
            [
              ["all", "全部"],
              ["list", "清單"],
              ["range", "範圍"],
            ] as const
          ).map(([key, text]) => (
            <label key={key}>
              <input type="radio" name="allowed" checked={allowed === key} onChange={() => setAllowed(key)} /> {text}
            </label>
          ))}
        </div>
        {allowed !== "all" && (
          <label className="inline">
            開啟報表時取自欄位
            <select aria-label="開啟報表時取自欄位" value={domainField} onChange={(e) => void chooseDomainField(e.target.value)}>
              <option value="">固定的值</option>
              {sources.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {allowed === "list" && (
          <textarea aria-label="允許的值清單" rows={5} className="formula" value={list} placeholder="每行一個值" onChange={(e) => setList(e.target.value)} />
        )}
        {allowed === "range" && (
          <div className="row">
            <label>
              最小值
              <input aria-label="參數的最小值" value={min} onChange={(e) => setMin(e.target.value)} />
            </label>
            <label>
              最大值
              <input aria-label="參數的最大值" value={max} onChange={(e) => setMax(e.target.value)} />
            </label>
            <label>
              間距
              <input aria-label="參數的間距" value={step} onChange={(e) => setStep(e.target.value)} />
            </label>
          </div>
        )}
        {hint && <p className="hint">{hint}</p>}
        {(valueField || (allowed !== "all" && domainField)) && (
          <p className="hint">開啟報表時（伺服器上每次開啟檢視時）以目前的資料重新取值；上面是目前的資料的值。</p>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void save()}>
            確定
          </button>
          {existing && (
            <button type="button" className="danger" onClick={() => void remove()}>
              刪除
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

// ---------------------------------------------------------------- 階層

/** 可以放進階層的欄位：維度（不含集合、彙總與 Table Calculation 的計算欄位）。 */
export function hierarchyCandidates(fields: CatalogField[]): CatalogField[] {
  return fields.filter(
    (f) => f.role === "dimension" && f.kind !== "set" && f.level !== "aggregate" && f.level !== "table_calc",
  );
}

/**
 * 建立或改變階層（Tableau 資料窗格的「階層」）：名稱與由上到下的欄位。加入的欄位從其他階層移除
 * （一個欄位只能在一個階層中）；移除所有欄位即刪除階層。
 */
export function HierarchyDialog({
  datasource,
  name,
  field,
  onClose,
  onSaved,
}: {
  datasource: string;
  name: string | null;
  field?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const ds = editor.catalog.datasources.find((d) => d.id === datasource);
  const existing = ds?.hierarchies?.find((h) => h.name === name) ?? null;
  const candidates = hierarchyCandidates(ds?.fields ?? []);
  const caption = (id: string) => ds?.fields.find((f) => f.id === id)?.caption ?? id;
  const taken = new Set(ds?.hierarchies?.map((h) => h.name) ?? []);
  const suggested = (() => {
    if (existing) return existing.name;
    const base = field ? `${caption(field)} 階層` : "階層";
    let text = base;
    for (let n = 2; taken.has(text); n += 1) text = `${base} ${n}`;
    return text;
  })();
  const [title, setTitle] = useState(suggested);
  const [levels, setLevels] = useState<string[]>(existing?.fields ?? (field ? [field] : []));
  const [adding, setAdding] = useState("");
  const [error, setError] = useState<string | null>(null);
  const available = candidates.filter((f) => !levels.includes(f.id));
  const owner = (id: string) => ds?.hierarchies?.find((h) => h.name !== existing?.name && h.fields.includes(id))?.name;
  const move = (index: number, by: number) => {
    const next = [...levels];
    const [item] = next.splice(index, 1);
    next.splice(index + by, 0, item);
    setLevels(next);
  };
  const save = async () => {
    setError(null);
    try {
      await editApi.saveHierarchy(editor.workbook, datasource, title, levels, existing?.name ?? null);
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const remove = async () => {
    if (!existing) return;
    try {
      await editApi.removeHierarchy(editor.workbook, datasource, existing.name);
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Modal title={existing ? `編輯階層：${existing.name}` : "建立階層"} onClose={onClose}>
      <div className="hierarchy-dialog">
        <label className="inline">
          名稱
          <input aria-label="階層名稱" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <ol className="hierarchy-levels" aria-label="階層的欄位（由上到下）">
          {levels.map((id, i) => (
            <li key={id} data-level={id}>
              <span className="level-name">{caption(id)}</span>
              <button type="button" className="link" aria-label={`上移 ${caption(id)}`} disabled={i === 0} onClick={() => move(i, -1)}>
                ↑
              </button>
              <button
                type="button"
                className="link"
                aria-label={`下移 ${caption(id)}`}
                disabled={i === levels.length - 1}
                onClick={() => move(i, 1)}
              >
                ↓
              </button>
              <button type="button" className="link" aria-label={`從階層移除 ${caption(id)}`} onClick={() => setLevels(levels.filter((l) => l !== id))}>
                ✕
              </button>
            </li>
          ))}
          {levels.length === 0 && <li className="hint">沒有欄位：確定後刪除階層</li>}
        </ol>
        <div className="row">
          <select aria-label="加入階層的欄位" value={adding} onChange={(e) => setAdding(e.target.value)}>
            <option value="">選擇維度…</option>
            {available.map((f) => (
              <option key={f.id} value={f.id}>
                {f.caption}
                {owner(f.id) ? `（目前在「${owner(f.id)}」）` : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={!adding}
            onClick={() => {
              setLevels([...levels, adding]);
              setAdding("");
            }}
          >
            加入
          </button>
        </div>
        <p className="hint">檢視上的階層欄位可以向下切入到下一層、向上收合；一個欄位只能在一個階層中。</p>
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={!existing && levels.length === 0} onClick={() => void save()}>
            確定
          </button>
          {existing && (
            <button type="button" className="danger" onClick={() => void remove()}>
              刪除階層
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

// ---------------------------------------------------------------- 工具提示

/**
 * 工具提示中的工作表（Tableau 工具提示編輯器的「插入＞工作表」）：滑過標記時，工具提示中顯示另一張工作表，
 * 以標記的所有欄位或選定的欄位篩選；設定最大寬、高。
 */
export function TooltipDialog({ card, onClose }: { card: Card; onClose: () => void }) {
  const editor = useEditor();
  const marks = cardMarks(editor.sheet, card);
  const [sheets, setSheets] = useState<TooltipSheetIR[]>(marks.tooltip_sheets ?? []);
  const [outline, setOutline] = useState<Outline | null>(null);
  useEffect(() => {
    editApi
      .outline(editor.workbook)
      .then(setOutline)
      .catch((e: Error) => editor.notify(e.message, "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor.workbook]);
  const targets = (outline?.worksheets ?? []).filter((w) => w.id !== editor.sheet.id);
  // 可以篩選的欄位：檢視上的維度
  const dimensions: FieldUse[] = [];
  for (const use of [...editor.sheet.rows, ...editor.sheet.columns, ...markUses(marks)]) {
    if (isSpecial(use) || isMeasure(use, editor.fields)) continue;
    if (!dimensions.some((d) => useKey(d) === useKey(use))) dimensions.push(use);
  }
  const change = (index: number, update: Partial<TooltipSheetIR>) =>
    setSheets(sheets.map((s, i) => (i === index ? { ...s, ...update } : s)));
  const save = () => {
    editor.apply((s) => updateCard(s, card, (m) => ({ ...m, tooltip_sheets: sheets })));
    onClose();
  };
  return (
    <Modal title="工具提示中的工作表" onClose={onClose}>
      <div className="tooltip-dialog">
        {sheets.length === 0 && <p className="hint">滑過標記時，工具提示中顯示另一張工作表，以標記的值篩選。</p>}
        {sheets.map((sheet, i) => (
          <fieldset key={i} className="tooltip-sheet" data-tooltip-sheet={sheet.worksheet}>
            <div className="row">
              <label className="inline">
                工作表
                <select aria-label="工具提示中的工作表" value={sheet.worksheet} onChange={(e) => change(i, { worksheet: e.target.value })}>
                  {targets.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                最大寬度
                <input
                  type="number"
                  min={50}
                  aria-label="最大寬度"
                  value={sheet.max_width}
                  onChange={(e) => change(i, { max_width: Math.max(1, Number(e.target.value) || 300) })}
                />
              </label>
              <label className="inline">
                最大高度
                <input
                  type="number"
                  min={50}
                  aria-label="最大高度"
                  value={sheet.max_height}
                  onChange={(e) => change(i, { max_height: Math.max(1, Number(e.target.value) || 300) })}
                />
              </label>
              <button type="button" className="link" aria-label="移除工具提示中的工作表" onClick={() => setSheets(sheets.filter((_, k) => k !== i))}>
                ✕
              </button>
            </div>
            <div className="row" role="radiogroup" aria-label="篩選">
              <label>
                <input type="radio" checked={sheet.fields === null} onChange={() => change(i, { fields: null })} /> 所有欄位
              </label>
              <label>
                <input type="radio" checked={sheet.fields !== null} onChange={() => change(i, { fields: [] })} /> 選定的欄位
              </label>
            </div>
            {sheet.fields !== null && (
              <div className="row">
                {dimensions.map((use) => {
                  const key = useKey(use);
                  const checked = (sheet.fields ?? []).some((f) => useKey(f) === key);
                  return (
                    <label key={key}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() =>
                          change(i, {
                            fields: checked
                              ? (sheet.fields ?? []).filter((f) => useKey(f) !== key)
                              : [...(sheet.fields ?? []), use],
                          })
                        }
                      />{" "}
                      {pillLabel(use, editor.fields, editor.sheet.table_calcs)}
                    </label>
                  );
                })}
                {dimensions.length === 0 && <span className="hint">檢視上沒有維度：不篩選</span>}
              </div>
            )}
          </fieldset>
        ))}
        <div className="row">
          <button
            type="button"
            disabled={targets.length === 0}
            onClick={() => {
              const used = new Set(sheets.map((s) => s.worksheet));
              const target = targets.find((w) => !used.has(w.id)) ?? targets[0];
              if (target) setSheets([...sheets, { worksheet: target.id, max_width: 300, max_height: 300, fields: null }]);
            }}
          >
            加入工作表
          </button>
        </div>
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

// ---------------------------------------------------------------- 色彩

export function ColorsDialog({ card, onClose }: { card: Card; onClose: () => void }) {
  const editor = useEditor();
  const marks = cardMarks(editor.sheet, card);
  const colorUse = marks.color as FieldUse | null;
  const continuousColor = colorUse !== null && colorUse.continuous;
  const palette = marks.color_palette as Record<string, unknown> | null | undefined;
  const [markColor, setMarkColor] = useState((marks.mark_color as string | null | undefined) ?? "#4e79a7");
  const [useMarkColor, setUseMarkColor] = useState(Boolean(marks.mark_color));
  const [opacity, setOpacity] = useState(Math.round(((marks.opacity as number | null | undefined) ?? 1) * 100));
  const [stroke, setStroke] = useState((marks.stroke as string | null | undefined) ?? "#000000");
  const [useStroke, setUseStroke] = useState(Boolean(marks.stroke));
  const [paletteName, setPaletteName] = useState((palette?.palette as string | null | undefined) ?? "");
  const [reverse, setReverse] = useState(Boolean(palette?.reverse));
  const [steps, setSteps] = useState<string>(palette?.steps ? String(palette.steps) : "");
  const choices = editor.catalog.palettes.filter((p) => (continuousColor ? p.kind !== "regular" : p.kind === "regular"));
  const save = () => {
    editor.apply((s: SheetIR) =>
      updateCard(s, card, (m) => {
        const next: MarksIR = { ...m, mark_color: useMarkColor ? markColor.toLowerCase() : null, opacity: opacity >= 100 ? null : opacity / 100, stroke: useStroke ? stroke.toLowerCase() : null };
        if (colorUse) {
          if (!paletteName) next.color_palette = null;
          else {
            const chosen = editor.catalog.palettes.find((p) => p.name === paletteName);
            next.color_palette = {
              ...(palette ?? {}),
              kind: continuousColor ? "continuous" : "discrete",
              palette: paletteName,
              assignments: continuousColor ? [] : ((palette?.assignments as unknown[]) ?? []),
              colors: [],
              diverging: chosen?.kind === "diverging",
              reverse,
              steps: continuousColor && steps.trim() ? Math.max(2, Math.floor(Number(steps))) : null,
            };
          }
        }
        return next;
      }),
    );
    onClose();
  };
  return (
    <Modal title="色彩" onClose={onClose}>
      <div className="colors-dialog">
        {colorUse && (
          <>
            <label className="inline">
              色盤（{pillLabel(colorUse, editor.fields, editor.sheet.table_calcs)}）
              <select aria-label="色盤" value={paletteName} onChange={(e) => setPaletteName(e.target.value)}>
                <option value="">自動</option>
                {choices.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            {paletteName && (
              <div className="swatches" aria-hidden="true">
                {(choices.find((p) => p.name === paletteName)?.colors ?? []).map((c, i) => (
                  <span key={`${c}${i}`} style={{ background: c }} />
                ))}
              </div>
            )}
            <label className="inline">
              <input type="checkbox" checked={reverse} onChange={(e) => setReverse(e.target.checked)} /> 反轉
            </label>
            {continuousColor && (
              <label className="inline">
                分段
                <input type="number" min={2} aria-label="分段數" value={steps} placeholder="連續" onChange={(e) => setSteps(e.target.value)} />
              </label>
            )}
          </>
        )}
        {!colorUse && (
          <label className="inline">
            <input type="checkbox" checked={useMarkColor} onChange={(e) => setUseMarkColor(e.target.checked)} /> 標記的顏色
            <input type="color" aria-label="標記的顏色" value={markColor} onChange={(e) => setMarkColor(e.target.value)} disabled={!useMarkColor} />
          </label>
        )}
        <label className="inline">
          不透明度
          <input type="range" min={0} max={100} aria-label="不透明度" value={opacity} onChange={(e) => setOpacity(Number(e.target.value))} />
          {opacity}%
        </label>
        <label className="inline">
          <input type="checkbox" checked={useStroke} onChange={(e) => setUseStroke(e.target.checked)} /> 框線
          <input type="color" aria-label="框線的顏色" value={stroke} onChange={(e) => setStroke(e.target.value)} disabled={!useStroke} />
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


// ---------------------------------------------------------------- 工作表的格式

interface ShelfStyleIR {
  grid: boolean;
  axis: boolean;
  ticks: boolean;
  field_labels: boolean;
  cell: number | null;
  banding: { pane: string | null; header: string | null; size: number; level: number | null } | null;
  divider: unknown;
  header_divider: unknown;
  zero_line: unknown;
  [key: string]: unknown;
}

const FONT_FAMILIES = ["Tableau Book", "Tableau Medium", "Tableau Semibold", "Tableau Bold", "Arial", "Microsoft JhengHei", "Segoe UI"];

export function SheetFormatDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const sheet = editor.sheet;
  const rows = sheet.row_style as ShelfStyleIR;
  const cols = sheet.column_style as ShelfStyleIR;
  const fonts = (sheet.fonts as Record<string, { family?: string | null; size?: number | null; bold?: boolean | null; italic?: boolean | null }> | undefined) ?? {};
  const [background, setBackground] = useState<string | null>((sheet.background as string | null) ?? null);
  const [textColor, setTextColor] = useState<string | null>((sheet.text_color as string | null) ?? null);
  const [family, setFamily] = useState(fonts.worksheet?.family ?? "");
  const [size, setSize] = useState<string>(fonts.worksheet?.size ? String(fonts.worksheet.size) : "");
  const [titleSize, setTitleSize] = useState<string>(fonts.title?.size ? String(fonts.title.size) : "");
  const [rowStyle, setRowStyle] = useState<ShelfStyleIR>(rows);
  const [colStyle, setColStyle] = useState<ShelfStyleIR>(cols);
  const save = () => {
    editor.apply((s: SheetIR) => {
      const nextFonts = { ...((s.fonts as Record<string, unknown>) ?? {}) } as Record<string, Record<string, unknown>>;
      const worksheetFont: Record<string, unknown> = { ...(nextFonts.worksheet ?? {}), family: family || null, size: size ? Number(size) : null };
      if (Object.values(worksheetFont).some((v) => v !== null && v !== undefined)) nextFonts.worksheet = worksheetFont;
      else delete nextFonts.worksheet;
      const title: Record<string, unknown> = { ...(nextFonts.title ?? {}), size: titleSize ? Number(titleSize) : null };
      if (Object.values(title).some((v) => v !== null && v !== undefined)) nextFonts.title = title;
      else delete nextFonts.title;
      return { ...s, background, text_color: textColor, fonts: nextFonts, row_style: rowStyle, column_style: colStyle };
    });
    onClose();
  };
  const shelf = (label: string, value: ShelfStyleIR, set: (v: ShelfStyleIR) => void) => (
    <fieldset className="prop-group">
      <legend>{label}</legend>
      <div className="row">
        <label>
          <input type="checkbox" checked={value.grid} onChange={(e) => set({ ...value, grid: e.target.checked })} /> 格線
        </label>
        <label>
          <input type="checkbox" checked={value.axis} onChange={(e) => set({ ...value, axis: e.target.checked })} /> 座標軸線
        </label>
        <label>
          <input type="checkbox" checked={value.ticks} onChange={(e) => set({ ...value, ticks: e.target.checked })} /> 刻度
        </label>
        <label>
          <input type="checkbox" checked={value.field_labels} onChange={(e) => set({ ...value, field_labels: e.target.checked })} /> 欄位標籤
        </label>
        <label>
          <input type="checkbox" checked={value.zero_line !== null} onChange={(e) => set({ ...value, zero_line: e.target.checked ? {} : null })} /> 零線
        </label>
        <label>
          <input
            type="checkbox"
            checked={value.divider !== null}
            onChange={(e) => set({ ...value, divider: e.target.checked ? {} : null, header_divider: e.target.checked ? {} : null })}
          />{" "}
          分隔線
        </label>
      </div>
      <div className="row">
        <label>
          <input
            type="checkbox"
            checked={value.banding !== null}
            onChange={(e) => set({ ...value, banding: e.target.checked ? { pane: "#f4f4f4", header: "#f4f4f4", size: 1, level: null } : null })}
          />{" "}
          色帶
        </label>
        {value.banding && (
          <>
            <input
              type="color"
              aria-label={`${label}色帶的顏色`}
              value={(value.banding.pane ?? "#f4f4f4").slice(0, 7)}
              onChange={(e) => set({ ...value, banding: { ...value.banding!, pane: e.target.value, header: e.target.value } })}
            />
            <label>
              每
              <input
                type="number"
                min={1}
                aria-label={`${label}色帶的大小`}
                value={value.banding.size}
                onChange={(e) => set({ ...value, banding: { ...value.banding!, size: Math.max(1, Number(e.target.value)) } })}
              />
              列一段
            </label>
          </>
        )}
      </div>
    </fieldset>
  );
  return (
    <Modal title="格式：工作表" onClose={onClose} wide>
      <div className="format-dialog">
        <div className="row">
          <label>
            <input type="checkbox" checked={background !== null} onChange={(e) => setBackground(e.target.checked ? "#ffffff" : null)} /> 背景
          </label>
          <input type="color" aria-label="工作表的背景" disabled={background === null} value={(background ?? "#ffffff").slice(0, 7)} onChange={(e) => setBackground(e.target.value)} />
          <label>
            <input type="checkbox" checked={textColor !== null} onChange={(e) => setTextColor(e.target.checked ? "#333333" : null)} /> 文字顏色
          </label>
          <input type="color" aria-label="工作表的文字顏色" disabled={textColor === null} value={(textColor ?? "#333333").slice(0, 7)} onChange={(e) => setTextColor(e.target.value)} />
        </div>
        <div className="row">
          <label>
            字型
            <select aria-label="工作表的字型" value={family} onChange={(e) => setFamily(e.target.value)}>
              <option value="">預設</option>
              {FONT_FAMILIES.map((f) => (
                <option key={f}>{f}</option>
              ))}
            </select>
          </label>
          <label>
            字級
            <input type="number" min={6} max={72} aria-label="工作表的字級" placeholder="9" value={size} onChange={(e) => setSize(e.target.value)} />
          </label>
          <label>
            標題字級
            <input type="number" min={6} max={72} aria-label="標題的字級" placeholder="15" value={titleSize} onChange={(e) => setTitleSize(e.target.value)} />
          </label>
        </div>
        {shelf("列", rowStyle, setRowStyle)}
        {shelf("欄", colStyle, setColStyle)}
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
