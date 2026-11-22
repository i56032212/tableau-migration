// 整份報表的結構歷程：刪除工作表、dashboard、Story 可以復原與重做（Tableau：刪除工作表之後按 Ctrl+Z，
// 它原樣回到原本的位置）。
//
// 編輯的歷程是每個分頁各自的（工作表在 Editor.tsx、dashboard 與 Story 在 history.ts），換分頁就重來；刪除的是
// 整個分頁，所以記在這裡：每份報表一疊「刪除的紀錄」（引擎刪除時回傳的復原用紀錄，見 kernel 的
// tmig.sheet_undo），放在模組裡，換分頁不會不見。關閉報表或重新載入頁面之後就沒有了（與 Tableau 相同，
// 復原只在這次開啟的期間）。
//
// 復原、重做的順序（useUndoRedo，工具列的按鈕與 Ctrl+Z、Ctrl+Y 共用）：
// - 復原：先是目前這個分頁自己的歷程；它沒有可以復原的了，就復原最近一次刪除。刪除別的分頁時人還留在
//   目前的分頁——那次刪除是最近的一步，所以先復原它，再輪到這個分頁之前的變更（刪除時記下這個分頁的歷程
//   走到第幾步）
// - 重做：先是這個分頁自己的歷程，否則重做剛剛復原的刪除。復原之後只要再改過報表就不能重做（與 Tableau
//   相同）：編輯器改動時清掉（edited），引擎另外以報表內容的指紋把關
// - 復原之後編輯器開啟放回來的分頁

import { useEffect, useRef, useSyncExternalStore } from "react";
import { errorText } from "../api";
import { RESET_EVENT, toast } from "../desktop";
import { editApi, type RemovedSheet } from "./api";
import type { SheetKind, TabKind, WorkspaceInfo } from "./Workspace";

/** 分頁自己的歷程走到哪裡：mount 是這次開啟編輯器的編號（換分頁就是新的），index 是第幾步。 */
interface Position {
  mount: number;
  index: number;
}

interface Deletion {
  /** 這次刪除的編號（刪除訊息上的「復原」以它指定是哪一次） */
  seq: number;
  kind: SheetKind;
  id: string;
  name: string;
  /** 引擎回傳的復原用紀錄 */
  removed: RemovedSheet;
  /** 刪除時人留在別的分頁：那個分頁的歷程當時的位置（回到這個位置時先復原這次刪除） */
  at: Position | null;
}

interface Redo {
  kind: SheetKind;
  id: string;
  name: string;
  /** 復原之後報表內容的指紋：報表又改過就不重做 */
  state: string;
}

interface Stacks {
  undo: Deletion[];
  redo: Redo[];
}

/** 一份報表最多記幾次刪除 */
const LIMIT = 50;
const EMPTY: Stacks = { undo: [], redo: [] };
const stacks = new Map<string, Stacks>();
const listeners = new Set<() => void>();

function update(workbook: string, change: (current: Stacks) => Stacks): void {
  const current = stacks.get(workbook) ?? EMPTY;
  const next = change(current);
  if (next === current) return;
  stacks.set(workbook, next);
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => void listeners.delete(notify);
}

// 報表關閉了：這次開啟期間的紀錄不再有效（同一個檔案再開啟時 id 相同）
window.addEventListener(RESET_EVENT, (event) => {
  stacks.delete((event as CustomEvent<string>).detail);
  listeners.forEach((notify) => notify());
});

/** 目前開著的編輯器（一次只有一個）：報表的資訊與導覽、它的分頁、它自己的歷程的位置。 */
interface Active {
  ws: WorkspaceInfo;
  current: { kind: TabKind; id: string };
  /** 沒有分頁可以開的時候開啟的資料源 */
  source: string | null;
  position: Position | null;
  mount: number;
}
let active: Active | null = null;
/** 各份報表最近一次的資訊與導覽（離開編輯器之後，刪除訊息上的「復原」還用得到） */
const workspaces = new Map<string, WorkspaceInfo>();
let mounts = 0;
let deletions = 0;
/** 復原或重做進行中（等引擎回應）：不重複送出 */
let busy = false;
/** 刪除之後的訊息（上面有「復原」）；一次只有一則 */
let notice: HTMLElement | null = null;

const KIND_LABELS: Record<SheetKind, string> = { worksheet: "工作表", dashboard: " dashboard", story: " Story" };
const label = (item: { kind: SheetKind; name: string }) => `${KIND_LABELS[item.kind]}「${item.name}」`;

function closeNotice(): void {
  notice?.remove();
  notice = null;
}

