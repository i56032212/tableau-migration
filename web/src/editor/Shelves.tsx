// 欄列架、篩選架、Measure Values 卡與標記卡：膠囊的拖放與膠囊選單（彙總、日期、Table Calculation、排序…）。

import type React from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { FieldUse } from "../types";
import { editApi } from "./api";
import { currentDrag, endDrag, startDrag, useEditor, type DragItem } from "./context";
import {
  AGGREGATIONS,
  DATE_PARTS,
  DATE_TRUNCS,
  DIRECTIONS,
  type Card,
  cardMarks,
  changeUse,
  type CatalogField,
  defaultUse,
  drillPill,
  drillToggle,
  firstMeasure,
  type FilterIR,
  GENERATED_LABELS,
  GEOMETRY_GENERATED,
  generatedUse,
  insertUse,
  isDate,
  isMeasure,
  isNumeric,
  isSpecial,
  MARK_TYPES,
  type MarkSlot,
  MEASURE_NAMES,
  MEASURE_VALUES,
  newTableCalc,
  pillFormula,
  pillLabel,
  plainFormulaUse,
  QUICK_CALCS,
  removeUse,
  sameUse,
  setSort,
  setUses,
  type ShelfName,
  type SheetIR,
  type TableCalcIR,
  updateCard,
  usesOn,
} from "./model";
import { dateCalc, dateLevels } from "./MoreDialogs";

// ---------------------------------------------------------------- 選單

export interface MenuItem {
  label: string;
  checked?: boolean;
  disabled?: boolean;
  action?: () => void;
  children?: MenuItem[];
}

export const SEPARATOR: MenuItem = { label: "-" };

function MenuList({ items, onDone }: { items: MenuItem[]; onDone: () => void }) {
  return (
    <ul className="menu" role="menu">
      {items.map((item, i) =>
        item.label === "-" ? (
          <li key={`sep${i}`} className="menu-sep" role="separator" />
        ) : (
          <li
            key={`${item.label}${i}`}
            role="menuitem"
            aria-disabled={item.disabled ? "true" : undefined}
            aria-checked={item.checked === undefined ? undefined : item.checked}
            className={`${item.children ? "has-sub" : ""}${item.disabled ? " disabled" : ""}`}
            tabIndex={-1}
            onClick={(e) => {
              e.stopPropagation();
              if (item.disabled || item.children) return;
              onDone();
              item.action?.();
            }}
          >
            <span className="menu-check">{item.checked ? "✓" : ""}</span>
            <span className="menu-label">{item.label}</span>
            {item.children && <span className="menu-arrow">▸</span>}
            {item.children && <MenuList items={item.children} onDone={onDone} />}
          </li>
        ),
      )}
    </ul>
  );
}

/** 浮動的選單：點選外面或按 Esc 關閉。 */
export function PopupMenu({ anchor, items, onClose }: { anchor: DOMRect; items: MenuItem[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: anchor.left, top: anchor.bottom + 2 });
  useLayoutEffect(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const left = Math.max(4, Math.min(anchor.left, window.innerWidth - box.width - 8));
    const top = anchor.bottom + box.height + 4 > window.innerHeight ? Math.max(4, anchor.top - box.height - 2) : anchor.bottom + 2;
    setPosition({ left, top });
  }, [anchor]);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", key);
    };
  }, [onClose]);
  return (
    <div className="popup-menu" ref={ref} style={{ left: position.left, top: position.top }}>
      <MenuList items={items} onDone={onClose} />
    </div>
  );
}

// ---------------------------------------------------------------- 膠囊

function pillClass(use: FieldUse, fields: Map<string, CatalogField>): string {
  if (use.field === MEASURE_NAMES) return "pill discrete";
  if (use.field === MEASURE_VALUES) return "pill continuous";
  if (use.field in GENERATED_LABELS) return "pill continuous measure";
  const measure = isMeasure(use, fields);
  return `pill ${use.continuous ? "continuous" : "discrete"}${measure ? " measure" : ""}`;
}

function specialLabel(use: FieldUse): string | null {
  if (use.field === MEASURE_NAMES) return "Measure Names";
  if (use.field === MEASURE_VALUES) return "Measure Values";
  return GENERATED_LABELS[use.field] ?? null;
}

