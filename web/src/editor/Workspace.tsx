// 編輯器共用的工作區外框（排列與 Tableau Desktop 相同）：上方的功能表列與圖示工具列、
// 下方的分頁列（資料來源、工作表、dashboard、Story）與狀態列。各編輯器把自己的命令放進功能表與工具列。

import type React from "react";
import { createContext, useContext, useRef, useState } from "react";
import { addDatasource, closeDocument, connectServer, newWorkbook, openDialog, openDocument, saveDocument, toast } from "../desktop";
import { Modal } from "../Server";
import type { Dashboard, Story } from "../types";
import { editApi } from "./api";
import { Icon, type IconName } from "./Icons";
import type { SheetIR } from "./model";
import { type MenuItem, PopupMenu, SEPARATOR } from "./Shelves";

export type SheetKind = "worksheet" | "dashboard" | "story";
export type TabKind = SheetKind | "datasource";

export interface SheetTab {
  kind: SheetKind;
  id: string;
  name: string;
  hidden?: boolean;
  color?: string | null;
}

/** 報表層級的資訊與導覽（由 App 提供給各編輯器的外框）。 */
export interface WorkspaceInfo {
  workbook: string;
  /** 報表的名稱 */
  name: string;
  datasources: { id: string; caption: string }[];
  /** 分頁：工作表、dashboard、Story（各依報表中的順序） */
  sheets: SheetTab[];
  /** 不支援的元素筆數 */
  unsupported: number;
  /** 功能表列右側的使用者名稱 */
  account: React.ReactNode;
  /** 開啟另一個分頁的編輯器 */
  open: (kind: TabKind, id: string) => void;
  /** 完成編輯，回到檢視；id 為 null 時回到報表（例如刪除之後） */
  view: (kind: TabKind, id: string | null) => void;
  navigate: (to: string) => void;
  /** 報表的定義改變了：重新載入 */
  changed: () => void;
}

export const WorkspaceContext = createContext<WorkspaceInfo | null>(null);

export function useWorkspace(): WorkspaceInfo {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("工作區的外框要在 WorkspaceContext 之內");
  return value;
}

const KIND_NAMES: Record<SheetKind, string> = { worksheet: "工作表", dashboard: "dashboard", story: "Story" };

// ---------------------------------------------------------------- 共用的功能表

export interface MenuDef {
  label: string;
  items: MenuItem[];
}

/** 「檔案」：開啟、儲存、另存新檔、關閉；extra 是各編輯器自己的項目（例如匯入工作表）。 */
export function fileMenu(ws: WorkspaceInfo, extra: MenuItem[] = []): MenuDef {
  return {
    label: "檔案",
    items: [
      { label: "新增報表（連線到資料）…", action: () => void newWorkbook(ws.navigate) },
      { label: "新增報表（連線到伺服器）…", action: () => connectServer(null) },
      { label: "開啟檔案…", action: () => void openDocument(ws.navigate) },
      SEPARATOR,
      { label: "儲存", action: () => void saveDocument(ws.workbook, false) },
      { label: "另存新檔…", action: () => void saveDocument(ws.workbook, true) },
      ...(extra.length > 0 ? [SEPARATOR, ...extra] : []),
      SEPARATOR,
      { label: "報表清單", action: () => ws.navigate("/") },
      { label: "關閉", action: () => void closeDocument(ws.workbook, ws.navigate) },
    ],
  };
}

/** 「資料」開頭共用的項目：新增資料源、連線到伺服器，以及各資料源的分頁。 */
export function dataMenuItems(ws: WorkspaceInfo, current?: string | null): MenuItem[] {
  return [
    { label: "新增資料源…", action: () => void addDatasource(ws.workbook, ws.navigate) },
    { label: "連線到伺服器…", action: () => connectServer(ws.workbook) },
    ...(ws.datasources.length > 0 ? [SEPARATOR] : []),
    ...ws.datasources.map((d) => ({
      label: `資料源：${d.caption}`,
      checked: current === d.id,
      action: () => ws.open("datasource", d.id),
    })),
  ];
}

