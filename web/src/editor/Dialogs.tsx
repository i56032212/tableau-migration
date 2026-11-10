// 編輯器的對話框：計算欄位、計算方向（指定維度）、數字格式、標題、座標軸、參考線、趨勢線、參數與色彩。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi, type FormulaCheck } from "./api";
import { useEditor } from "./context";
import {
  type Card,
  cardMarks,
  type CatalogField,
  EDITOR_REF,
  isMeasure,
  isSpecial,
  type MarksIR,
  markUses,
  pillLabel,
  sameUse,
  type SheetIR,
  updateCard,
  useKey,
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

export function CalcDialog({ field, onClose, onSaved }: { field: CatalogField | null; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const ds = editor.sheet.datasource;
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
  const fields = [...editor.fields.values()].filter((f) => !f.hidden && f.id !== field?.id);
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
  const [error, setError] = useState<string | null>(null);
  const convert = (text: string): Scalar => {
    if (datatype === "integer") return text.trim() === "" ? null : Math.trunc(Number(text));
    if (datatype === "real") return text.trim() === "" ? null : Number(text);
    if (datatype === "boolean") return text === "true" || text === "真";
    return text;
  };
  const save = async () => {
    setError(null);
    const body: Record<string, unknown> = { id: existing?.id ?? null, caption, datatype, value: convert(value), aliases: existing?.aliases ?? [] };
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