export function Pill({
  use,
  shelf,
  index,
  card,
  label,
  menu,
  className,
}: {
  use: FieldUse;
  shelf: ShelfName;
  index: number;
  card: Card;
  label?: string;
  menu: () => MenuItem[];
  className?: string;
}) {
  const { fields, sheet } = useEditor();
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const text = label ?? specialLabel(use) ?? pillLabel(use, fields, sheet.table_calcs);
  return (
    <>
      <button
        type="button"
        className={className ?? pillClass(use, fields)}
        draggable
        data-pill={text}
        onDragStart={(e) => startDrag(e, { kind: "pill", shelf, index, card })}
        onDragEnd={endDrag}
        onClick={(e) => setAnchor((e.currentTarget as HTMLElement).getBoundingClientRect())}
        title={text}
      >
        <span className="pill-text">{text}</span>
        <span className="pill-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {anchor && <PopupMenu anchor={anchor} items={menu()} onClose={() => setAnchor(null)} />}
    </>
  );
}

/** 欄、列架上階層膠囊的切入按鈕（Tableau 膠囊上的 ⊞／⊟）：向下切入加入下一層，收合移除後面的下層。 */
function DrillButton({ shelf, index }: { shelf: "rows" | "columns"; index: number }) {
  const editor = useEditor();
  const toggle = drillToggle(editor.sheet, shelf, index, editor.catalog, editor.fields);
  if (!toggle) return null;
  const label = pillLabel(toggle.next, editor.fields, editor.sheet.table_calcs);
  const text = toggle.kind === "down" ? `向下切入到 ${label}` : `收合 ${label}`;
  return (
    <button
      type="button"
      className="pill-drill"
      title={text}
      aria-label={text}
      onClick={() => editor.apply((sheet) => drillPill(sheet, shelf, index, editor.catalog, editor.fields))}
    >
      {toggle.kind === "down" ? "⊞" : "⊟"}
    </button>
  );
}

// ---------------------------------------------------------------- 拖放

/** 放下：資料窗格的欄位（加上預設的彙總、日期）或架上的膠囊（移動；按住 Ctrl 為複製）。 */
export function useDropTarget(
  shelf: ShelfName,
  card: Card,
  onFilter?: (use: FieldUse) => void,
): {
  over: boolean;
  handlers: Pick<React.HTMLAttributes<HTMLElement>, "onDragOver" | "onDragEnter" | "onDragLeave" | "onDrop">;
  dropAt: (event: React.DragEvent, index: number) => void;
} {
  const editor = useEditor();
  const [over, setOver] = useState(false);
  const accept = (item: DragItem | null) => {
    if (!item) return false;
    if (shelf === "measure_values") {
      // Measure Values 卡只放量值
      const use = item.kind === "field" ? fieldUse(item.field) : usesOn(editor.sheet, item.shelf, item.card)[item.index];
      return use !== undefined && use !== null && !isSpecial(use) && isMeasure(use, editor.fields);
    }
    return true;
  };
  const fieldUse = (id: string): FieldUse | null => {
    if (id === MEASURE_NAMES || id === MEASURE_VALUES)
      return { field: id, agg: null, date: null, continuous: id === MEASURE_VALUES, table_calc: null };
    if (id in GENERATED_LABELS) return generatedUse(id);
    const field = editor.fields.get(id);
    return field ? defaultUse(field) : null;
  };
  const dropAt = (event: React.DragEvent, index: number) => {
    event.preventDefault();
    event.stopPropagation();
    setOver(false);
    const item = currentDrag();
    endDrag();
    if (!accept(item) || !item) return;
    const copy = event.ctrlKey || event.metaKey || event.altKey;
    if (item.kind === "field") {
      const use = fieldUse(item.field);
      if (!use) return;
      if (shelf === "filters") {
        onFilter?.(use);
        return;
      }
      editor.apply(async (sheet) => {
        let updated = sheet;
        if (item.field === MEASURE_VALUES && updated.measure_values.length === 0) {
          // 與 Tableau 相同：Measure Values 卡放入資料源中所有的量值
          const measures = [...editor.fields.values()]
            .filter((f) => !f.hidden && f.role === "measure" && f.level !== "table_calc" && (isNumeric(f) || f.level === "aggregate"))
            .map((f) => defaultUse(f));
          updated = { ...updated, measure_values: measures };
        }
        const [prepared, ready] = await editor.prepare(updated, use);
        return insertUse(prepared, shelf, ready, index, card);
      });
      return;
    }
    const source = usesOn(editor.sheet, item.shelf, item.card)[item.index];
    if (!source) return;
    if (shelf === "filters") {
      if (item.shelf !== "filters") onFilter?.(source);
      return;
    }
    editor.apply((sheet) => {
      if (item.shelf === shelf && item.card === card) {
        // 同一架上移動位置
        const uses = [...usesOn(sheet, shelf, card)];
        const [moved] = uses.splice(item.index, 1);
        uses.splice(index > item.index ? index - 1 : index, 0, moved);
        return setUses(sheet, shelf, uses, card);
      }
      const removed = copy || item.shelf === "filters" ? sheet : removeUse(sheet, item.shelf, item.index, item.card);
      return insertUse(removed, shelf, source, index, card);
    });
  };
  return {
    over,
    dropAt,
    handlers: {
      onDragOver: (e) => {
        if (!accept(currentDrag())) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = e.ctrlKey || e.metaKey ? "copy" : "move";
      },
      onDragEnter: (e) => {
        if (!accept(currentDrag())) return;
        e.preventDefault();
        setOver(true);
      },
      onDragLeave: (e) => {
        if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) setOver(false);
      },
      onDrop: (e) => dropAt(e, Number.MAX_SAFE_INTEGER),
    },
  };
}

