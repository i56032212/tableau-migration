// 編輯器的共用狀態（工作表、資料窗格、目前的標記卡）與拖放中的項目。

import { createContext, useContext } from "react";
import type React from "react";
import type { FieldUse, Scalar } from "../types";
import type { Card, Catalog, CatalogField, ShelfName, SheetIR } from "./model";

/** 篩選對話框的對象：新的篩選（index 為 null）或修改第 index 個篩選。 */
export interface FilterTarget {
  use: FieldUse;
  index: number | null;
}

export interface EditorContextValue {
  workbook: string;
  sheet: SheetIR;
  catalog: Catalog;
  fields: Map<string, CatalogField>;
  card: Card;
  setCard: (card: Card) => void;
  /** 套用變更：整理（排序、Table Calculation 的定址等）後存回，可以復原 */
  apply: (change: (sheet: SheetIR) => SheetIR | Promise<SheetIR>) => void;
  /** 欄位放到架上之前：Table Calculation 計算欄位需要一個公式的 Table Calculation */
  prepare: (sheet: SheetIR, use: FieldUse) => Promise<[SheetIR, FieldUse]>;
  newId: (prefix: string, text: string) => Promise<string>;
  openFilter: (target: FilterTarget) => void;
  /** 建立（field 為 null）或編輯計算欄位；datasource 為計算欄位所屬的資料源，省略時為工作表的資料源 */
  openCalc: (field: CatalogField | null, datasource?: string) => void;
  openDialog: (dialog: EditorDialog) => void;
  notify: (message: string, kind?: "info" | "error") => void;
  /** 資料窗格的內容改變後（計算欄位、階層）重新讀取 */
  reloadCatalog: () => void;
  /** 重新讀取資料窗格並等它完成（之後的 apply 看得到新的欄位）；回傳這張工作表資料源的欄位 */
  refreshCatalog: () => Promise<Map<string, CatalogField>>;
  /** 伺服器端改了工作表之後（預設屬性、取代參照）重新讀取目前的工作表 */
  reloadSheet: () => void;
}

export type EditorDialog =
  /** 表計算：計算類型與選項、計算方向、計算層級與重新開始間隔 */
  | { kind: "direction"; tableCalc: string }
  /** 排序：資料來源順序、字母、依欄位或手動 */
  | { kind: "sort"; use: FieldUse }
  /** 編輯混合關係：Data Blending 的次要資料源各自的連結欄位 */
  | { kind: "blends" }
  | { kind: "totals" }
  | { kind: "shapes"; card: Card }
  /** 篩選套用到其他工作表（選取的工作表、使用此資料源的所有工作表） */
  | { kind: "apply-filter"; filter: string }
  /** 註解：mark 為檢視上選取的標記（維度的欄位鍵 → 值） */
  | { kind: "annotations"; mark: Record<string, Scalar> | null }
  | { kind: "format"; use: FieldUse }
  | { kind: "title" }
  | { kind: "caption" }
  | { kind: "sheet-format" }
  | { kind: "axis"; use: FieldUse; shelf: "rows" | "columns" }
  | { kind: "reference"; index: number | null; axis: FieldUse | null }
  | { kind: "trend"; card: Card }
  | { kind: "forecast" }
  | { kind: "cluster" }
  | { kind: "parameter"; parameter: string | null }
  /** 建立階層（name 為 null，field 為第一個欄位）或改變既有的階層 */
  | { kind: "hierarchy"; datasource: string; name: string | null; field?: string }
  | { kind: "colors"; card: Card }
  | { kind: "tooltip"; card: Card }
  /** 標籤：顯示與否、自訂的標籤文字、要標示的標記、允許重疊 */
  | { kind: "labels"; card: Card }
  /** 欄位的地理角色與編輯位置 */
  | { kind: "geo"; datasource: string; field: string }
  /** 資料窗格的欄位選單：重新命名、註解、預設格式、別名 */
  | { kind: "rename" | "comment" | "field-format" | "aliases"; datasource: string; field: string }
  /** 建立（field 為 null）或編輯群組；members 為從檢視上選取的成員 */
  | { kind: "group"; datasource: string; source: string; field: string | null; members?: Scalar[] }
  /** 建立或編輯數值分組 */
  | { kind: "bins"; datasource: string; source: string; field: string | null }
  /** 建立或編輯集合；members 為從檢視上選取的成員（各層級的值組合） */
  | { kind: "set"; datasource: string; levels: string[]; field: string | null; members?: Scalar[][]; combine?: string }
  /** 合併欄位（field 為先選的維度）、日期欄位的會計年度起始月 */
  | { kind: "combined" | "fiscal"; datasource: string; field: string }
  /** 建立資料夾（fields 移進去）或改名（folder 為現有的資料夾） */
  | { kind: "folder"; datasource: string; fields: string[]; folder: string | null }
  /** 資料源的日期屬性 */
  | { kind: "date-properties"; datasource: string }
  /** 預設屬性的色彩、形狀、排序；分割與自訂分割、取代參照、描述欄位 */
  | { kind: "default-color" | "default-shapes" | "default-sort" | "split" | "replace-references" | "describe"; datasource: string; field: string };

export const EditorContext = createContext<EditorContextValue | null>(null);

export function useEditor(): EditorContextValue {
  const value = useContext(EditorContext);
  if (!value) throw new Error("useEditor 必須在編輯器中使用");
  return value;
}

/** 拖放中的項目：資料窗格的欄位，或架上的膠囊。 */
export type DragItem =
  | { kind: "field"; field: string }
  | { kind: "pill"; shelf: ShelfName; index: number; card: Card };

let dragging: DragItem | null = null;

export function startDrag(event: React.DragEvent, item: DragItem): void {
  dragging = item;
  event.dataTransfer.effectAllowed = "copyMove";
  // 有些瀏覽器沒有資料就不開始拖曳；內容以模組變數為準（dragover 時讀不到 dataTransfer）
  event.dataTransfer.setData("text/plain", item.kind === "field" ? item.field : `${item.shelf}:${item.index}`);
  event.stopPropagation();
}

export function currentDrag(): DragItem | null {
  return dragging;
}

export function endDrag(): void {
  dragging = null;
}