/** 編輯器改動了報表：剛剛復原的刪除不能再重做。 */
export function edited(workbook: string): void {
  update(workbook, (current) => (current.redo.length > 0 ? { ...current, redo: [] } : current));
}

const REMOVE = {
  worksheet: editApi.removeWorksheet,
  dashboard: editApi.removeDashboard,
  story: editApi.removeStory,
};

/**
 * 刪除工作表、dashboard 或 Story。expect：重做時帶上復原之後的指紋。成功後記下復原用的紀錄；刪除的是目前的
 * 分頁時留在編輯器，開啟左邊的分頁（它是第一個時開啟右邊的；都沒有時開啟資料來源）。
 */
async function remove(
  ws: WorkspaceInfo,
  tab: { kind: SheetKind; id: string; name: string },
  expect: string | null,
  fail: (message: string) => void,
): Promise<void> {
  const here = active !== null && active.ws.workbook === ws.workbook ? active : null;
  const isCurrent = here !== null && here.current.kind === tab.kind && here.current.id === tab.id;
  const at = ws.sheets.findIndex((sheet) => sheet.kind === tab.kind && sheet.id === tab.id);
  const neighbour = at < 0 ? null : (ws.sheets[at - 1] ?? ws.sheets[at + 1] ?? null);
  const source = here?.source ?? ws.datasources[0]?.id ?? null;
  let removed: RemovedSheet;
  try {
    removed = (await REMOVE[tab.kind](ws.workbook, tab.id, expect ?? undefined)).removed;
  } catch (e) {
    fail(errorText(e));
    return;
  }
  const deletion: Deletion = {
    seq: ++deletions,
    kind: tab.kind,
    id: tab.id,
    name: tab.name,
    removed,
    at: isCurrent ? null : (here?.position ?? null),
  };
  update(ws.workbook, (current) => ({
    undo: [...current.undo, deletion].slice(-LIMIT),
    // 新的刪除之後不能再重做之前復原的；重做的那一筆呼叫之前已經拿掉
    redo: expect === null ? [] : current.redo,
  }));
  if (isCurrent) {
    if (neighbour) ws.open(neighbour.kind, neighbour.id);
    else if (source) ws.open("datasource", source);
    else ws.view(tab.kind, null);
  }
  ws.changed();
  closeNotice();
  // 刪除不先問（與 Tableau 相同）：訊息上直接可以復原
  notice = toast(`已刪除${label(tab)}`, "info", {
    label: "復原",
    title: "復原刪除（Ctrl+Z）",
    run: () => void undoDelete(ws.workbook, deletion.seq),
  });
}

/** 刪除工作表、dashboard 或 Story（分頁的右鍵選單、Story 的功能表）。可以復原：Ctrl+Z、工具列的「復原」，
 * 或刪除之後右下角訊息上的「復原」。 */
export function removeSheet(
  ws: WorkspaceInfo,
  tab: { kind: SheetKind; id: string; name: string },
  fail: (message: string) => void,
): Promise<void> {
  return remove(ws, tab, null, fail);
}

/**
 * 復原刪除（seq 省略時是最近一次；刪除訊息上的「復原」指定那一次）：分頁回到原本的位置，編輯器開啟它。
 * 放不回去時（例如之後把別的工作表改成了同一個名稱）說明原因，紀錄留著——排除原因之後可以再復原。
 */
export async function undoDelete(workbook: string, seq?: number): Promise<void> {
  const ws = workspaces.get(workbook);
  const pending = (stacks.get(workbook) ?? EMPTY).undo;
  const target = seq === undefined ? pending[pending.length - 1] : pending.find((item) => item.seq === seq);
  if (!ws || !target || busy) return;
  busy = true;
  try {
    const restored = await editApi.restoreSheet(workbook, target.removed);
    update(workbook, (current) => ({
      undo: current.undo.filter((item) => item.seq !== target.seq),
      redo: [...current.redo, { kind: target.kind, id: target.id, name: target.name, state: restored.state }],
    }));
    closeNotice();
    ws.changed();
    ws.open(target.kind, target.id);
    toast(`已復原${label(target)}`);
  } catch (e) {
    // 這一筆不再搶在分頁自己的歷程之前（否則按 Ctrl+Z 會一直停在這裡）
    update(workbook, (current) => ({
      ...current,
      undo: current.undo.map((item) => (item.seq === target.seq ? { ...item, at: null } : item)),
    }));
    toast(errorText(e), "error");
  } finally {
    busy = false;
  }
}