// ---------------------------------------------------------------- 膠囊選單

/** 架上膠囊的選單項目。 */
export function usePillMenu() {
  const editor = useEditor();
  const { fields, apply } = editor;

  return (shelf: ShelfName, index: number, card: Card): MenuItem[] => {
    const sheet = editor.sheet;
    const use = usesOn(sheet, shelf, card)[index];
    if (!use) return [];
    const remove: MenuItem = { label: "移除", action: () => apply((s) => removeUse(s, shelf, index, card)) };
    if (isSpecial(use)) return [remove];
    const field = fields.get(use.field);
    const measure = isMeasure(use, fields);
    const change = (next: Partial<FieldUse>) => apply((s) => changeUse(s, shelf, index, { ...use, ...next }, card));
    const items: MenuItem[] = [];
    const onAxis = shelf === "rows" || shelf === "columns";

    items.push({ label: "篩選…", action: () => editor.openFilter({ use, index: null }) });
    if (!measure && field) {
      const by = firstMeasure(sheet, fields);
      const current = sheet.sorts.find((s) => s.field === use.field);
      const sort = (kind: "alphabetic" | "by_field", order: "asc" | "desc") =>
        apply((s) => setSort(s, use.field, { kind, order, by: kind === "by_field" ? by : null, manual_values: [] }));
      items.push({
        label: "排序",
        children: [
          { label: "遞增（字母）", checked: current?.kind === "alphabetic" && current.order === "asc", action: () => sort("alphabetic", "asc") },
          { label: "遞減（字母）", checked: current?.kind === "alphabetic" && current.order === "desc", action: () => sort("alphabetic", "desc") },
          {
            label: by ? `依 ${pillLabel(by, fields, sheet.table_calcs)} 遞增` : "依量值遞增",
            disabled: !by,
            checked: current?.kind === "by_field" && current.order === "asc",
            action: () => sort("by_field", "asc"),
          },
          {
            label: by ? `依 ${pillLabel(by, fields, sheet.table_calcs)} 遞減` : "依量值遞減",
            disabled: !by,
            checked: current?.kind === "by_field" && current.order === "desc",
            action: () => sort("by_field", "desc"),
          },
          { label: "清除排序", disabled: !current, action: () => apply((s) => setSort(s, use.field, null)) },
          // 資料來源順序、依任一欄位與彙總、手動排列成員
          { label: "排序…", checked: current?.kind === "manual" || current?.kind === "data_source", action: () => editor.openDialog({ kind: "sort", use }) },
        ],
      });
    }
    items.push(SEPARATOR);

    if (isDate(field) && !measure) {
      // 日期：上半為日期部分（離散），下半為日期截斷（連續）；精確日期
      for (const [unit, label] of DATE_PARTS)
        items.push({
          label: `${label}`,
          checked: use.date?.kind === "part" && use.date.unit === unit,
          action: () => change({ date: { kind: "part", unit }, continuous: false }),
        });
      items.push(SEPARATOR);
      for (const [unit, label] of DATE_TRUNCS)
        items.push({
          label: `${label}（連續）`,
          checked: use.date?.kind === "trunc" && use.date.unit === unit,
          action: () => change({ date: { kind: "trunc", unit }, continuous: true }),
        });
      items.push({
        label: "精確日期",
        checked: use.date === null,
        action: () => change({ date: null, continuous: use.continuous }),
      });
      items.push(SEPARATOR);
    }

    if (measure && use.agg !== null) {
      items.push({
        label: "量值",
        children: AGGREGATIONS.map(([agg, label]) => ({
          label,
          checked: use.agg === agg,
          action: () => change({ agg }),
        })),
      });
    }
    if (!measure && field && field.kind !== "set") {
      // 維度轉成量值（Tableau 維度膠囊選單的「量值」）
      const aggs = isNumeric(field) ? AGGREGATIONS : AGGREGATIONS.filter(([a]) => ["count", "countd", "min", "max", "attr"].includes(a));
      items.push({
        label: "量值",
        children: aggs.map(([agg, label]) => ({
          label,
          action: () => change({ agg, date: null, continuous: agg !== "attr" }),
        })),
      });
    }
    if (measure && use.agg !== null && field && field.level === null && field.kind === "column") {
      items.push({ label: "維度", action: () => change({ agg: null, continuous: false, table_calc: null }) });
    }
    if (isNumeric(field) || isDate(field) || measure) {
      items.push({ label: "離散", checked: !use.continuous, action: () => change({ continuous: false }) });
      items.push({ label: "連續", checked: use.continuous, action: () => change({ continuous: true }) });
    }

    if (measure) {
      items.push(SEPARATOR);
      const tc = sheet.table_calcs.find((t) => t.id === use.table_calc);
      // 換上新的快速表計算（原本的快速表計算移除；Table Calculation 計算欄位的保留）
      const quick = (type: string, make: (id: string) => TableCalcIR) =>
        apply(async (s) => {
          const id = await editor.newId("tc", `${type} ${field?.caption ?? use.field}`);
          const others = s.table_calcs.filter((t) => t.id !== use.table_calc || t.type === "formula");
          const updated = { ...s, table_calcs: [...others, make(id)] };
          return changeUse(updated, shelf, index, { ...use, table_calc: id }, card);
        });
      // 以日期層級計算的快速表計算：檢視中要有年；年初至今另外要有比年細的日期層級
      const levels = dateLevels(sheet, fields);
      const compounded = tc?.options.compounded === true;
      items.push({
        label: "快速表計算",
        children: [
          ...QUICK_CALCS.map(([type, label, options]) => ({
            label,
            checked: tc?.type === type && !compounded && !tc.restart && !tc.level,
            action: () => quick(type, (id) => newTableCalc(id, type, options)),
          })),
          {
            label: "年初至今總計",
            disabled: !levels || levels.finer.length === 0,
            checked: tc?.type === "running_total" && Boolean(tc.restart),
            action: () => levels && quick("ytd total", (id) => dateCalc(newTableCalc(id, "running_total", {}), "ytd", levels)),
          },
          {
            label: "複合成長率",
            checked: tc?.type === "percent_difference" && compounded,
            action: () => quick("compound growth", (id) => newTableCalc(id, "percent_difference", { relative_to: "first", compounded: true })),
          },
          {
            label: "年成長率",
            disabled: !levels,
            checked: tc?.type === "percent_difference" && Boolean(tc.level),
            action: () => levels && quick("year over year", (id) => dateCalc(newTableCalc(id, "percent_difference", {}), "yoy", levels)),
          },
        ],
      });
      if (tc && tc.type !== "formula") {
        items.push({
          label: "清除表計算",
          action: () =>
            apply((s) => changeUse({ ...s, table_calcs: s.table_calcs.filter((t) => t.id !== tc.id) }, shelf, index, { ...use, table_calc: null }, card)),
        });
      }
      if (tc) {
        items.push({
          label: "計算方向",
          children: [
            ...DIRECTIONS.map(([preset, label]) => ({
              label,
              checked: tc.preset === preset,
              action: () =>
                apply((s) => ({
                  ...s,
                  table_calcs: s.table_calcs.map((t) => (t.id === tc.id ? { ...t, preset, inferred: false } : t)),
                })),
            })),
            { label: "指定維度…", checked: tc.preset === "specific", action: () => editor.openDialog({ kind: "direction", tableCalc: tc.id }) },
          ],
        });
        // 計算類型與選項（相對於、排名方式、移動計算的範圍等）、計算層級與重新開始間隔
        items.push({ label: "編輯表計算…", action: () => editor.openDialog({ kind: "direction", tableCalc: tc.id }) });
      }
      items.push({ label: "格式…", action: () => editor.openDialog({ kind: "format", use }) });
    }

    if (onAxis) {
      items.push(SEPARATOR);
      const axisShelf = shelf as "rows" | "columns";
      const axis = ((sheet.axes as { use: FieldUse; shelf: string; show?: boolean }[] | undefined) ?? []).find(
        (a) => sameUse(a.use, use) && a.shelf === shelf,
      );
      items.push({
        label: "顯示標題",
        checked: axis?.show !== false,
        action: () => apply((s) => toggleHeader(s, use, axisShelf)),
      });
      if (use.continuous) items.push({ label: "編輯座標軸…", action: () => editor.openDialog({ kind: "axis", use, shelf: axisShelf }) });
      const shelfUses = usesOn(sheet, shelf, card);
      if (measure) {
        const position = shelfUses.slice(0, index).filter((u) => isMeasure(u, fields)).length;
        if (position > 0) {
          const pane = sheet.panes.find((p) => p.axis === shelf && sameUse(p.measure, use));
          items.push({ label: "雙軸", checked: pane?.dual === true, action: () => apply((s) => toggleDual(s, use, axisShelf)) });
        }
      } else if (!use.continuous && index < shelfUses.length - 1) {
        const on = sheet.subtotals.some((u) => sameUse(u, use));
        items.push({
          label: "小計",
          checked: on,
          action: () =>
            apply((s) => ({ ...s, subtotals: on ? s.subtotals.filter((u) => !sameUse(u, use)) : [...s.subtotals, use] })),
        });
      }
    }
    items.push(SEPARATOR, remove);
    return items;
  };
}

