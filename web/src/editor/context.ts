// 編輯器的共用狀態（工作表、資料窗格、目前的標記卡）與拖放中的項目。

import { createContext, useContext } from "react";
import type React from "react";
import type { FieldUse } from "../types";
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
}

export type EditorDialog =
  | { kind: "direction"; tableCalc: string }
  | { kind: "format"; use: FieldUse }
  | { kind: "title" }
  | { kind: "sheet-format" }
  | { kind: "axis"; use: FieldUse; shelf: "rows" | "columns" }
  | { kind: "reference"; index: number | null; axis: FieldUse | null }
  | { kind: "trend"; card: Card }
  | { kind: "parameter"; parameter: string | null }
  /** 建立階層（name 為 null，field 為第一個欄位）或改變既有的階層 */
  | { kind: "hierarchy"; datasource: string; name: string | null; field?: string }
  | { kind: "colors"; card: Card };

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