/** 重做剛剛復原的刪除（復原之後報表又改過時引擎不刪，說明原因）。 */
export async function redoDelete(workbook: string): Promise<void> {
  const ws = workspaces.get(workbook);
  const pending = (stacks.get(workbook) ?? EMPTY).redo;
  const target = pending[pending.length - 1];
  if (!ws || !target || busy) return;
  busy = true;
  // 不論成不成功都拿掉：不成功表示報表已經不是復原之後的樣子
  update(workbook, (current) => ({ ...current, redo: current.redo.filter((item) => item !== target) }));
  try {
    await remove(ws, target, target.state, (message) => toast(message, "error"));
  } finally {
    busy = false;
  }
}

/** 分頁自己的歷程（工作表、dashboard、Story 的編輯器；資料來源分頁沒有，傳 null）。 */
export interface OwnHistory {
  /** 目前在第幾步（0 是開啟時的樣子） */
  index: number;
  /** 一共幾步（含開啟時的樣子） */
  length: number;
  undo: () => void;
  redo: () => void;
}

export interface UndoRedo {
  canUndo: boolean;
  canRedo: boolean;
  undo: () => void;
  redo: () => void;
  /** 工具列按鈕的提示文字：下一步復原、重做的是什麼 */
  undoTitle: string;
  redoTitle: string;
}

/**
 * 編輯器的復原與重做：分頁自己的歷程加上整份報表刪除分頁的紀錄（順序見這個檔案開頭的說明）。工具列的按鈕
 * 用回傳的 undo、redo；Ctrl+Z、Ctrl+Y（Ctrl+Shift+Z）也在這裡處理。
 */
export function useUndoRedo(
  ws: WorkspaceInfo,
  current: { kind: TabKind; id: string },
  own: OwnHistory | null,
  source?: string | null,
): UndoRedo {
  const mount = useRef(0);
  if (mount.current === 0) mount.current = ++mounts;
  const workbook = ws.workbook;
  const stack = useSyncExternalStore(subscribe, () => stacks.get(workbook) ?? EMPTY);
  const index = own?.index ?? null;

  // 刪除分頁時要知道目前是哪個編輯器、它的歷程走到哪裡
  useEffect(() => {
    workspaces.set(workbook, ws);
    active = {
      ws,
      current,
      source: source ?? null,
      position: index === null ? null : { mount: mount.current, index },
      mount: mount.current,
    };
  });
  useEffect(
    () => () => {
      if (active?.mount === mount.current) active = null;
    },
    [],
  );

  const deletion = stack.undo[stack.undo.length - 1];
  const again = stack.redo[stack.redo.length - 1];
  const ownUndo = own !== null && own.index > 0;
  const ownRedo = own !== null && own.index < own.length - 1;
  // 最近一步是在這個分頁的這一步之後刪除了別的分頁：先復原那次刪除
  const deletionFirst =
    deletion !== undefined && deletion.at !== null && deletion.at.mount === mount.current && deletion.at.index === index;
  const undoesDeletion = deletion !== undefined && (deletionFirst || !ownUndo);
  const redoesDeletion = again !== undefined && !ownRedo;
  const undo = () => {
    if (undoesDeletion) void undoDelete(workbook);
    else if (ownUndo) own.undo();
  };
  const redo = () => {
    if (ownRedo) own.redo();
    else if (redoesDeletion) void redoDelete(workbook);
  };

  const keys = useRef({ undo, redo });
  keys.current = { undo, redo };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      // 焦點在可以輸入的地方（輸入框、下拉選單、格式文字的方塊）時，Ctrl+Z、Ctrl+Y 是那個方塊自己的復原；
      // 開著對話框時也不動對話框後面的東西——否則打字打到一半按 Ctrl+Z，後面的工作表被退回一步，
      // 復原的若是刪除分頁，編輯器還會換到別的分頁、對話框裡輸入的內容跟著不見
      const target = e.target as HTMLElement | null;
      if (target?.closest?.("input, textarea, select") || target?.isContentEditable) return;
      if (document.querySelector(".modal-backdrop")) return;
      const key = e.key.toLowerCase();
      if (key === "z" && !e.shiftKey) {
        e.preventDefault();
        keys.current.undo();
      } else if (key === "y" || (key === "z" && e.shiftKey)) {
        e.preventDefault();
        keys.current.redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return {
    canUndo: ownUndo || deletion !== undefined,
    canRedo: ownRedo || again !== undefined,
    undo,
    redo,
    undoTitle: undoesDeletion ? `復原刪除${label(deletion)}（Ctrl+Z）` : "復原（Ctrl+Z）",
    redoTitle: redoesDeletion ? `重做刪除${label(again)}（Ctrl+Y）` : "重做（Ctrl+Y）",
  };
}