function toggleHeader(sheet: SheetIR, use: FieldUse, shelf: "rows" | "columns"): SheetIR {
  const axes = ((sheet.axes as Record<string, unknown>[] | undefined) ?? []) as { use: FieldUse; shelf: string; show?: boolean }[];
  const found = axes.find((a) => sameUse(a.use, use) && a.shelf === shelf);
  if (found) return { ...sheet, axes: axes.map((a) => (a === found ? { ...a, show: a.show === false } : a)) };
  return { ...sheet, axes: [...axes, { use, shelf, show: false }] };
}

/** 雙軸：量值有自己的標記卡（沒有時以共用的標記卡建立），並與前一個量值重疊；與 Tableau 相同，
 * 標記卡上沒有色彩時把 Measure Names 放到色彩，兩個量值以不同的顏色區分。 */
function toggleDual(sheet: SheetIR, use: FieldUse, shelf: "rows" | "columns"): SheetIR {
  const found = sheet.panes.find((p) => p.axis === shelf && sameUse(p.measure, use));
  const updated = found
    ? { ...sheet, panes: sheet.panes.map((p) => (p === found ? { ...p, dual: !p.dual } : p)) }
    : { ...sheet, panes: [...sheet.panes, { measure: use, axis: shelf, dual: true, marks: structuredClone(sheet.marks) }] };
  const dual = updated.panes.some((p) => p.dual);
  const colored = [updated.marks, ...updated.panes.map((p) => p.marks)].some((m) => m.color !== null);
  if (!dual || colored) return updated;
  const names: FieldUse = { field: MEASURE_NAMES, agg: null, date: null, continuous: false, table_calc: null };
  return updateCard(updated, "all", (m) => ({ ...m, color: names }));
}

