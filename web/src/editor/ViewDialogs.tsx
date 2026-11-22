// 檢視層級的對話框與操作：總計（位置、總計使用）、複製與貼上格式。

import { useEffect, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi } from "./api";
import { useEditor } from "./context";
import { allUses, type Card, cardMarks, isMeasure, pillLabel, type SheetIR, updateCard, useKey } from "./model";
import { textTokens } from "./MoreDialogs";
import { type RichRun, RichTextEditor } from "./RichText";
import { type ShapeChoice, ShapePalettes, ShapeSelect, SHAPES, useShapePalettes } from "./ShapePicker";

type TotalAgg = "sum" | "avg" | "min" | "max";
interface VisualTotalIR {
  use: FieldUse;
  agg: TotalAgg;
}

const TOTAL_AGGS: [TotalAgg | "", string][] = [
  ["", "自動"],
  ["sum", "總和"],
  ["avg", "平均值"],
  ["min", "最小值"],
  ["max", "最大值"],
];

/**
 * 總計（Tableau 的「分析＞總計」）：顯示欄總計與列總計、位置（欄總計移至頂端、列總計移至左側）、依量值著色時
 * 總計是否上色，以及各量值的「總計使用」（自動＝在較粗的詳細程度重新彙總；其餘為畫面上各標記的值再彙總）。
 */
