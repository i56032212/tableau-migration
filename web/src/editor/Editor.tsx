// 桌面版的工作表編輯器（Tableau Desktop 的工作表）：資料窗格、篩選與標記卡、欄列架與即時的檢視。
// 每次變更整理後把整張工作表存回（PUT），檢視重新查詢；復原與重做在這裡記錄。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FieldUse } from "../types";
import { WorksheetView } from "../WorksheetView";
import { editApi } from "./api";
import { type EditorContextValue, type EditorDialog, EditorContext, type FilterTarget, useEditor } from "./context";
import { DataPane } from "./DataPane";
import {
  AxisDialog,
  CalcDialog,
  ColorsDialog,
  DirectionDialog,
  FormatDialog,
  ParameterDialog,
  ReferenceDialog,
  SheetFormatDialog,
  TitleDialog,
  TrendDialog,
} from "./Dialogs";
import { FilterDialog } from "./FilterDialog";
import {
  type Card,
  type Catalog,
  type CatalogField,
  cardMarks,
  fieldIndex,
  MEASURE_VALUES,
  markUses,
  newTableCalc,
  pillLabel,
  type SheetIR,
  swapAxes,
  tidySheet,
} from "./model";
import { FiltersShelf, MarksCard, MeasureValuesCard, Shelf } from "./Shelves";

const EMPTY_STATE = { filters: {}, parameters: {} };

interface History {
  entries: SheetIR[];
  index: number;
}

/** 換掉工作表的資料源：欄位依內部名稱對應到新資料源的欄位；有對應不到的欄位時回傳 null。 */
function replaceDatasource(sheet: SheetIR, from: CatalogField[], to: CatalogField[], target: string): SheetIR | null {
  const byName = new Map(to.map((f) => [f.name, f.id]));
  const mapping = new Map<string, string>();
  for (const f of from) {
    const found = byName.get(f.name);
    if (found) mapping.set(f.id, found);
  }
  let missing = false;
  const map = (id: string) => {
    if (id === "measure_names" || id === MEASURE_VALUES) return id;
    const found = mapping.get(id);
    if (!found) missing = true;
    return found ?? id;
  };
  const walk = (value: unknown, key?: string): unknown => {
    if (Array.isArray(value)) return key === "addressing" ? value.map((v) => (typeof v === "string" ? map(v) : v)) : value.map((v) => walk(v));
    if (value && typeof value === "object") {
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, k === "field" && typeof v === "string" ? map(v) : walk(v, k)]),
      );
    }
    return value;
  };
  const updated = walk(sheet) as SheetIR;
  if (missing) return null;
  return { ...updated, datasource: target, formats: {} };
}