// ---------------------------------------------------------------- 架上的臨時計算

/** 在架上輸入公式（Tableau 的臨時計算）：連按兩下架上的空白處輸入，或從膠囊選單「在架上編輯」。
 * 公式只是一個欄位（或欄位的彙總）時直接用這個欄位，其他的建立臨時計算（資料窗格不顯示）。 */
function useAdHoc(shelf: ShelfName, card: Card) {
  const editor = useEditor();
  const [editing, setEditing] = useState<{ index: number | null; text: string; original: string } | null>(null);
  // Enter 之後輸入框消失時可能再觸發一次 blur：同一次編輯只送出一次
  const committed = useRef(false);
  const start = (index: number | null, text: string) => {
    committed.current = false;
    setEditing({ index, text, original: text });
  };
  const commit = async () => {
    if (committed.current) return;
    committed.current = true;
    const current = editing;
    setEditing(null);
    const formula = current?.text.trim() ?? "";
    if (!current || !formula || formula === current.original) return;
    let use = plainFormulaUse(formula, editor.fields);
    if (!use) {
      try {
        const id = await editApi.saveAdHoc(editor.workbook, editor.sheet.datasource, formula);
        const field = (await editor.refreshCatalog()).get(id);
        if (!field) return;
        use = defaultUse(field);
      } catch (e) {
        editor.notify((e as Error).message, "error");
        return;
      }
    }
    const ready = use;
    editor.apply(async (sheet) => {
      const [prepared, placed] = await editor.prepare(sheet, ready);
      return current.index === null ? insertUse(prepared, shelf, placed, undefined, card) : changeUse(prepared, shelf, current.index, placed, card);
    });
  };
  const input = editing && (
    <input
      className="adhoc-input"
      aria-label="在架上輸入公式"
      autoFocus
      value={editing.text}
      placeholder="輸入公式，例如 SUM([Sales]) / SUM([Quantity])"
      onChange={(e) => setEditing({ ...editing, text: e.target.value })}
      onKeyDown={(e) => {
        if (e.key === "Enter") void commit();
        else if (e.key === "Escape") {
          committed.current = true;
          setEditing(null);
        }
      }}
      onBlur={() => void commit()}
    />
  );
  /** 連按兩下架上的空白處（不是膠囊）開始輸入。 */
  const onDoubleClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest(".pill, .adhoc-input, button")) return;
    start(null, "");
  };
  /** 膠囊選單加上「在架上編輯…」：以膠囊的公式開始編輯。 */
  const withEdit = (items: MenuItem[], use: FieldUse, index: number): MenuItem[] => {
    if (isSpecial(use) || use.table_calc !== null || items.length < 2) return items;
    const text = pillFormula(use, editor.fields);
    const edit: MenuItem[] = [{ label: "在架上編輯…", action: () => start(index, text) }];
    const field = editor.fields.get(use.field);
    // 臨時計算命名之後成為資料窗格中的計算欄位（Tableau 把膠囊拖到資料窗格）
    if (field?.ad_hoc) edit.push({ label: "命名並加到資料窗格…", action: () => editor.openCalc(field) });
    return [...items.slice(0, -2), ...edit, ...items.slice(-2)];
  };
  return { editing, input, onDoubleClick, withEdit };
}

