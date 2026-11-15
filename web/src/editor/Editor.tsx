// 桌面版的工作表編輯器（Tableau Desktop 的工作表）：資料窗格、篩選與標記卡、欄列架與即時的檢視。
// 每次變更整理後把整張工作表存回（PUT），檢視重新查詢；復原與重做在這裡記錄。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Modal } from "../Server";
import type { FieldUse, IR, Mark, Scalar } from "../types";
import type { ViewerAccess } from "../viewerApi";
import { DataDialog } from "../ViewerTools";
import { WorksheetView } from "../WorksheetView";
import { editApi } from "./api";
import { type EditorContextValue, type EditorDialog, EditorContext, type FilterTarget, useEditor } from "./context";
import { ImportSheetsDialog } from "./ImportDialog";
import { DescribeModelDialog } from "./DescribeDialogs";
import { DataPane } from "./DataPane";
import {
  AliasDialog,
  BinDialog,
  CombinedDialog,
  CommentDialog,
  DatePropertiesDialog,
  FieldFormatDialog,
  FiscalDialog,
  FolderDialog,
  GroupDialog,
  RenameDialog,
  SetDialog,
} from "./FieldDialogs";
import {
  AxisDialog,
  CalcDialog,
  ClusterDialog,
  ColorsDialog,
  ForecastDialog,
  FormatDialog,
  GeoDialog,
  HierarchyDialog,
  ParameterDialog,
  ReferenceDialog,
  SheetFormatDialog,
  TitleDialog,
  TooltipDialog,
  TrendDialog,
} from "./Dialogs";
import { FilterDialog } from "./FilterDialog";
import { AnnotationsDialog, ApplyFilterDialog, BlendsDialog, LabelDialog, SortDialog, TableCalcDialog } from "./MoreDialogs";
import { CaptionDialog, ShapesDialog, TotalsDialog, clearFormat, copyFormat, hasCopiedFormat, pasteFormat } from "./ViewDialogs";
import { moreFieldDialog } from "./FieldMoreDialogs";
import {
  type Card,
  type Catalog,
  type CatalogField,
  cardMarks,
  fieldIndex,
  addMapLayer,
  isMapSheet,
  isMeasure,
  isSpecial,
  MEASURE_VALUES,
  markUses,
  newTableCalc,
  pillLabel,
  selectionFilter,
  selectionLevels,
  setAggregated,
  setSort,
  type SheetIR,
  swapAxes,
  tidySheet,
} from "./model";
import { FiltersShelf, MarksCard, MeasureValuesCard, Shelf } from "./Shelves";
import { ShowMePanel } from "./ShowMe";
import { PageControl } from "../PageControl";

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
  const own = new Set(from.map((f) => f.id));
  let missing = false;
  const map = (id: string) => {
    // 次要資料源（Data Blending）的欄位不變
    if (id === "measure_names" || id === MEASURE_VALUES || !own.has(id)) return id;
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
  // 次要資料源與連結由伺服器依新的主要資料源重新決定
  return { ...updated, datasource: target, formats: {}, blends: [] };
}

/** 地圖的設定（Tableau 的「地圖」選單：背景地圖、地圖圖層）。 */
interface MapOptionsIR {
  style: string;
  washout: number;
  layers: Record<string, boolean>;
  viewport: { west: number; south: number; east: number; north: number } | null;
  hide_unknown: boolean;
}

const MAP_STYLES: [string, string][] = [
  ["light", "淺色"],
  ["normal", "一般"],
  ["dark", "深色"],
  ["streets", "街道"],
  ["outdoors", "戶外"],
  ["satellite", "衛星"],
];

/** 底圖圖層與預設（與 kernel 的 tmig.render.maps.DEFAULT_LAYERS 相同）。 */
const MAP_LAYERS: [string, string, boolean][] = [
  ["base", "陸地", true],
  ["water", "水域", true],
  ["country_borders", "國界", true],
  ["country_names", "國名", true],
  ["state_borders", "州界、省界", true],
  ["state_names", "州名、省名", true],
  ["county_borders", "郡界（美國）", false],
  ["cities", "城市名稱", false],
];