export function TotalsDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields } = editor;
  const [totals, setTotals] = useState(new Set(sheet.totals));
  const [top, setTop] = useState(sheet.totals_top === true);
  const [left, setLeft] = useState(sheet.totals_left === true);
  const [colored, setColored] = useState(sheet.totals_colored === true);
  const measures: FieldUse[] = [];
  const seen = new Set<string>();
  for (const use of [...allUses(sheet), ...sheet.measure_values]) {
    const key = useKey(use);
    if (seen.has(key) || !isMeasure(use, fields)) continue;
    seen.add(key);
    measures.push(use);
  }
  const [using, setUsing] = useState<Record<string, TotalAgg | "">>(() =>
    Object.fromEntries(((sheet.visual_totals as VisualTotalIR[] | undefined) ?? []).map((v) => [useKey(v.use), v.agg])),
  );
  const toggle = (which: "rows" | "columns") =>
    setTotals((current) => {
      const next = new Set(current);
      if (next.has(which)) next.delete(which);
      else next.add(which);
      return next;
    });
  const save = () => {
    const visual: VisualTotalIR[] = measures.flatMap((use) => {
      const agg = using[useKey(use)];
      return agg ? [{ use, agg }] : [];
    });
    editor.apply((s: SheetIR) => ({
      ...s,
      totals: (["rows", "columns"] as const).filter((t) => totals.has(t)),
      totals_top: top,
      totals_left: left,
      totals_colored: colored,
      visual_totals: visual,
    }));
    onClose();
  };
  return (
    <Modal title="總計" onClose={onClose}>
      <div className="action-form totals-dialog">
        <label className="inline" title="每一欄的合計（總計列）">
          <input type="checkbox" checked={totals.has("rows")} onChange={() => toggle("rows")} /> 顯示欄總計
        </label>
        <label className="inline" title="每一列的合計（總計欄）">
          <input type="checkbox" checked={totals.has("columns")} onChange={() => toggle("columns")} /> 顯示列總計
        </label>
        <label className="inline" title="總計列與小計放在資料的上方（預設在下方）">
          <input type="checkbox" checked={top} onChange={(e) => setTop(e.target.checked)} /> 欄總計移至頂端
        </label>
        <label className="inline" title="總計欄與小計放在資料的左側（預設在右側）">
          <input type="checkbox" checked={left} onChange={(e) => setLeft(e.target.checked)} /> 列總計移至左側
        </label>
        <label className="inline" title="依量值著色時總計也上色、納入色階的範圍">
          <input type="checkbox" checked={colored} onChange={(e) => setColored(e.target.checked)} /> 色彩包含總計
        </label>
        {measures.length > 0 && (
          <fieldset>
            <legend>總計使用</legend>
            {measures.map((use) => {
              const key = useKey(use);
              const label = pillLabel(use, fields, sheet.table_calcs);
              return (
                <label className="inline" key={key}>
                  {label}
                  <select
                    aria-label={`${label}的總計使用`}
                    value={using[key] ?? ""}
                    onChange={(e) => setUsing((current) => ({ ...current, [key]: e.target.value as TotalAgg | "" }))}
                  >
                    {TOTAL_AGGS.map(([value, text]) => (
                      <option key={value} value={value}>
                        {text}
                      </option>
                    ))}
                  </select>
                </label>
              );
            })}
            <p className="hint">自動：在總計的詳細程度重新彙總底層資料；其餘：畫面上各標記的值再計算。</p>
          </fieldset>
        )}
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

// ---------------------------------------------------------------- 說明

/** 說明（Tableau 的「工作表＞顯示說明」）：檢視下方的文字；留空白時是自動產生的說明（檢視的內容）。 */
export function CaptionDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const [shown, setShown] = useState(editor.sheet.caption_shown === true);
  const [runs, setRuns] = useState<RichRun[]>((editor.sheet.caption as RichRun[] | undefined) ?? []);
  // 說明中可以插入參數的值（欄位的值不行）
  const tokens = textTokens(editor.sheet, editor.fields, editor.catalog, { uses: false });
  const save = () => {
    const blank = runs.every((r) => r.text != null && !r.text.trim());
    editor.apply((s: SheetIR) => ({ ...s, caption_shown: shown, caption: blank ? [] : runs }));
    onClose();
  };
  return (
    <Modal title="說明" onClose={onClose}>
      <div className="action-form caption-dialog">
        <label className="inline">
          <input type="checkbox" checked={shown} onChange={(e) => setShown(e.target.checked)} /> 顯示說明
        </label>
        <RichTextEditor label="說明的文字" value={runs} tokens={tokens} onChange={setRuns} rows={3} />
        <p className="hint">留空白時是自動產生的說明（欄列架、標記卡與篩選的欄位）；「插入」可以放進參數的值。</p>
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

// ---------------------------------------------------------------- 形狀

type Assigned = Record<string, { value: Scalar } & ShapeChoice>;

/**
 * 形狀（Tableau 標記卡「形狀」的「編輯形狀」）：形狀上有欄位時為各成員指定形狀（「自動」依預設調色盤的
 * 順序）；沒有欄位時是所有標記的形狀。形狀可以是預設調色盤的形狀，或自訂調色盤中的圖片。
 */
export function ShapesDialog({ card, onClose }: { card: Card; onClose: () => void }) {
  const editor = useEditor();
  const marks = cardMarks(editor.sheet, card);
  const use = marks.shape as FieldUse | null;
  const [members, setMembers] = useState<Scalar[]>([]);
  const [assigned, setAssigned] = useState<Assigned>(() =>
    Object.fromEntries((marks.shape_assignments ?? []).map((a) => [JSON.stringify(a.value), a])),
  );
  const [fixed, setFixed] = useState<ShapeChoice | null>(marks.mark_shape ?? null);
  const { palettes, setPalettes } = useShapePalettes(editor.workbook);
  const key = use ? useKey(use) : "";
  useEffect(() => {
    if (!use) return;
    let cancelled = false;
    editApi
      .domain(editor.workbook, use, { kind: "values", worksheet: editor.sheet.id })
      .then((domain) => {
        if (!cancelled && domain.kind === "values") setMembers(domain.values.slice(0, 200));
      })
      .catch(() => setMembers([]));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const save = () => {
    editor.apply((s: SheetIR) =>
      updateCard(s, card, (m) => (use ? { ...m, shape_assignments: Object.values(assigned) } : { ...m, mark_shape: fixed })),
    );
    onClose();
  };
  const reset = () => {
    setAssigned({});
    setFixed(null);
  };
  /** 指定調色盤：調色盤的圖片依序指定給各成員（成員比圖片多時從頭再用） */
  const assignPalette = (images: string[]) => {
    if (images.length === 0) return;
    setAssigned(Object.fromEntries(members.map((value, i) => [JSON.stringify(value), { value, shape: "circle", image: images[i % images.length] }])));
  };
  return (
    <Modal title="形狀" onClose={onClose}>
      <div className="action-form shapes-dialog">
        {use && members.length === 0 && <p className="hint">沒有可以指定形狀的成員。</p>}
        {use &&
          members.map((value, i) => {
            const id = JSON.stringify(value);
            const label = value === null ? "Null" : String(value);
            return (
              <ShapeSelect
                key={id}
                workbook={editor.workbook}
                label={label}
                ariaLabel={`${label}的形狀`}
                value={assigned[id]}
                auto={`自動（${SHAPES[i % SHAPES.length][1]}）`}
                palettes={palettes}
                onChange={(choice) =>
                  setAssigned((current) => {
                    const next = { ...current };
                    if (choice) next[id] = { value, ...choice };
                    else delete next[id];
                    return next;
                  })
                }
              />
            );
          })}
        {!use && (
          <ShapeSelect
            workbook={editor.workbook}
            label="所有標記"
            ariaLabel="所有標記的形狀"
            value={fixed}
            auto={`預設（${SHAPES[0][1]}）`}
            palettes={palettes}
            onChange={setFixed}
          />
        )}
        <ShapePalettes workbook={editor.workbook} palettes={palettes} onPalettes={setPalettes} onAssign={use ? assignPalette : undefined} />
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save}>
            確定
          </button>
          <button type="button" title="回到預設的形狀（Tableau 的「重設」）" onClick={reset}>
            重設
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 複製、貼上格式

/** 工作表層級的格式（Tableau 的「格式」窗格）：不含欄位、篩選、排序等內容。 */
const FORMAT_KEYS = [
  "row_style",
  "column_style",
  "background",
  "text_color",
  "cell_color",
  "cell_align",
  "label_valign",
  "fonts",
  "field_label_background",
  "title_background",
  "null_text",
  "null_marks",
  "null_indicator",
  "cell_border",
  "pane_border",
  "header_border",
  "total_borders",
] as const;
const FORMAT_STORE = "tmig.copied-format";
let copiedFormat: Record<string, unknown> | null = null;

function storedFormat(): Record<string, unknown> | null {
  if (copiedFormat) return copiedFormat;
  try {
    const text = window.sessionStorage.getItem(FORMAT_STORE);
    copiedFormat = text ? (JSON.parse(text) as Record<string, unknown>) : null;
  } catch {
    copiedFormat = null;
  }
  return copiedFormat;
}

/**
 * 把一張工作表的格式套用到報表中其他所有的工作表（Tableau 的「格式＞活頁簿」：整份報表一致的字型、顏色與線條）；
 * 回傳套用的工作表數。
 */
export async function applyFormatToAll(workbook: string, source: SheetIR): Promise<number> {
  const format = Object.fromEntries(FORMAT_KEYS.filter((key) => key in source).map((key) => [key, source[key]]));
  const outline = await editApi.outline(workbook);
  let count = 0;
  for (const other of outline.worksheets) {
    if (other.id === source.id) continue;
    const worksheet = await editApi.worksheet(workbook, other.id);
    await editApi.saveWorksheet(workbook, { ...worksheet, ...structuredClone(format) });
    count += 1;
  }
  return count;
}

/** 複製格式：記下這張工作表的格式設定（同一個視窗中可以貼到其他工作表）。 */
export function copyFormat(sheet: SheetIR): void {
  copiedFormat = Object.fromEntries(FORMAT_KEYS.filter((key) => key in sheet).map((key) => [key, sheet[key]]));
  try {
    window.sessionStorage.setItem(FORMAT_STORE, JSON.stringify(copiedFormat));
  } catch {
    // 不能用 sessionStorage 時只記在記憶體中
  }
}

export function hasCopiedFormat(): boolean {
  return storedFormat() !== null;
}

/**
 * 清除格式（Tableau 的「工作表＞清除＞格式」）：格式設定與工作表上的數字格式都回到預設（存檔時由引擎補上預設值）；
 * 欄位、篩選、排序與標記卡的內容不變。
 */
export function clearFormat(sheet: SheetIR): SheetIR {
  const cleared: SheetIR = { ...sheet, formats: {} };
  for (const key of FORMAT_KEYS) delete cleared[key];
  return cleared;
}

/** 貼上格式：以複製的格式取代這張工作表的格式設定；沒有複製過時不變。 */
export function pasteFormat(sheet: SheetIR): SheetIR {
  const format = storedFormat();
  return format ? { ...sheet, ...structuredClone(format) } : sheet;
}