// ---------------------------------------------------------------- 架

export function Shelf({ label, shelf, card = "all" }: { label: string; shelf: ShelfName; card?: Card }) {
  const editor = useEditor();
  const menu = usePillMenu();
  const { over, handlers, dropAt } = useDropTarget(shelf, card, (use) => editor.openFilter({ use, index: null }));
  const uses = usesOn(editor.sheet, shelf, card);
  const adHoc = useAdHoc(shelf, card);
  const typed = shelf === "rows" || shelf === "columns";
  return (
    <div className="shelf" data-shelf={shelf}>
      <span className="shelf-label">{label}</span>
      <div
        className={`shelf-drop${over ? " over" : ""}`}
        {...handlers}
        data-drop={shelf}
        onDoubleClick={typed ? adHoc.onDoubleClick : undefined}
        title={typed ? "將欄位拖曳到這裡，或連按兩下輸入公式" : undefined}
      >
        {uses.map((use, i) =>
          adHoc.editing?.index === i ? (
            <span key={`${i}:edit`} className="pill-slot">
              {adHoc.input}
            </span>
          ) : (
            <span key={`${i}:${use.field}`} className="pill-slot" onDragOver={handlers.onDragOver} onDrop={(e) => dropAt(e, i)}>
              {(shelf === "rows" || shelf === "columns") && <DrillButton shelf={shelf} index={i} />}
              <Pill use={use} shelf={shelf} index={i} card={card} menu={() => (typed ? adHoc.withEdit(menu(shelf, i, card), use, i) : menu(shelf, i, card))} />
            </span>
          ),
        )}
        {adHoc.editing?.index === null && adHoc.input}
        {uses.length === 0 && !adHoc.editing && <span className="shelf-hint">將欄位拖曳到這裡</span>}
      </div>
    </div>
  );
}

/** 依值組合的篩選：篩選動作，或檢視上「只保留」「排除」的多個維度（與 Tableau 的包含、排除項目相同）。 */
function tupleLabel(filter: FilterIR, fields: Map<string, CatalogField>): string {
  if (filter.action) return "動作（篩選）";
  const names = ((filter.fields as FieldUse[] | undefined) ?? []).map((u) => fields.get(u.field)?.caption ?? u.field).join("、");
  return `${filter.mode === "exclude" ? "排除" : "包含"}（${names}）`;
}

/** 工作表上顯示的控制項（顯示篩選、顯示參數）。 */
export interface SheetControlIR {
  kind: "filter" | "parameter";
  field: FieldUse | null;
  parameter: string | null;
  control: string;
  relevant: boolean;
}