function MapCard() {
  const editor = useEditor();
  const options: MapOptionsIR = {
    style: "light",
    washout: 0,
    layers: {},
    viewport: null,
    hide_unknown: false,
    ...((editor.sheet.map as Partial<MapOptionsIR> | null | undefined) ?? {}),
  };
  const change = (update: Partial<MapOptionsIR>) =>
    editor.apply((s) => ({ ...s, map: { ...options, ...update } }));
  return (
    <div className="card map-card" data-shelf="map">
      <div className="card-title">地圖</div>
      <label className="inline">
        背景地圖
        <select aria-label="背景地圖" value={options.style} onChange={(e) => change({ style: e.target.value })}>
          {MAP_STYLES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="inline" title="淡化背景地圖（0% 為原色，100% 為全白）">
        淡化
        <input
          type="range"
          aria-label="淡化"
          min={0}
          max={100}
          value={Math.round(options.washout * 100)}
          onChange={(e) => change({ washout: Number(e.target.value) / 100 })}
        />
        {Math.round(options.washout * 100)}%
      </label>
      <button
        type="button"
        title="再加上一個標記圖層（Tableau 的「新增標記圖層」）：每個圖層有自己的標記卡，由下往上畫"
        onClick={() => {
          const index = Math.max(editor.sheet.panes.length, 1);
          editor.apply((s) => addMapLayer(s));
          editor.setCard(index);
        }}
      >
        ＋ 標記圖層
      </button>
      <fieldset className="map-layers">
        <legend>地圖圖層</legend>
        {MAP_LAYERS.map(([key, label, preset]) => (
          <label key={key} className="inline">
            <input
              type="checkbox"
              checked={options.layers[key] ?? preset}
              onChange={(e) => {
                const layers = { ...options.layers, [key]: e.target.checked };
                if (layers[key] === preset) delete layers[key];
                change({ layers });
              }}
            />{" "}
            {label}
          </label>
        ))}
      </fieldset>
      <label className="inline" title="找不到位置的標記不列入檢視（Tableau 的「篩選資料」）">
        <input type="checkbox" checked={options.hide_unknown} onChange={(e) => change({ hide_unknown: e.target.checked })} /> 篩選掉未知的位置
      </label>
      {options.viewport && (
        <button type="button" title="改回依標記的位置自動縮放" onClick={() => change({ viewport: null })}>
          重設地圖（自動縮放）
        </button>
      )}
    </div>
  );
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
        <button type="button" onClick={() => editor.openDialog({ kind: "forecast" })}>
          {sheet.forecast ? "預測 ✓" : "＋ 預測…"}
        </button>
        <button type="button" onClick={() => editor.openDialog({ kind: "cluster" })}>
          {sheet.cluster ? "叢集 ✓" : "＋ 叢集…"}
        </button>
        {Boolean(trend) && (
          <button type="button" className="link" title="趨勢線的公式、R 平方、P 值與標準誤" onClick={() => editor.openDialog({ kind: "describe-model", model: "trend" })}>
            描述趨勢模型…
          </button>
        )}
        {Boolean(sheet.forecast) && (
          <button type="button" className="link" title="預測的選項與模型（趨勢、季節、平滑係數與品質）" onClick={() => editor.openDialog({ kind: "describe-model", model: "forecast" })}>
            描述預測…
          </button>
        )}
        {Boolean(sheet.cluster) && (
          <button type="button" className="link" title="叢集的摘要、各叢集的中心與變異數分析；可以存成群組" onClick={() => editor.openDialog({ kind: "describe-model", model: "cluster" })}>
            描述叢集…
          </button>
        )}
        <button type="button" title="點與區域的註解；標記的註解先在檢視上選取標記" onClick={() => editor.openDialog({ kind: "annotations", mark: null })}>
          {(sheet.annotations as unknown[] | undefined)?.length ? `註解（${(sheet.annotations as unknown[]).length}）` : "＋ 註解…"}
        </button>
      </div>
      <label className="hint aggregate-toggle" title="關閉時量值不彙總，每一列資料是一個標記（Tableau 的「分析＞彙總量值」）">
        <input
          type="checkbox"
          aria-label="彙總量值"
          checked={!sheet.disaggregated}
          onChange={(e) => {
            const aggregated = e.target.checked;
            editor.apply((s) => setAggregated(s, aggregated, fields));
          }}
        />{" "}
        彙總量值
      </label>
      <label className="hint stack-marks" title="長條與區域預設依標記卡上的維度堆疊（Tableau 的「分析＞堆疊標記」）：關閉時從 0 畫起、互相重疊，開啟時折線與圓點也堆疊">
        堆疊標記{" "}
        <select
          aria-label="堆疊標記"
          value={(sheet.stack as string | null | undefined) ?? ""}
          onChange={(e) => {
            const stack = e.target.value || null;
            editor.apply((s) => ({ ...s, stack }));
          }}
        >
          <option value="">自動</option>
          <option value="on">開啟</option>
          <option value="off">關閉</option>
        </select>
      </label>
      <label className="hint drop-lines" title="從標記畫到座標軸的虛線（Tableau 的「分析＞下拉線」）；連續的座標軸才有">
        下拉線{" "}
        <select
          aria-label="下拉線"
          value={(sheet.drop_lines as { when?: string } | null | undefined)?.when ?? ""}
          onChange={(e) => {
            const when = e.target.value;
            editor.apply((s) => ({ ...s, drop_lines: when ? { x: true, y: true, labels: false, ...((s.drop_lines as object | null) ?? {}), when } : null }));
          }}
        >
          <option value="">無</option>
          <option value="selected">選取標記時</option>
          <option value="always">一律顯示</option>
        </select>
      </label>
      {Boolean(sheet.drop_lines) && (
        <label className="hint drop-labels" title="下拉線碰到座標軸的地方寫上標記的值（Tableau 下拉線的「顯示標籤」）">
          <input
            type="checkbox"
            aria-label="下拉線的值標籤"
            checked={(sheet.drop_lines as { labels?: boolean } | null)?.labels === true}
            onChange={(e) => {
              const labels = e.target.checked;
              editor.apply((s) => ({ ...s, drop_lines: s.drop_lines ? { ...(s.drop_lines as object), labels } : null }));
            }}
          />{" "}
          下拉線的值標籤
        </label>
      )}
      <label className="hint percent-of" title="量值顯示成占範圍合計的比例（Tableau 的「分析＞百分比」）">
        百分比{" "}
        <select
          aria-label="百分比"
          value={(sheet.percentages as string | null | undefined) ?? ""}
          onChange={(e) => {
            const percentages = e.target.value || null;
            editor.apply((s) => ({ ...s, percentages }));
          }}
        >
          <option value="">無</option>
          <option value="table">表</option>
          <option value="column">欄</option>
          <option value="row">列</option>
          <option value="pane">窗格</option>
          <option value="row_in_pane">窗格中的列</option>
          <option value="column_in_pane">窗格中的欄</option>
          <option value="cell">儲存格</option>
        </select>
      </label>
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
  onEditDatasource,
}: {
  workbook: string;
  worksheet: string;
  /** 工作表新增、刪除、改名之後：報表的定義重新載入 */
  onChanged: () => void;
  /** 編輯另一張工作表 */
  onOpen: (worksheet: string) => void;
  /** 完成編輯（回到檢視）；沒有工作表時為 null */
  onClose: (worksheet: string | null) => void;
  /** 開啟資料源分頁的編輯 */
  onEditDatasource?: (datasource: string) => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [history, setHistoryState] = useState<History | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  // 檢視上選取的標記（只保留、排除、群組與集合）；工作表或它的定義改變時清除
  const [selected, setSelected] = useState<Mark[]>([]);
  const [showMe, setShowMe] = useState(false);
  const [viewData, setViewData] = useState(false);
  const [importing, setImporting] = useState(false);
  const [card, setCard] = useState<Card>("all");
  const [filter, setFilter] = useState<FilterTarget | null>(null);
  const [calc, setCalc] = useState<{ field: CatalogField | null; datasource?: string } | undefined>(undefined);
  const [dialog, setDialog] = useState<EditorDialog | null>(null);
  // 這個視窗中複製過格式（可以貼到其他工作表）
  const [copied, setCopied] = useState(false);
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
  const sheetId = sheet?.id ?? null;
  useEffect(() => setSelected([]), [sheetId, revision]);
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

  /** 重新讀取資料窗格並立即更新欄位索引：之後排進去的變更（apply）看得到新的欄位。 */
  const refreshCatalog = useCallback(async () => {
    const loaded = await editApi.catalog(workbook);
    setCatalog(loaded);
    const current = historyRef.current;
    const datasource = current ? current.entries[current.index].datasource : "";
    const index = fieldIndex(loaded, datasource);
    fieldsRef.current = index;
    return index;
  }, [workbook]);

  /** 伺服器端改了工作表之後（預設屬性套用到工作表、取代參照）：以伺服器上的內容取代目前這一步。 */
  const reloadSheet = useCallback(
    () =>
      enqueue(async () => {
        const current = historyRef.current;
        if (!current) return;
        try {
          const fresh = await editApi.worksheet(workbook, current.entries[current.index].id);
          setHistory({ entries: current.entries.map((e, i) => (i === current.index ? fresh : e)), index: current.index });
          setStored(fresh);
          setRevision((r) => r + 1);
        } catch (e) {
          notify((e as Error).message, "error");
        }
      }),
    [enqueue, workbook, notify],
  );

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
    openCalc: (field, datasource) => setCalc({ field, datasource }),
    openDialog: setDialog,
    notify,
    reloadCatalog: () => void reloadCatalog(),
    refreshCatalog,
    reloadSheet,
  };
  const shown = stored ?? sheet;
  const empty = shown.rows.length === 0 && shown.columns.length === 0 && markUses(shown.marks).length === 0;
  const measureValuesUsed = [...sheet.rows, ...sheet.columns, ...markUses(sheet.marks)].some((u) => u.field === MEASURE_VALUES);
  const totals = new Set(sheet.totals);
  // 工具列的排序（Tableau 工具列的遞增、遞減排序）：最內層的離散維度依另一架上的量值排序；
  // 同一架上前面還有維度時是巢狀排序（各窗格各自排序），沒有量值時依名稱
  const quickSort = (() => {
    for (const [dims, measures] of [
      [sheet.rows, sheet.columns],
      [sheet.columns, sheet.rows],
    ] as [FieldUse[], FieldUse[]][]) {
      const discrete = dims.filter((u) => !isSpecial(u) && !u.continuous && !isMeasure(u, fields));
      const by = measures.find((u) => !isSpecial(u) && isMeasure(u, fields)) ?? null;
      const use = discrete[discrete.length - 1];
      if (use && (by || measures.length === 0))
        return { use, by, nested: by !== null && discrete.length > 1, caption: fields.get(use.field)?.caption ?? use.field };
    }
    return null;
  })();
  const sortBy = (order: "asc" | "desc") => {
    if (!quickSort) return;
    const { use, by, nested } = quickSort;
    apply((s) => setSort(s, use.field, by ? { kind: "by_field", order, by, nested, manual_values: [] } : { kind: "alphabetic", order, by: null, manual_values: [] }));
  };
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
          <button type="button" title="總計的位置、色彩與「總計使用」" onClick={() => setDialog({ kind: "totals" })}>
            總計…
          </button>
          <button type="button" onClick={() => setDialog({ kind: "title" })}>
            標題…
          </button>
          <label className="inline" title="檢視下方顯示各量值的筆數、總和、平均、最小值、最大值與中位數；選取標記時只算選取的標記（Tableau 的「工作表＞顯示摘要」）">
            <input
              type="checkbox"
              aria-label="顯示摘要"
              checked={sheet.summary_shown === true}
              onChange={(e) => {
                const summary_shown = e.target.checked; // 先取值：存檔是非同步的，之後核取方塊已還原
                apply((s) => ({ ...s, summary_shown }));
              }}
            />{" "}
            摘要
          </label>
          <button type="button" title="檢視下方的說明（Tableau 的「工作表＞顯示說明」）" onClick={() => setDialog({ kind: "caption" })}>
            說明…
          </button>
          <button type="button" onClick={() => setDialog({ kind: "sheet-format" })}>
            格式…
          </button>
          <button type="button" title="這張工作表的摘要資料（每個標記一列）與完整資料（基礎資料列），可以下載 CSV（Tableau 的「檢視資料」）" onClick={() => setViewData(true)}>
            檢視資料…
          </button>
          <button
            type="button"
            title="複製這張工作表的格式（Tableau 的「格式＞複製格式」）"
            onClick={() => {
              copyFormat(sheet);
              setCopied(true);
              notify("已複製格式；到其他工作表按「貼上格式」", "info");
            }}
          >
            複製格式
          </button>
          <button
            type="button"
            disabled={!copied && !hasCopiedFormat()}
            title="套用複製的格式（背景、字型、文字顏色、格線與分隔線、對齊；欄位、篩選與資料不變）"
            onClick={() => apply((s) => pasteFormat(s))}
          >
            貼上格式
          </button>
          <button
            type="button"
            title="這張工作表的格式（背景、字型、顏色、格線與分隔線、對齊、數字格式）回到預設（Tableau 的「工作表＞清除＞格式」）；可以復原"
            onClick={() => apply((s) => clearFormat(s))}
          >
            清除格式
          </button>
          {quickSort && (
            <>
              <button type="button" aria-label="遞增排序" title={`依 ${quickSort.by ? "量值" : "名稱"}由小到大排序「${quickSort.caption}」（Tableau 工具列的排序）`} onClick={() => sortBy("asc")}>
                ↑ 排序
              </button>
              <button type="button" aria-label="遞減排序" title={`依 ${quickSort.by ? "量值" : "名稱"}由大到小排序「${quickSort.caption}」`} onClick={() => sortBy("desc")}>
                ↓ 排序
              </button>
              <button type="button" aria-label="清除排序" disabled={!sheet.sorts.some((s) => s.field === quickSort.use.field)} onClick={() => apply((s) => setSort(s, quickSort.use.field, null))}>
                清除排序
              </button>
            </>
          )}
          <button
            type="button"
            className={showMe ? "active" : undefined}
            aria-pressed={showMe}
            title="依檢視中的欄位建議圖表類型（Tableau 的 Show Me）"
            onClick={() => setShowMe((v) => !v)}
          >
            智慧顯示
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
          <button type="button" title="從工作區中開啟的另一份報表匯入工作表，或整份匯入（Tableau 的「匯入活頁簿」、書籤與在活頁簿之間複製貼上工作表）" onClick={() => setImporting(true)}>
            匯入工作表…
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
            title="新的工作表是這個檢視的文字表（Tableau 的「複製為交叉資料表」）"
            onClick={() =>
              void editApi
                .duplicateAsCrosstab(workbook, sheet.id)
                .then((created) => {
                  onChanged();
                  onOpen(created.id);
                })
                .catch((e: Error) => notify(e.message, "error"))
            }
          >
            複製成交叉資料表
          </button>
          <label className="inline" title="隱藏的工作表只在 dashboard 與 Story 中使用，檢視器的工作表清單不列出（Tableau 的「隱藏工作表」）">
            <input
              type="checkbox"
              aria-label="隱藏工作表"
              checked={sheet.hidden === true}
              onChange={(e) => {
                const hidden = e.target.checked;
                apply((s) => ({ ...s, hidden }));
                window.setTimeout(onChanged, 600);
              }}
            />{" "}
            隱藏
          </label>
          <label className="inline" title="工作表分頁的顏色（清單中以色塊標示）">
            分頁顏色
            <input
              type="color"
              aria-label="分頁顏色"
              value={(sheet.tab_color as string | null | undefined) ?? "#ffffff"}
              onChange={(e) => {
                const color = e.target.value.toLowerCase();
                apply((s) => ({ ...s, tab_color: color === "#ffffff" ? null : color }));
                window.setTimeout(onChanged, 600);
              }}
            />
          </label>
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
        <DataPane onDatasource={changeDatasource} onEditDatasource={onEditDatasource} />
        <div className="cards-pane">
          <Shelf label="頁面" shelf="pages" />
          <FiltersShelf />
          {measureValuesUsed && <MeasureValuesCard />}
          <MarksCard />
          {isMapSheet(sheet) && <MapCard />}
          <AnalyticsCard />
        </div>
        <div className="view-pane">
          <Shelf label="欄" shelf="columns" />
          <Shelf label="列" shelf="rows" />
          {Boolean(stored?.pages) && (
            <PageControl
              workbook={workbook}
              worksheet={sheet.id}
              use={stored!.pages as FieldUse}
              saved={(shown.page as Scalar | undefined) ?? null}
              caption={fields.get((stored!.pages as FieldUse).field)?.caption ?? ""}
              view={{
                sheetFilters: {},
                parameters: {},
                actions: {},
                selections: {},
                pages: shown.page !== null && shown.page !== undefined ? { [sheet.id]: shown.page as Scalar } : {},
              }}
              onView={(next) => {
                const page = next.pages?.[sheet.id];
                if (page !== undefined) apply((s) => ({ ...s, page }));
              }}
            />
          )}
          {Boolean(stored?.pages) &&
            (() => {
              // 頁面的顯示歷程（頁面控制項的「顯示歷程」）：之前各頁的標記（淡化）、軌跡或兩者
              const history = (sheet.page_history as { show: string; length: number | null; fade: boolean } | null | undefined) ?? null;
              const change = (patch: Record<string, unknown> | null) =>
                apply((s) => ({ ...s, page_history: patch === null ? null : { show: "marks", length: null, fade: true, ...((s.page_history as object | null) ?? {}), ...patch, previous: [] } }));
              return (
                <div className="page-history" data-page-history={history ? history.show : "none"}>
                  <label className="inline" title="目前這一頁之外，也畫出之前各頁的標記（淡化）或同一個標記在各頁之間的軌跡（Tableau 頁面控制項的「顯示歷程」）">
                    顯示歷程
                    <select aria-label="顯示歷程" value={history?.show ?? ""} onChange={(e) => change(e.target.value ? { show: e.target.value } : null)}>
                      <option value="">無</option>
                      <option value="marks">標記</option>
                      <option value="trails">軌跡</option>
                      <option value="both">兩者</option>
                    </select>
                  </label>
                  {history && (
                    <>
                      <label className="inline" title="顯示之前幾頁；空白為全部">
                        長度
                        <input
                          type="number"
                          min={1}
                          aria-label="歷程的長度"
                          placeholder="全部"
                          value={history.length ?? ""}
                          onChange={(e) => change({ length: e.target.value ? Math.max(1, Math.round(Number(e.target.value))) : null })}
                        />
                      </label>
                      <label className="inline">
                        <input type="checkbox" aria-label="歷程的標記淡化" checked={history.fade !== false} onChange={(e) => change({ fade: e.target.checked })} /> 淡化
                      </label>
                    </>
                  )}
                </div>
              );
            })()}
          {showMe && <ShowMePanel onClose={() => setShowMe(false)} />}
          <div className="editor-canvas">
            {empty ? (
              <div className="placeholder">將欄位從資料窗格拖曳到欄、列或標記卡</div>
            ) : (
              <>
                {selected.length > 0 && (
                  <SelectionBar
                    marks={selected}
                    onClear={() => setSelected([])}
                    onFilter={(mode) => {
                      const marks = selected;
                      setSelected([]);
                      apply(async (s) => selectionFilter(s, marks, mode, await newId("flt", mode === "include" ? "keep only" : "exclude")) ?? s);
                      notify(mode === "include" ? "已只保留選取的標記" : "已排除選取的標記");
                    }}
                  />
                )}
                <WorksheetView
                  workbook={workbook}
                  worksheet={sheet.id}
                  state={EMPTY_STATE}
                  revision={revision}
                  selected={selected}
                  onMarks={(event) => {
                    if (event.kind !== "click") return;
                    const mark = event.mark;
                    if (!mark || !Object.keys(mark).length) {
                      setSelected([]);
                      return;
                    }
                    const key = JSON.stringify(mark);
                    setSelected((current) => {
                      const has = current.some((m) => JSON.stringify(m) === key);
                      if (event.additive) return has ? current.filter((m) => JSON.stringify(m) !== key) : [...current, mark];
                      return has && current.length === 1 ? [] : [mark];
                    });
                  }}
                  tooltipState={(_target, mark) => ({ ...EMPTY_STATE, tooltip: { source: sheet.id, mark } })}
                  onAnnotationMove={(index, dx, dy) =>
                    // 拖曳註解的文字：改變文字相對於標記或點的位置（區域的註解以對話框調整）
                    apply((s) => ({
                      ...s,
                      annotations: ((s.annotations as { kind: string; offset?: [number, number] }[] | undefined) ?? []).map((a, i) =>
                        i === index && a.kind !== "area" ? { ...a, offset: [(a.offset?.[0] ?? 0) + dx, (a.offset?.[1] ?? 0) + dy] } : a,
                      ),
                    }))
                  }
                  onViewport={(viewport) =>
                    // 與 Tableau Desktop 相同，平移、縮放後的地圖範圍存進工作表（「重設地圖」改回自動縮放）
                    apply((s) => {
                      const current = (s.map as Partial<MapOptionsIR> | null | undefined) ?? null;
                      if (!viewport && !current?.viewport) return s;
                      return {
                        ...s,
                        map: {
                          style: "light",
                          washout: 0,
                          layers: {},
                          hide_unknown: false,
                          ...(current ?? {}),
                          viewport,
                        },
                      };
                    })
                  }
                />
              </>
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
      {viewData && <ViewDataDialog workbook={workbook} sheet={shown} onClose={() => setViewData(false)} />}
      {calc !== undefined && (
        <CalcDialog
          field={calc.field}
          datasource={calc.datasource}
          onClose={() => setCalc(undefined)}
          onSaved={() => {
            void reloadCatalog();
            setRevision((r) => r + 1);
          }}
        />
      )}
      {dialog?.kind === "direction" && <TableCalcDialog tableCalc={dialog.tableCalc} onClose={() => setDialog(null)} />}
      {dialog?.kind === "sort" && <SortDialog use={dialog.use} onClose={() => setDialog(null)} />}
      {dialog?.kind === "annotations" && <AnnotationsDialog mark={dialog.mark} onClose={() => setDialog(null)} />}
      {dialog?.kind === "apply-filter" && <ApplyFilterDialog filter={dialog.filter} onClose={() => setDialog(null)} />}
      {dialog?.kind === "format" && <FormatDialog use={dialog.use} onClose={() => setDialog(null)} />}
      {dialog?.kind === "title" && <TitleDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "sheet-format" && <SheetFormatDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "axis" && <AxisDialog use={dialog.use} shelf={dialog.shelf} onClose={() => setDialog(null)} />}
      {dialog?.kind === "reference" && <ReferenceDialog index={dialog.index} axis={dialog.axis} onClose={() => setDialog(null)} />}
      {dialog?.kind === "trend" && <TrendDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "forecast" && <ForecastDialog onClose={() => setDialog(null)} onSaved={() => void reloadCatalog()} />}
      {dialog?.kind === "cluster" && <ClusterDialog onClose={() => setDialog(null)} onSaved={() => void reloadCatalog()} />}
      {importing && (
        <ImportSheetsDialog
          workbook={workbook}
          onClose={() => setImporting(false)}
          onImported={(first, count) => {
            setImporting(false);
            onChanged();
            notify(`已匯入 ${count} 張工作表`, "info");
            if (first) onOpen(first);
          }}
        />
      )}
      {dialog?.kind === "describe-model" && <DescribeModelDialog kind={dialog.model} onClose={() => setDialog(null)} onSaved={() => void reloadCatalog()} />}
      {dialog?.kind === "colors" && <ColorsDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "tooltip" && <TooltipDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "labels" && <LabelDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "blends" && <BlendsDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "totals" && <TotalsDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "shapes" && <ShapesDialog card={dialog.card} onClose={() => setDialog(null)} />}
      {dialog?.kind === "caption" && <CaptionDialog onClose={() => setDialog(null)} />}
      {dialog?.kind === "hierarchy" && (
        <HierarchyDialog
          datasource={dialog.datasource}
          name={dialog.name}
          field={dialog.field}
          onClose={() => setDialog(null)}
          onSaved={() => {
            void reloadCatalog();
            onChanged();
          }}
        />
      )}
      {dialog?.kind === "geo" && (
        <GeoDialog
          datasource={dialog.datasource}
          field={dialog.field}
          onClose={() => setDialog(null)}
          onSaved={() => {
            void reloadCatalog();
            setRevision((r) => r + 1);
            onChanged();
          }}
        />
      )}
      {dialog && fieldDialog(dialog, () => setDialog(null), () => {
        void reloadCatalog();
        setRevision((r) => r + 1);
        onChanged();
      })}
      {dialog && moreFieldDialog(dialog, () => setDialog(null), () => {
        // 預設屬性與取代參照會改到工作表：重新讀取資料窗格與目前的工作表
        void reloadCatalog();
        reloadSheet();
        onChanged();
      })}
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

/** 編輯器在桌面版本機上：檢視資料不受伺服器的權限限制。 */
const FULL_ACCESS: ViewerAccess = {
  export_image: true,
  export_data: true,
  underlying_data: true,
  download_workbook: true,
  share_view: false,
  view_comments: false,
  add_comment: false,
  store: false,
};

/** 檢視資料（編輯器）：與檢視器相同的對話框，資料是這張工作表目前存檔的定義（摘要與完整資料）。 */
function ViewDataDialog({ workbook, sheet, onClose }: { workbook: string; sheet: SheetIR; onClose: () => void }) {
  const [ir, setIr] = useState<IR | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .ir(workbook)
      .then((loaded) => !cancelled && setIr(loaded))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook]);
  if (error) return <Modal title="檢視資料" onClose={onClose}><p className="error">{error}</p></Modal>;
  if (!ir) return null;
  return (
    <DataDialog
      workbook={workbook}
      ir={ir}
      sheets={[{ id: sheet.id, name: sheet.name }]}
      view={{ sheetFilters: {}, parameters: {}, actions: {}, selections: {} }}
      access={FULL_ACCESS}
      desktop
      onClose={onClose}
    />
  );
}

/** 選取標記後的命令（Tableau 工具提示的「只保留」「排除」「群組」與「建立集合」）。 */
function SelectionBar({
  marks,
  onClear,
  onFilter,
}: {
  marks: Mark[];
  onClear: () => void;
  onFilter: (mode: "include" | "exclude") => void;
}) {
  const editor = useEditor();
  const levels = selectionLevels(editor.sheet, marks);
  const plain = levels?.every((l) => l.use.date === null) ?? false;
  const fields = (levels ?? []).map((l) => editor.fields.get(l.use.field));
  const groupable = plain && levels?.length === 1 && fields[0]?.kind !== "set";
  const datasource = editor.sheet.datasource;
  const values = (key: string) => {
    const found: Scalar[] = [];
    for (const m of marks) if (!found.some((v) => v === m[key])) found.push(m[key] ?? null);
    return found;
  };
  return (
    <div className="selection-bar" role="toolbar" aria-label="選取的標記">
      <span className="hint">已選取 {marks.length} 個標記</span>
      <button type="button" disabled={!levels} title="只保留選取的標記（加上篩選）" onClick={() => onFilter("include")}>
        ✓ 只保留
      </button>
      <button type="button" disabled={!levels} title="排除選取的標記（加上篩選）" onClick={() => onFilter("exclude")}>
        ✕ 排除
      </button>
      <button
        type="button"
        disabled={marks.length !== 1}
        title={marks.length === 1 ? "在選取的標記上加註解" : "選取一個標記時可以加上標記的註解"}
        onClick={() => editor.openDialog({ kind: "annotations", mark: marks[0] })}
      >
        💬 註解…
      </button>
      <button
        type="button"
        disabled={!groupable || !levels}
        title={groupable ? "以選取的成員建立群組" : "只有一個維度（不是日期的部分）時可以建立群組"}
        onClick={() =>
          levels &&
          editor.openDialog({ kind: "group", datasource, source: levels[0].use.field, field: null, members: values(levels[0].key) })
        }
      >
        📎 群組…
      </button>
      <button
        type="button"
        disabled={!plain || !levels}
        title={plain ? "以選取的成員建立集合" : "日期的部分不能建立集合"}
        onClick={() =>
          levels &&
          editor.openDialog({
            kind: "set",
            datasource,
            levels: levels.map((l) => l.use.field),
            field: null,
            members: marks.map((m) => levels.map((l) => m[l.key] ?? null)),
          })
        }
      >
        ◎ 建立集合…
      </button>
      <button type="button" className="link" onClick={onClear}>
        清除選取
      </button>
    </div>
  );
}

/** 資料窗格的欄位選單開啟的對話框（重新命名、註解、格式、別名、群組、數值分組、集合）。 */
function fieldDialog(dialog: EditorDialog, onClose: () => void, onSaved: () => void): React.ReactNode {
  switch (dialog.kind) {
    case "rename":
      return <RenameDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "comment":
      return <CommentDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "field-format":
      return <FieldFormatDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "aliases":
      return <AliasDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "combined":
      return <CombinedDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "fiscal":
      return <FiscalDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "folder":
      return <FolderDialog datasource={dialog.datasource} fields={dialog.fields} folder={dialog.folder} onClose={onClose} onSaved={onSaved} />;
    case "date-properties":
      return <DatePropertiesDialog datasource={dialog.datasource} onClose={onClose} onSaved={onSaved} />;
    case "group":
      return (
        <GroupDialog
          datasource={dialog.datasource}
          source={dialog.source}
          field={dialog.field}
          members={dialog.members}
          onClose={onClose}
          onSaved={onSaved}
        />
      );
    case "bins":
      return <BinDialog datasource={dialog.datasource} source={dialog.source} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "set":
      return (
        <SetDialog
          datasource={dialog.datasource}
          levels={dialog.levels}
          field={dialog.field}
          members={dialog.members}
          combine={dialog.combine}
          onClose={onClose}
          onSaved={onSaved}
        />
      );
    default:
      return null;
  }
}