function AnalyticsCard() {
  const editor = useEditor();
  const { sheet, fields, card } = editor;
  const lines = (sheet.reference_lines as { formula: string; kind: string; axis: FieldUse; paired?: string | null }[] | undefined) ?? [];
  const trend = cardMarks(sheet, card).trend;
  const names: Record<string, string> = {
    average: "平均",
    median: "中位數",
    sum: "總和",
    total: "總計",
    min: "最小值",
    max: "最大值",
    constant: "常數",
    percentile: "百分位數",
    stdev: "標準差",
    quantiles: "分位數",
    confidence: "信賴區間",
    median_confidence: "中位數信賴區間",
  };
  return (
    <div className="card analytics-card" data-shelf="analytics">
      <div className="card-title">分析</div>
      <div className="analytics-actions">
        <button type="button" onClick={() => editor.openDialog({ kind: "reference", index: null, axis: null })}>
          ＋ 參考線…
        </button>
        <button type="button" onClick={() => editor.openDialog({ kind: "trend", card })}>
          {trend ? "趨勢線 ✓" : "＋ 趨勢線…"}
        </button>
      </div>
      <ul className="analytics-list">
        {lines.map((line, i) => (
          <li key={i}>
            <span>
              {line.kind === "boxplot" ? "盒鬚圖" : line.kind === "distribution" ? `分布（${names[line.formula] ?? line.formula}）` : `${names[line.formula] ?? line.formula}${line.paired ? "（區間）" : ""}`}
              ：{pillLabel(line.axis, fields, sheet.table_calcs)}
            </span>
            <span>
              <button type="button" className="link" title="編輯" onClick={() => editor.openDialog({ kind: "reference", index: i, axis: line.axis })}>
                ✎
              </button>
              <button
                type="button"
                className="link"
                title="移除"
                onClick={() => editor.apply((s) => ({ ...s, reference_lines: ((s.reference_lines as unknown[]) ?? []).filter((_, j) => j !== i) }))}
              >
                ✕
              </button>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Editor({
  workbook,
  worksheet,
  onChanged,
  onOpen,
  onClose,
}: {
  workbook: string;
  worksheet: string;
  /** 工作表新增、刪除、改名之後：報表的定義重新載入 */
  onChanged: () => void;
  /** 編輯另一張工作表 */
  onOpen: (worksheet: string) => void;
  /** 完成編輯（回到檢視）；沒有工作表時為 null */
  onClose: (worksheet: string | null) => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [history, setHistoryState] = useState<History | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [card, setCard] = useState<Card>("all");
  const [filter, setFilter] = useState<FilterTarget | null>(null);
  const [calc, setCalc] = useState<CatalogField | null | undefined>(undefined);
  const [dialog, setDialog] = useState<EditorDialog | null>(null);
  const [message, setMessage] = useState<{ text: string; kind: "info" | "error" } | null>(null);
  const [pending, setPending] = useState(0);
  // 引擎中目前的工作表（存檔完成的版本）：檢視以它決定要不要查詢
  const [stored, setStored] = useState<SheetIR | null>(null);
  const historyRef = useRef<History | null>(null);
  const fieldsRef = useRef(new Map<string, CatalogField>());
  const queue = useRef<Promise<void>>(Promise.resolve());

  const setHistory = (next: History | null) => {
    historyRef.current = next;
    setHistoryState(next);
  };

  const notify = useCallback((text: string, kind: "info" | "error" = "info") => {
    setMessage({ text, kind });
  }, []);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(null), message.kind === "error" ? 8000 : 3000);
    return () => window.clearTimeout(timer);
  }, [message]);

  const reloadCatalog = useCallback(
    () =>
      editApi
        .catalog(workbook)
        .then(setCatalog)
        .catch((e: Error) => notify(e.message, "error")),
    [workbook, notify],
  );

  useEffect(() => {
    let cancelled = false;
    setHistory(null);
    setCard("all");
    Promise.all([editApi.catalog(workbook), editApi.worksheet(workbook, worksheet)])
      .then(([loadedCatalog, sheet]) => {
        if (cancelled) return;
        setCatalog(loadedCatalog);
        setHistory({ entries: [sheet], index: 0 });
        setStored(sheet);
        setError(null);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook, worksheet]);

  const sheet = history ? history.entries[history.index] : null;
  const fields = useMemo(() => (catalog && sheet ? fieldIndex(catalog, sheet.datasource) : new Map<string, CatalogField>()), [catalog, sheet?.datasource]);
  fieldsRef.current = fields;

  /** 依序執行（存檔的順序與變更相同）。 */
  const enqueue = useCallback((task: () => Promise<void>) => {
    setPending((n) => n + 1);
    queue.current = queue.current.then(task).finally(() => setPending((n) => n - 1));
  }, []);

  const save = useCallback(
    async (next: SheetIR): Promise<SheetIR | null> => {
      try {
        const saved = await editApi.saveWorksheet(workbook, next);
        setStored(saved);
        setRevision((r) => r + 1);
        return saved;
      } catch (e) {
        notify((e as Error).message, "error");
        return null;
      }
    },
    [workbook, notify],
  );

  const apply = useCallback(
    (change: (sheet: SheetIR) => SheetIR | Promise<SheetIR>) =>
      enqueue(async () => {
        const current = historyRef.current;
        if (!current) return;
        const base = current.entries[current.index];
        let next: SheetIR;
        try {
          next = tidySheet(await change(base), fieldsRef.current);
        } catch (e) {
          notify((e as Error).message, "error");
          return;
        }
        if (JSON.stringify(next) === JSON.stringify(base)) return;
        // 先顯示變更，存檔失敗時退回
        setHistory({ entries: [...current.entries.slice(0, current.index + 1), next], index: current.index + 1 });
        const saved = await save(next);
        const latest = historyRef.current;
        if (!latest) return;
        if (saved === null) {
          setHistory({ entries: latest.entries.slice(0, latest.index), index: latest.index - 1 });
          return;
        }
        setHistory({ entries: latest.entries.map((e, i) => (i === latest.index ? saved : e)), index: latest.index });
        if (saved.name !== base.name) onChanged();
      }),
    [enqueue, save, notify, onChanged],
  );

  const move = useCallback(
    (delta: number) =>
      enqueue(async () => {
        const current = historyRef.current;
        if (!current) return;
        const index = current.index + delta;
        if (index < 0 || index >= current.entries.length) return;
        const saved = await save(current.entries[index]);
        if (saved === null) return;
        setHistory({ entries: current.entries, index });
        if (saved.name !== current.entries[current.index].name) onChanged();
      }),
    [enqueue, save, onChanged],
  );
  const undo = useCallback(() => move(-1), [move]);
  const redo = useCallback(() => move(1), [move]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")) return;
      if (!(e.ctrlKey || e.metaKey)) return;
      const key = e.key.toLowerCase();
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (key === "y" || (key === "z" && e.shiftKey)) {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  const newId = useCallback((prefix: string, text: string) => editApi.newId(workbook, prefix, text), [workbook]);

  const prepare = useCallback(
    async (current: SheetIR, use: FieldUse): Promise<[SheetIR, FieldUse]> => {
      const field = fieldsRef.current.get(use.field);
      if (field?.level !== "table_calc" || use.table_calc !== null) return [current, use];
      // Table Calculation 計算欄位：與 Tableau 相同，預設的計算方向為表格（橫向）
      const id = await newId("tc", `formula ${field.caption}`);
      return [{ ...current, table_calcs: [...current.table_calcs, newTableCalc(id, "formula", {})] }, { ...use, table_calc: id }];
    },
    [newId],
  );

  if (error) return <main className="error">無法編輯：{error}</main>;
  if (!catalog || !sheet || !history) return <main>載入中…</main>;

  const context: EditorContextValue = {
    workbook,
    sheet,
    catalog,
    fields,
    card: card !== "all" && card >= sheet.panes.length ? "all" : card,
    setCard,
    apply,
    prepare,
    newId,
    openFilter: setFilter,
    openCalc: (field) => setCalc(field),
    openDialog: setDialog,
    notify,
  };
  const shown = stored ?? sheet;
  const empty = shown.rows.length === 0 && shown.columns.length === 0 && markUses(shown.marks).length === 0;
  const measureValuesUsed = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks)].some((u) => u.field === MEASURE_VALUES);
  const totals = new Set(sheet.totals);
  const toggleTotal = (which: "rows" | "columns") =>
    apply((s) => ({ ...s, totals: s.totals.includes(which) ? s.totals.filter((t) => t !== which) : [...s.totals, which] }));

  const changeDatasource = (target: string) => {
    if (target === sheet.datasource) return;
    const from = catalog.datasources.find((d) => d.id === sheet.datasource)?.fields ?? [];
    const to = catalog.datasources.find((d) => d.id === target)?.fields ?? [];
    const replaced = replaceDatasource(sheet, from, to, target);
    if (!replaced) {
      notify("工作表上有新資料源中沒有的欄位，無法取代資料源", "error");
      return;
    }
    apply(() => replaced);
  };

  return (
    <EditorContext.Provider value={context}>
      <div className="editor" data-editor={sheet.id} data-saving={pending > 0 ? "true" : "false"}>
        <div className="editor-toolbar" role="toolbar" aria-label="工作表編輯">
          <button type="button" onClick={undo} disabled={history.index === 0} title="復原（Ctrl+Z）">
            ↶ 復原
          </button>
          <button type="button" onClick={redo} disabled={history.index >= history.entries.length - 1} title="重做（Ctrl+Y）">
            ↷ 重做
          </button>
          <span className="sep" />
          <button type="button" onClick={() => apply(swapAxes)} title="交換欄與列">
            ⇄ 交換
          </button>
          <label className="inline">
            顯示大小
            <select
              aria-label="顯示大小"
              value={sheet.fit}
              onChange={(e) => {
                const fit = e.target.value;
                apply((s) => ({ ...s, fit }));
              }}
            >
              <option value="standard">標準</option>
              <option value="fit_width">符合寬度</option>
              <option value="fit_height">符合高度</option>
              <option value="entire_view">整個檢視</option>
            </select>
          </label>
          <label className="inline" title="每一欄的合計（下方的總計列）">
            <input type="checkbox" checked={totals.has("rows")} onChange={() => toggleTotal("rows")} /> 顯示欄總計
          </label>
          <label className="inline" title="每一列的合計（右側的總計欄）">
            <input type="checkbox" checked={totals.has("columns")} onChange={() => toggleTotal("columns")} /> 顯示列總計
          </label>
          <button type="button" onClick={() => setDialog({ kind: "title" })}>
            標題…
          </button>
          <button type="button" onClick={() => setDialog({ kind: "sheet-format" })}>
            格式…
          </button>
          <span className="sep" />
          <label className="inline">
            工作表
            <input
              key={`${sheet.id}:${sheet.name}`}
              aria-label="工作表名稱"
              defaultValue={sheet.name}
              onBlur={(e) => {
                const name = e.target.value.trim();
                if (name && name !== sheet.name) apply((s) => ({ ...s, name }));
                else e.target.value = sheet.name;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
          </label>
          <button
            type="button"
            onClick={() =>
              void editApi
                .addWorksheet(workbook, undefined, sheet.datasource)
                .then((created) => {
                  onChanged();
                  onOpen(created.id);
                })
                .catch((e: Error) => notify(e.message, "error"))
            }
          >
            新增工作表
          </button>
          <button
            type="button"
            onClick={() =>
              void editApi
                .duplicateWorksheet(workbook, sheet.id)
                .then((created) => {
                  onChanged();
                  onOpen(created.id);
                })
                .catch((e: Error) => notify(e.message, "error"))
            }
          >
            複製
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (!window.confirm(`刪除工作表「${sheet.name}」？`)) return;
              void editApi
                .removeWorksheet(workbook, sheet.id)
                .then(() => {
                  onChanged();
                  onClose(null);
                })
                .catch((e: Error) => notify(e.message, "error"));
            }}
          >
            刪除
          </button>
          <span className="spacer" />
          {pending > 0 && <span className="hint">儲存中…</span>}
          <button type="button" className="primary" onClick={() => onClose(sheet.id)}>
            完成
          </button>
        </div>
        <DataPane onDatasource={changeDatasource} />
        <div className="cards-pane">
          <FiltersShelf />
          {measureValuesUsed && <MeasureValuesCard />}
          <MarksCard />
          <AnalyticsCard />
        </div>
        <div className="view-pane">
          <Shelf label="欄" shelf="columns" />
          <Shelf label="列" shelf="rows" />
          <div className="editor-canvas">
            {empty ? (
              <div className="placeholder">將欄位從資料窗格拖曳到欄、列或標記卡</div>
            ) : (
              <WorksheetView workbook={workbook} worksheet={sheet.id} state={EMPTY_STATE} revision={revision} />
            )}
          </div>
        </div>
        {message && (
          <div className={`toast${message.kind === "error" ? " error" : ""}`} role="status">
            {message.text}
          </div>
        )}
      </div>
      {filter && <FilterDialog target={filter} onClose={() => setFilter(null)} />}
      {calc !== undefined && (
        <CalcDialog
          field={calc}
          onClose={() => setCalc(undefined)}
          onSaved={() => {
            void reloadCatalog();
            setRevision((r) => r + 1);
          }}
        />
      )}
      {dialog?.kind === "direction" && <DirectionDialog tableCalc={dialog.tableCalc} onClose={() => setDialog(null)} />}
      {dialog?.kind === "format" && <FormatDialog use={dialog.use} onClose={() => setDialog(null)} />}
      {dialog?.kind === "title" && <TitleDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "sheet-format" && <SheetFormatDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "axis" && <AxisDialog use={dialog.use} shelf={dialog.shelf} onClose={() => setDialog(null)} />}
      {dialog?.kind === "reference" && <ReferenceDialog index={dialog.index} axis={dialog.axis} onClose={() => setDialog(null)} />}
      {dialog?.kind === "trend" && <TrendDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "colors" && <ColorsDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "parameter" && (
        <ParameterDialog
          parameter={dialog.parameter}
          onClose={() => setDialog(null)}
          onSaved={() => {
            void reloadCatalog();
            setRevision((r) => r + 1);
            onChanged();
          }}
        />
      )}
    </EditorContext.Provider>
  );
}