/** 篩選架：每個篩選一個膠囊（編輯、加入至內容、移除）。 */
export function FiltersShelf() {
  const editor = useEditor();
  const { sheet, fields, apply } = editor;
  const { over, handlers } = useDropTarget("filters", "all", (use) => editor.openFilter({ use, index: null }));
  const menu = (filter: FilterIR, index: number): MenuItem[] => {
    if (filter.kind === "tuple") return [{ label: "移除", action: () => apply((s) => removeUse(s, "filters", index)) }];
    const context = filter.stage === "context";
    const canContext = filter.stage === "dimension" || filter.stage === "top_n" || context;
    const shown = ((sheet.controls as SheetControlIR[] | undefined) ?? []).find((c) => c.kind === "filter" && sameUse(c.field, filter.field));
    return [
      { label: "編輯篩選…", action: () => editor.openFilter({ use: filter.field, index }) },
      {
        label: context ? "從內容中移除" : "加入至內容",
        disabled: !canContext,
        action: () =>
          apply((s) => ({
            ...s,
            filters: s.filters.map((f, i) =>
              i === index ? { ...f, stage: context ? (f.kind === "top_n" ? "top_n" : "dimension") : "context" } : f,
            ),
          })),
      },
      // 選取的工作表、使用此資料源的所有工作表（同一個篩選，檢視器中一起變更）
      { label: "套用到工作表…", checked: Boolean(filter.group), action: () => editor.openDialog({ kind: "apply-filter", filter: filter.id }) },
      // 顯示篩選：單獨檢視工作表時，檢視旁邊顯示這個篩選的控制項
      {
        label: "顯示篩選",
        checked: shown !== undefined,
        action: () =>
          apply((s) => {
            const controls = (s.controls as SheetControlIR[] | undefined) ?? [];
            if (shown) return { ...s, controls: controls.filter((c) => c !== shown && !(c.kind === "filter" && sameUse(c.field, filter.field))) };
            const date = ["date", "datetime"].includes(fields.get(filter.field.field)?.datatype ?? "") && !filter.field.date;
            const control =
              filter.kind === "range" ? (date ? "date_range" : "range_slider") : filter.kind === "relative_date" ? "relative_date" : filter.kind === "condition" || filter.kind === "top_n" ? "condition" : "multi_list";
            return { ...s, controls: [...controls, { kind: "filter", field: filter.field, parameter: null, control, relevant: false }] };
          }),
      },
      ...(shown && filter.kind === "categorical"
        ? [
            {
              label: "只顯示相關的值",
              checked: shown.relevant === true,
              action: () =>
                apply((s) => ({
                  ...s,
                  controls: ((s.controls as SheetControlIR[] | undefined) ?? []).map((c) => (c.kind === "filter" && sameUse(c.field, filter.field) ? { ...c, relevant: !c.relevant } : c)),
                })),
            },
          ]
        : []),
      SEPARATOR,
      { label: "移除", action: () => apply((s) => removeUse(s, "filters", index)) },
    ];
  };
  return (
    <div className="card filters-card" data-shelf="filters">
      <div className="card-title">篩選</div>
      <div className={`card-drop${over ? " over" : ""}`} {...handlers} data-drop="filters">
        {sheet.filters.map((filter, i) => {
          const label =
            filter.kind === "tuple"
              ? tupleLabel(filter, fields)
              : `${pillLabel(filter.field, fields, sheet.table_calcs)}${filter.kind === "top_n" ? "（頂部）" : filter.kind === "relative_date" ? "（相對日期）" : ""}`;
          const className = `pill filter${filter.stage === "context" ? " context" : ""}${filter.field?.continuous ? " continuous" : " discrete"}`;
          return (
            <Pill
              key={filter.id}
              use={filter.field ?? { field: filter.id, agg: null, date: null, continuous: false, table_calc: null }}
              shelf="filters"
              index={i}
              card="all"
              label={label}
              className={className}
              menu={() => menu(filter, i)}
            />
          );
        })}
        {sheet.filters.length === 0 && <span className="shelf-hint">將欄位拖曳到這裡篩選</span>}
      </div>
    </div>
  );
}