/** 「伺服器」：登入、從伺服器開啟、發布報表與資料源。 */
export function serverMenu(ws: WorkspaceInfo): MenuDef {
  return {
    label: "伺服器",
    items: [
      { label: "登入…", action: () => openDialog({ kind: "sign-in" }) },
      { label: "從伺服器開啟…", action: () => openDialog({ kind: "open-server" }) },
      SEPARATOR,
      { label: "發布…", action: () => openDialog({ kind: "publish", workbook: ws.workbook }) },
      { label: "發布資料源…", action: () => openDialog({ kind: "publish-datasource", workbook: ws.workbook }) },
    ],
  };
}

/** 「視窗」：完成編輯（回到檢視）與各分頁。 */
export function windowMenu(ws: WorkspaceInfo, current: { kind: TabKind; id: string }, onDone: () => void): MenuDef {
  return {
    label: "視窗",
    items: [
      { label: "完成編輯（回到檢視）", action: onDone },
      ...(ws.sheets.length > 0 ? [SEPARATOR] : []),
      ...ws.sheets.map((tab) => ({
        label: `${KIND_NAMES[tab.kind]}：${tab.name}`,
        checked: tab.kind === current.kind && tab.id === current.id,
        action: () => ws.open(tab.kind, tab.id),
      })),
    ],
  };
}

/** 新增工作表、dashboard、Story（功能表、工具列與分頁列共用）；完成後開啟新的分頁。 */
export function useCreateSheet(onError: (message: string) => void) {
  const ws = useWorkspace();
  return (kind: SheetKind, options: { datasource?: string | null; size?: { width: number; height: number } } = {}) => {
    const created =
      kind === "worksheet"
        ? editApi.addWorksheet(ws.workbook, undefined, options.datasource ?? undefined)
        : kind === "dashboard"
          ? editApi.addDashboard(ws.workbook, undefined, options.size?.width, options.size?.height)
          : editApi.addStory(ws.workbook);
    void created
      .then((item) => {
        ws.changed();
        ws.open(kind, item.id);
      })
      .catch((e: Error) => onError(e.message));
  };
}

// ---------------------------------------------------------------- 功能表列

/** 功能表列：按一下開啟選單，選單開著時滑到另一個功能表就換過去。右側是報表名稱、狀態與「完成」。 */
export function MenuBar({ menus, saving, onDone }: { menus: MenuDef[]; saving: boolean; onDone: () => void }) {
  const ws = useWorkspace();
  const [open, setOpen] = useState<{ index: number; anchor: DOMRect } | null>(null);
  // 選單開著時再按同一個功能表：選單先因為按在外面而關閉，接著的 click 不再打開
  const wasOpen = useRef(false);
  const shown = menus.filter((menu) => menu.items.length > 0);
  return (
    <div className="menu-bar">
      <div className="menu-bar-items" role="menubar" aria-label="功能表">
        {shown.map((menu, index) => (
          <button
            key={menu.label}
            type="button"
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={open?.index === index}
            className={open?.index === index ? "open" : undefined}
            onMouseDown={() => {
              wasOpen.current = open?.index === index;
            }}
            onClick={(e) => {
              const toggledOff = wasOpen.current;
              wasOpen.current = false;
              if (!toggledOff) setOpen({ index, anchor: e.currentTarget.getBoundingClientRect() });
            }}
            onMouseEnter={(e) => {
              if (open && open.index !== index) setOpen({ index, anchor: e.currentTarget.getBoundingClientRect() });
            }}
          >
            {menu.label}
          </button>
        ))}
      </div>
      <span className="menu-bar-title" title="報表">
        {ws.name}
      </span>
      <span className="spacer" />
      {ws.unsupported > 0 && <span className="error">不支援 {ws.unsupported} 筆</span>}
      {saving && <span className="hint">儲存中…</span>}
      {ws.account}
      <button type="button" className="primary" title="完成編輯，回到檢視" onClick={onDone}>
        完成
      </button>
      {open && shown[open.index] && (
        <PopupMenu
          key={open.index}
          anchor={open.anchor}
          items={shown[open.index].items}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 工具列

/** 工具列的圖示按鈕：名稱在 aria-label 與提示文字。 */
export function ToolButton({
  icon,
  label,
  title,
  onClick,
  disabled,
  pressed,
}: {
  icon: IconName;
  label: string;
  title?: string;
  onClick: (event: React.MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  pressed?: boolean;
}) {
  return (
    <button
      type="button"
      className={`tool-button${pressed ? " active" : ""}`}
      aria-label={label}
      aria-pressed={pressed}
      title={title ?? label}
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name={icon} />
    </button>
  );
}

/** 按下去開啟選單的工具列按鈕（例如「清除」）。 */
export function ToolMenuButton({ icon, label, title, items }: { icon: IconName; label: string; title?: string; items: MenuItem[] }) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  return (
    <>
      <button
        type="button"
        className={`tool-button with-caret${anchor ? " active" : ""}`}
        aria-label={label}
        aria-haspopup="menu"
        title={title ?? label}
        onClick={(e) => setAnchor(e.currentTarget.getBoundingClientRect())}
      >
        <Icon name={icon} />
        <Icon name="caret" size={10} />
      </button>
      {anchor && <PopupMenu anchor={anchor} items={items} onClose={() => setAnchor(null)} />}
    </>
  );
}

// ---------------------------------------------------------------- 分頁列

const NAME_LABELS: Record<SheetKind, string> = { worksheet: "工作表名稱", dashboard: "dashboard 名稱", story: "Story 名稱" };

type Patch = (value: Record<string, unknown>) => Record<string, unknown>;

/** 底部的分頁列：最左邊是資料來源，接著每張工作表、dashboard、Story 一個分頁，最後是三個新增的按鈕。
 * 按一下切換，連按兩下重新命名，按右鍵是這個分頁的命令。 */
export function SheetTabs({
  current,
  live,
  apply,
  datasource,
  dashboardSize,
  onError,
}: {
  current: { kind: TabKind; id: string };
  /** 目前這個分頁在編輯器中的名稱、隱藏與顏色（復原、重做之後報表的定義還沒重新載入時以它為準） */
  live?: { name: string; hidden?: boolean; color?: string | null };
  /** 改變目前這個分頁的定義（經過編輯器，可以復原）；其他分頁直接讀出、修改、存回 */
  apply?: (change: Patch) => void;
  /** 「資料來源」分頁開啟的資料源、新工作表使用的資料源（預設為第一個） */
  datasource?: string | null;
  /** 新的 dashboard 的大小（dashboard 編輯器：與目前的相同） */
  dashboardSize?: { width: number; height: number };
  onError?: (message: string) => void;
}) {
  const ws = useWorkspace();
  const [menu, setMenu] = useState<{ tab: SheetTab | null; anchor: DOMRect } | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [coloring, setColoring] = useState<{ tab: SheetTab; color: string } | null>(null);
  const fail = (message: string) => (onError ? onError(message) : toast(message, "error"));
  const create = useCreateSheet(fail);
  const key = (tab: { kind: TabKind; id: string }) => `${tab.kind}:${tab.id}`;
  const isCurrent = (tab: SheetTab) => tab.kind === current.kind && tab.id === current.id;
  const tabs = ws.sheets.map((tab) => (live && isCurrent(tab) ? { ...tab, ...live } : tab));
  const source = datasource ?? (current.kind === "datasource" ? current.id : null) ?? ws.datasources[0]?.id ?? null;

  /** 改變一個分頁的定義：目前的分頁經過編輯器（可以復原），其他的直接存回。 */
  const patch = (tab: SheetTab, change: Patch, reload = true) => {
    if (isCurrent(tab) && apply) {
      apply(change);
      // 與原來相同：存檔完成後重新載入報表的定義（分頁的名稱、顏色與隱藏）
      if (reload) window.setTimeout(ws.changed, 600);
      return;
    }
    const saved =
      tab.kind === "worksheet"
        ? editApi.worksheet(ws.workbook, tab.id).then((s) => editApi.saveWorksheet(ws.workbook, change(s as unknown as Record<string, unknown>) as unknown as SheetIR))
        : tab.kind === "dashboard"
          ? editApi.dashboard(ws.workbook, tab.id).then((d) => editApi.saveDashboard(ws.workbook, change(d as unknown as Record<string, unknown>) as unknown as Dashboard))
          : editApi.story(ws.workbook, tab.id).then((s) => editApi.saveStory(ws.workbook, change(s as unknown as Record<string, unknown>) as unknown as Story));
    void saved.then(() => ws.changed()).catch((e: Error) => fail(e.message));
  };
  const opened = (item: Promise<{ id: string }>, kind: SheetKind) =>
    void item
      .then((made) => {
        ws.changed();
        ws.open(kind, made.id);
      })
      .catch((e: Error) => fail(e.message));
  const remove = (tab: SheetTab) => {
    if (!window.confirm(`刪除${tab.kind === "worksheet" ? "工作表" : ` ${KIND_NAMES[tab.kind]}`}「${tab.name}」？`)) return;
    const removed =
      tab.kind === "worksheet"
        ? editApi.removeWorksheet(ws.workbook, tab.id)
        : tab.kind === "dashboard"
          ? editApi.removeDashboard(ws.workbook, tab.id)
          : editApi.removeStory(ws.workbook, tab.id);
    void removed
      .then(() => {
        ws.changed();
        if (isCurrent(tab)) ws.view(tab.kind, null);
      })
      .catch((e: Error) => fail(e.message));
  };
  const commands = (tab: SheetTab): MenuItem[] => [
    { label: "重新命名", action: () => setRenaming(key(tab)) },
    ...(tab.kind === "worksheet"
      ? [
          { label: "複製", action: () => opened(editApi.duplicateWorksheet(ws.workbook, tab.id), "worksheet") },
          { label: "複製成交叉資料表", action: () => opened(editApi.duplicateAsCrosstab(ws.workbook, tab.id), "worksheet") },
          SEPARATOR,
          { label: "隱藏工作表", checked: tab.hidden === true, action: () => patch(tab, (s) => ({ ...s, hidden: s.hidden !== true })) },
          { label: "分頁顏色…", action: () => setColoring({ tab, color: tab.color ?? "#ffffff" }) },
        ]
      : tab.kind === "dashboard"
        ? [{ label: "複製", action: () => opened(editApi.duplicateDashboard(ws.workbook, tab.id), "dashboard") }]
        : []),
    SEPARATOR,
    { label: "刪除", action: () => remove(tab) },
  ];
  const sourceMenu: MenuItem[] = ws.datasources.map((d) => ({
    label: `資料源：${d.caption}`,
    checked: current.kind === "datasource" && current.id === d.id,
    action: () => ws.open("datasource", d.id),
  }));

  return (
    <div className="sheet-tabs" role="tablist" aria-label="分頁">
      <button
        type="button"
        role="tab"
        aria-selected={current.kind === "datasource"}
        className={`sheet-tab tab-datasource${current.kind === "datasource" ? " current" : ""}`}
        data-tab="datasource"
        disabled={!source}
        title="資料來源：資料表、關聯、join 與欄位的設定"
        onClick={() => source && current.kind !== "datasource" && ws.open("datasource", source)}
        onContextMenu={(e) => {
          e.preventDefault();
          if (sourceMenu.length > 0) setMenu({ tab: null, anchor: new DOMRect(e.clientX, e.clientY, 0, 0) });
        }}
      >
        <Icon name="datasource" size={14} />
        <span className="sheet-tab-name">資料來源</span>
      </button>
      <div className="sheet-tab-list">
        {tabs.map((tab) =>
          renaming === key(tab) ? (
            <span key={key(tab)} className={`sheet-tab tab-${tab.kind} renaming${isCurrent(tab) ? " current" : ""}`}>
              <Icon name={tab.kind} size={14} />
              <input
                aria-label={NAME_LABELS[tab.kind]}
                defaultValue={tab.name}
                autoFocus
                onFocus={(e) => e.target.select()}
                onBlur={(e) => {
                  const name = e.target.value.trim();
                  setRenaming(null);
                  // 改名之後編輯器自己會重新載入報表的定義
                  if (name && name !== tab.name) patch(tab, (s) => ({ ...s, name }), false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                  else if (e.key === "Escape") {
                    e.currentTarget.value = tab.name;
                    e.currentTarget.blur();
                  }
                }}
              />
            </span>
          ) : (
            <button
              key={key(tab)}
              type="button"
              role="tab"
              aria-selected={isCurrent(tab)}
              className={`sheet-tab tab-${tab.kind}${isCurrent(tab) ? " current" : ""}${tab.hidden ? " hidden-sheet" : ""}`}
              data-tab={key(tab)}
              title={`${KIND_NAMES[tab.kind]}：${tab.name}${tab.hidden ? "（隱藏）" : ""}`}
              style={tab.color ? { boxShadow: `inset 0 -3px 0 ${tab.color}` } : undefined}
              onClick={() => !isCurrent(tab) && ws.open(tab.kind, tab.id)}
              onDoubleClick={() => setRenaming(key(tab))}
              onContextMenu={(e) => {
                e.preventDefault();
                setMenu({ tab, anchor: new DOMRect(e.clientX, e.clientY, 0, 0) });
              }}
            >
              <Icon name={tab.kind} size={14} />
              <span className="sheet-tab-name">{tab.name}</span>
            </button>
          ),
        )}
      </div>
      <span className="sheet-tab-add">
        <ToolButton icon="worksheet-add" label="新增工作表" onClick={() => create("worksheet", { datasource: source })} />
        <ToolButton icon="dashboard-add" label="新增 dashboard" onClick={() => create("dashboard", { size: dashboardSize })} />
        <ToolButton icon="story-add" label="新增 Story" onClick={() => create("story")} />
      </span>
      {menu && <PopupMenu anchor={menu.anchor} items={menu.tab ? commands(menu.tab) : sourceMenu} onClose={() => setMenu(null)} />}
      {coloring && (
        <Modal title="分頁顏色" onClose={() => setColoring(null)}>
          <div className="tab-color-dialog">
            <label className="inline">
              「{coloring.tab.name}」分頁的顏色
              <input type="color" aria-label="分頁顏色" value={coloring.color} onChange={(e) => setColoring({ ...coloring, color: e.target.value.toLowerCase() })} />
            </label>
            <div className="dialog-actions">
              <button
                type="button"
                className="primary"
                onClick={() => {
                  const color = coloring.color === "#ffffff" ? null : coloring.color;
                  patch(coloring.tab, (s) => ({ ...s, tab_color: color }));
                  setColoring(null);
                }}
              >
                確定
              </button>
              <button
                type="button"
                onClick={() => {
                  patch(coloring.tab, (s) => ({ ...s, tab_color: null }));
                  setColoring(null);
                }}
              >
                無色彩
              </button>
              <button type="button" onClick={() => setColoring(null)}>
                取消
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/** 狀態列：分頁列下方靠左的小字。 */
export function StatusBar({ children }: { children?: React.ReactNode }) {
  return <div className="status-bar">{children}</div>;
}

/** 資料源分頁的外框：與其他編輯器相同的功能表列、工具列與分頁列（內容是資料源的編輯畫面）。 */
export function DatasourceFrame({ datasource, children }: { datasource: string; children: React.ReactNode }) {
  const ws = useWorkspace();
  const create = useCreateSheet((message) => toast(message, "error"));
  const current = { kind: "datasource" as const, id: datasource };
  const done = () => ws.view("datasource", datasource);
  const menus: MenuDef[] = [
    fileMenu(ws),
    { label: "資料", items: dataMenuItems(ws, datasource) },
    { label: "工作表", items: [{ label: "新增工作表", action: () => create("worksheet", { datasource }) }] },
    { label: "dashboard", items: [{ label: "新增 dashboard", action: () => create("dashboard") }] },
    { label: "Story", items: [{ label: "新增 Story", action: () => create("story") }] },
    serverMenu(ws),
    windowMenu(ws, current, done),
  ];
  return (
    <div className="workspace ds-workspace" data-datasource-workspace={datasource}>
      <MenuBar menus={menus} saving={false} onDone={done} />
      <div className="ws-toolbar" role="toolbar" aria-label="資料源編輯">
        <ToolButton icon="save" label="儲存" onClick={() => void saveDocument(ws.workbook, false)} />
        <span className="sep" />
        <ToolButton icon="datasource-add" label="新增資料源…" onClick={() => void addDatasource(ws.workbook, ws.navigate)} />
        <span className="sep" />
        <ToolButton icon="worksheet-add" label="新增工作表" onClick={() => create("worksheet", { datasource })} />
      </div>
      <div className="ws-scroll">{children}</div>
      <SheetTabs current={current} datasource={datasource} />
      <StatusBar />
    </div>
  );
}