/** Measure Values 卡（檢視中有 Measure Values 時）。 */
export function MeasureValuesCard() {
  const editor = useEditor();
  const menu = usePillMenu();
  const { over, handlers, dropAt } = useDropTarget("measure_values", "all");
  const uses = editor.sheet.measure_values;
  return (
    <div className="card" data-shelf="measure_values">
      <div className="card-title">量值</div>
      <div className={`card-drop${over ? " over" : ""}`} {...handlers} data-drop="measure_values">
        {uses.map((use, i) => (
          <span key={`${i}:${use.field}`} className="pill-slot" onDrop={(e) => dropAt(e, i)} onDragOver={handlers.onDragOver}>
            <Pill use={use} shelf="measure_values" index={i} card="all" menu={() => menu("measure_values", i, "all")} />
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 標記卡

const SLOTS: [MarkSlot, string][] = [
  ["color", "色彩"],
  ["size", "大小"],
  ["label", "標籤"],
  ["detail", "詳細資料"],
  ["tooltip", "工具提示"],
  ["shape", "形狀"],
  ["angle", "角度"],
  ["path", "路徑"],
  ["geometry", "空間"],
];

function MarkSlotRow({ slot, label, card }: { slot: MarkSlot; label: string; card: Card }) {
  const editor = useEditor();
  const menu = usePillMenu();
  const shelf = `marks.${slot}` as ShelfName;
  const { over, handlers, dropAt } = useDropTarget(shelf, card);
  const uses = usesOn(editor.sheet, shelf, card).filter((u) => slot !== "geometry" || u.field !== GEOMETRY_GENERATED);
  // 臨時計算：連按兩下「詳細資料」的空白處輸入公式（與 Tableau 標記卡相同，加到詳細資料）
  const adHoc = useAdHoc(shelf, card);
  const typed = slot === "detail";
  return (
    <div
      className={`mark-slot${over ? " over" : ""}`}
      {...handlers}
      data-drop={shelf}
      onDoubleClick={typed ? adHoc.onDoubleClick : undefined}
      title={typed ? "連按兩下輸入公式" : undefined}
    >
      <span className="mark-slot-label">{label}</span>
      <span className="mark-slot-pills">
        {uses.map((use, i) => (
          <span key={`${i}:${use.field}`} className="pill-slot" onDrop={(e) => dropAt(e, i)} onDragOver={handlers.onDragOver}>
            <Pill use={use} shelf={shelf} index={i} card={card} menu={() => menu(shelf, i, card)} />
          </span>
        ))}
        {typed && adHoc.input}
      </span>
    </div>
  );
}

export function MarksCard() {
  const editor = useEditor();
  const { sheet, card, setCard, fields, apply } = editor;
  const marks = cardMarks(sheet, card);
  const tabs = sheet.panes.length > 0;
  // 空間資料的形狀：資料源有空間欄位時才顯示這一格（Geometry (generated) 不顯示）
  const spatialShown = [...fields.values()].some((f) => f.datatype === "spatial");
  return (
    <div className="card marks-card" data-shelf="marks">
      <div className="card-title">標記</div>
      {tabs && (
        <div className="mark-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={card === "all"} className={card === "all" ? "active" : ""} onClick={() => setCard("all")}>
            全部
          </button>
          {sheet.panes.map((pane, i) => (
            <button
              type="button"
              role="tab"
              key={i}
              aria-selected={card === i}
              className={card === i ? "active" : ""}
              onClick={() => setCard(i)}
            >
              {pillLabel(pane.measure, fields, sheet.table_calcs)}
            </button>
          ))}
        </div>
      )}
      <select
        aria-label="標記類型"
        value={marks.type}
        onChange={(e) => {
          const type = e.target.value;
          apply((s) => updateCard(s, card, (m) => ({ ...m, type })));
        }}
      >
        {MARK_TYPES.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <div className="mark-buttons">
        <button type="button" onClick={() => editor.openDialog({ kind: "colors", card })}>
          色彩…
        </button>
        {marks.shape && (
          <button type="button" title="各成員的形狀（Tableau 的「編輯形狀」）" onClick={() => editor.openDialog({ kind: "shapes", card })}>
            形狀…
          </button>
        )}
        <button type="button" title="標記標籤：顯示與否、自訂文字、要標示的標記" onClick={() => editor.openDialog({ kind: "labels", card })}>
          標籤…
        </button>
        <button
          type="button"
          title="工具提示的文字與格式、工具提示中的工作表（Viz in Tooltip）"
          onClick={() => editor.openDialog({ kind: "tooltip", card })}
        >
          工具提示…
        </button>
        <label className="mark-size" title="標記的大小">
          大小
          <input
            type="range"
            min={0.1}
            max={2}
            step={0.05}
            value={typeof marks.mark_size === "number" ? marks.mark_size : 1}
            onChange={(e) => {
              const size = Number(e.target.value);
              apply((s) => updateCard(s, card, (m) => ({ ...m, mark_size: size })));
            }}
          />
        </label>
      </div>
      {marks.type === "line" && (
        <label className="inline line-type" title="線條類型（Tableau 標記卡「路徑」的線條類型）">
          線條類型
          <select
            aria-label="線條類型"
            value={(marks.line_type as string | undefined) ?? "linear"}
            onChange={(e) => {
              const line_type = e.target.value;
              apply((s) => updateCard(s, card, (m) => ({ ...m, line_type })));
            }}
          >
            <option value="linear">直線</option>
            <option value="step">階梯</option>
            <option value="jump">跳躍</option>
          </select>
        </label>
      )}
      {SLOTS.filter(([slot]) => slot !== "geometry" || spatialShown).map(([slot, label]) => (
        <MarkSlotRow key={slot} slot={slot} label={label} card={card} />
      ))}
    </div>
  );
}
