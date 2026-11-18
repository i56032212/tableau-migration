// 資料窗格：資料源的維度（含階層）、量值、集合、計算欄位與參數；拖曳到架上，連按兩下加入檢視。

import type React from "react";
import { useState } from "react";
import { editApi } from "./api";
import { currentDrag, endDrag, startDrag, useEditor } from "./context";
import { hierarchyCandidates } from "./Dialogs";
import { fieldMenu } from "./FieldDialogs";
import { Icon } from "./Icons";
import { type MenuItem, PopupMenu, SEPARATOR, type SheetControlIR } from "./Shelves";
import {
  blendSources,
  buildMap,
  type CatalogField,
  type CatalogFolder,
  type CatalogHierarchy,
  defaultUse,
  GENERATED_LABELS,
  insertUse,
  isDate,
  isMeasure,
  LATITUDE_GENERATED,
  LONGITUDE_GENERATED,
  MEASURE_NAMES,
  MEASURE_VALUES,
  removeUse,
} from "./model";

/** 可以指定地理角色的欄位：文字或整數的維度（地名、郵遞區號），數字的欄位（緯度、經度）。 */
function canHaveGeoRole(field: CatalogField): boolean {
  return field.kind !== "set" && ["string", "integer", "real"].includes(field.datatype);
}

/** 欄位的圖示（與 Tableau 相同：Abc、#、日曆、T|F、集合、群組的迴紋針、數值分組；有地理角色的為地球、
空間資料為地圖）。 */
function fieldIcon(field: CatalogField): string {
  if (field.geo_role) return "🌐";
  if (field.combined) return "🔗";
  if (field.datatype === "spatial") return "🗺";
  if (field.kind === "set") return "◎";
  if (field.group) return "📎";
  if (field.bin) return "▥";
  if (field.datatype === "integer" || field.datatype === "real") return "#";
  if (isDate(field)) return field.datatype === "datetime" ? "◷" : "▦";
  if (field.datatype === "boolean") return "T|F";
  return "Abc";
}

/** 改變階層的欄位（沒有欄位時刪除階層），完成後重新讀取資料窗格。 */
function saveLevels(
  editor: ReturnType<typeof useEditor>,
  datasource: string,
  hierarchy: CatalogHierarchy,
  fields: string[],
  message: string,
) {
  editApi
    .saveHierarchy(editor.workbook, datasource, hierarchy.name, fields, hierarchy.name)
    .then(() => {
      editor.reloadCatalog();
      editor.notify(message);
    })
    .catch((e: Error) => editor.notify(e.message, "error"));
}

function FieldRow({
  field,
  datasource,
  hierarchy,
  hierarchical = false,
}: {
  field: CatalogField;
  datasource: string;
  /** 欄位所在的階層 */
  hierarchy?: CatalogHierarchy;
  /** 可以放進階層的維度 */
  hierarchical?: boolean;
}) {
  const editor = useEditor();
  const [menu, setMenu] = useState<DOMRect | null>(null);
  const measure = field.role === "measure" || field.level === "aggregate" || field.level === "table_calc";
  const add = () =>
    editor.apply(async (sheet) => {
      const use = defaultUse(field);
      const [prepared, ready] = await editor.prepare(sheet, use);
      // 地理角色的維度：與 Tableau 相同建立地圖（或加到地圖的詳細資料）
      const role = field.geo_role;
      if (role && role !== "latitude" && role !== "longitude" && !measure) return buildMap(prepared, ready, role);
      // 連按兩下：維度放到列，量值放到欄（列上已有量值時）或列
      const rowsHaveMeasure = prepared.rows.some((u) => isMeasure(u, editor.fields));
      const shelf = measure ? (prepared.rows.length > 0 && !rowsHaveMeasure ? "columns" : "rows") : "rows";
      return insertUse(prepared, shelf, ready);
    });
  return (
    <li
      className={`field ${measure ? "measure" : "dimension"}${menu ? " menu-open" : ""}${field.hidden ? " hidden-field" : ""}`}
      draggable
      data-field={field.id}
      onDragStart={(e) => startDrag(e, { kind: "field", field: field.id })}
      onDragEnd={endDrag}
      onDoubleClick={add}
      onContextMenu={(e) => {
        // 按右鍵：與 Tableau 相同的欄位選單
        e.preventDefault();
        setMenu(new DOMRect(e.clientX, e.clientY, 0, 0));
      }}
      title={[field.caption, field.formula, field.comment].filter(Boolean).join("\n")}
    >
      <span className="field-icon">{fieldIcon(field)}</span>
      <span className="field-name">
        {field.kind === "calc" && !field.group && !field.bin ? "=" : ""}
        {field.caption}
      </span>
      <span className="field-actions">
        <button
          type="button"
          className="link field-menu"
          title="欄位選單（重新命名、複製、隱藏、建立群組、集合、數值分組、別名、預設屬性…）"
          aria-label={`「${field.caption}」的選單`}
          onClick={(e) => setMenu((e.currentTarget as HTMLElement).getBoundingClientRect())}
        >
          ▾
        </button>
        {canHaveGeoRole(field) && (
          <button
            type="button"
            className="link"
            title={field.geo_role ? "地理角色、編輯位置…" : "指定地理角色…"}
            aria-label={`「${field.caption}」的地理角色`}
            onClick={() => editor.openDialog({ kind: "geo", datasource, field: field.id })}
          >
            🌐
          </button>
        )}
          {hierarchical && !hierarchy && (
            <button
              type="button"
              className="link"
              title="建立階層…"
              aria-label={`以「${field.caption}」建立階層`}
              onClick={() => editor.openDialog({ kind: "hierarchy", datasource, name: null, field: field.id })}
            >
              ⊞
            </button>
          )}
          {hierarchy && (
            <button
              type="button"
              className="link"
              title="從階層移除"
              aria-label={`從階層移除「${field.caption}」`}
              onClick={() =>
                saveLevels(
                  editor,
                  datasource,
                  hierarchy,
                  hierarchy.fields.filter((f) => f !== field.id),
                  `已從「${hierarchy.name}」移除「${field.caption}」`,
                )
              }
            >
              ↤
            </button>
          )}
        {field.kind === "calc" && (
            <>
              {!field.combined && (
                <button type="button" className="link" title="編輯計算欄位" onClick={() => editor.openCalc(field, datasource)}>
                  ✎
                </button>
              )}
              <button
                type="button"
                className="link"
                title="刪除計算欄位"
                onClick={() => {
                  editApi
                    .removeCalc(editor.workbook, datasource, field.id)
                    .then(() => {
                      editor.reloadCatalog();
                      editor.notify(`已刪除「${field.caption}」`);
                    })
                    .catch((e: Error) => editor.notify(e.message, "error"));
                }}
              >
                ✕
              </button>
            </>
          )}
      </span>
      {menu && (
        // 選單在可以拖曳的欄位中：選單上的拖曳、連按兩下不作用到欄位
        <span
          onDoubleClick={(e) => e.stopPropagation()}
          onDragStart={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
        >
          <PopupMenu anchor={menu} items={fieldMenu(editor, field, datasource, hierarchical && !hierarchy)} onClose={() => setMenu(null)} />
        </span>
      )}
    </li>
  );
}

/** 階層：名稱與由上到下的欄位；把維度拖曳到名稱上即加入階層的最下層。 */
function HierarchyGroup({
  hierarchy,
  fields,
  datasource,
}: {
  hierarchy: CatalogHierarchy;
  fields: CatalogField[];
  datasource: string;
}) {
  const editor = useEditor();
  const [open, setOpen] = useState(true);
  const [over, setOver] = useState(false);
  const known = new Map(fields.map((f) => [f.id, f]));
  const dragged = () => {
    const item = currentDrag();
    if (item?.kind !== "field" || hierarchy.fields.includes(item.field)) return null;
    const field = editor.catalog.datasources.find((d) => d.id === datasource)?.fields.find((f) => f.id === item.field);
    return field && hierarchyCandidates([field]).length > 0 ? field : null;
  };
  return (
    <li className="hierarchy" data-hierarchy={hierarchy.name}>
      <div
        className={`hierarchy-name${over ? " over" : ""}`}
        onDragOver={(e) => {
          if (dragged()) e.preventDefault();
        }}
        onDragEnter={() => setOver(dragged() !== null)}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const field = dragged();
          endDrag();
          if (field)
            saveLevels(editor, datasource, hierarchy, [...hierarchy.fields, field.id], `已把「${field.caption}」加入「${hierarchy.name}」`);
        }}
      >
        <button type="button" className="link toggle" aria-expanded={open} aria-label={`${open ? "收合" : "展開"}階層 ${hierarchy.name}`} onClick={() => setOpen(!open)}>
          {open ? "▾" : "▸"}
        </button>
        <span className="field-icon">⊞</span>
        <span className="field-name">{hierarchy.name}</span>
        <span className="field-actions">
          <button
            type="button"
            className="link"
            title="編輯階層"
            aria-label={`編輯階層 ${hierarchy.name}`}
            onClick={() => editor.openDialog({ kind: "hierarchy", datasource, name: hierarchy.name })}
          >
            ✎
          </button>
          <button
            type="button"
            className="link"
            title="刪除階層（欄位留在資料窗格中）"
            aria-label={`刪除階層 ${hierarchy.name}`}
            onClick={() => {
              editApi
                .removeHierarchy(editor.workbook, datasource, hierarchy.name)
                .then(() => {
                  editor.reloadCatalog();
                  editor.notify(`已刪除階層「${hierarchy.name}」`);
                })
                .catch((e: Error) => editor.notify(e.message, "error"));
            }}
          >
            ✕
          </button>
        </span>
      </div>
      {open && (
        <ul className="fields">
          {hierarchy.fields.map((id) => {
            const field = known.get(id);
            return field ? <FieldRow key={id} field={field} datasource={datasource} hierarchy={hierarchy} hierarchical /> : null;
          })}
        </ul>
      )}
    </li>
  );
}

/** 資料夾：名稱與其中的欄位；把欄位拖曳到名稱上即移進資料夾（與 Tableau 相同）。 */
function FolderGroup({ folder, datasource, children }: { folder: CatalogFolder; datasource: string; children: React.ReactNode }) {
  const editor = useEditor();
  const [open, setOpen] = useState(true);
  const [over, setOver] = useState(false);
  const dragged = () => {
    const item = currentDrag();
    return item?.kind === "field" && !folder.fields.includes(item.field) ? item.field : null;
  };
  const done = (message: string) => {
    editor.reloadCatalog();
    editor.notify(message);
  };
  return (
    <li className="folder" data-folder={folder.name}>
      <div
        className={`folder-name${over ? " over" : ""}`}
        onDragOver={(e) => {
          if (dragged()) {
            e.preventDefault();
            e.stopPropagation();
          }
        }}
        onDragEnter={() => setOver(dragged() !== null)}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          const field = dragged();
          if (!field) return;
          e.preventDefault();
          e.stopPropagation();
          setOver(false);
          endDrag();
          editApi
            .moveToFolder(editor.workbook, datasource, [field], folder.name)
            .then(() => done(`已移到資料夾「${folder.name}」`))
            .catch((err: Error) => editor.notify(err.message, "error"));
        }}
      >
        <button type="button" className="link toggle" aria-expanded={open} aria-label={`${open ? "收合" : "展開"}資料夾 ${folder.name}`} onClick={() => setOpen(!open)}>
          {open ? "▾" : "▸"}
        </button>
        <span className="field-icon">📁</span>
        <span className="field-name">{folder.name}</span>
        <span className="field-actions">
          <button
            type="button"
            className="link"
            title="重新命名資料夾"
            aria-label={`重新命名資料夾 ${folder.name}`}
            onClick={() => editor.openDialog({ kind: "folder", datasource, fields: [], folder: folder.name })}
          >
            ✎
          </button>
          <button
            type="button"
            className="link"
            title="刪除資料夾（欄位留在資料窗格中）"
            aria-label={`刪除資料夾 ${folder.name}`}
            onClick={() =>
              void editApi
                .removeFolder(editor.workbook, datasource, folder.name)
                .then(() => done(`已刪除資料夾「${folder.name}」`))
                .catch((err: Error) => editor.notify(err.message, "error"))
            }
          >
            ✕
          </button>
        </span>
      </div>
      {open && <ul className="fields">{children}</ul>}
    </li>
  );
}

/** 一個區段（維度或量值）中的資料夾：分在這個區段（或不分）且有顯示的欄位的資料夾；空的資料夾列在維度。 */
function sectionFolders(
  folders: CatalogFolder[],
  role: "dimension" | "measure",
  shown: CatalogField[],
  searching: boolean,
): { folder: CatalogFolder; fields: CatalogField[] }[] {
  const byId = new Map(shown.map((f) => [f.id, f]));
  return folders
    .filter((f) => f.role === null || f.role === role)
    .map((folder) => ({ folder, fields: folder.fields.map((id) => byId.get(id)).filter((f): f is CatalogField => !!f) }))
    .filter(({ folder, fields }) => fields.length > 0 || (!searching && role === "dimension" && folder.role === null && folder.fields.length === 0));
}

function SpecialRow({ id, label, measure, geo = false }: { id: string; label: string; measure: boolean; geo?: boolean }) {
  return (
    <li
      className={`field ${measure ? "measure" : "dimension"} special`}
      draggable
      data-field={id}
      onDragStart={(e) => startDrag(e, { kind: "field", field: id })}
      onDragEnd={endDrag}
    >
      <span className="field-icon">{geo ? "🌐" : measure ? "#" : "Abc"}</span>
      <span className="field-name">{label}</span>
    </li>
  );
}

export function DataPane({
  onDatasource,
  onEditDatasource,
  hidden = false,
}: {
  onDatasource: (id: string) => void;
  /** 開啟資料源分頁的編輯（桌面版） */
  onEditDatasource?: (id: string) => void;
  /** 側欄切到另一個分頁時隱藏（搜尋的文字與瀏覽的資料源保留） */
  hidden?: boolean;
}) {
  const editor = useEditor();
  const parameterShown = (id: string) =>
    ((editor.sheet.controls as SheetControlIR[] | undefined) ?? []).some((c) => c.kind === "parameter" && c.parameter === id);
  const { catalog, sheet } = editor;
  const [search, setSearch] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  // 搜尋框旁邊的選單（Tableau 資料窗格的下拉選單）：建立計算欄位、參數、資料夾，顯示隱藏的欄位
  const [paneMenu, setPaneMenu] = useState<DOMRect | null>(null);
  // 膠囊拖回資料窗格＝從架上移除
  const { over, handlers } = useRemoveTarget();
  // 資料窗格瀏覽的資料源：預設為工作表的（主要）資料源；其他資料源的量值放到檢視上即為 Data Blending
  const [browse, setBrowse] = useState<string | null>(null);
  const shown = browse !== null && catalog.datasources.some((d) => d.id === browse) ? browse : sheet.datasource;
  const ds = catalog.datasources.find((d) => d.id === shown);
  const secondary = new Set(blendSources(sheet));
  const term = search.trim().toLowerCase();
  // 臨時計算（直接在架上輸入的公式）只在工作表上，資料窗格不列出
  const visible = (ds?.fields ?? []).filter(
    (f) => !f.ad_hoc && (showHidden || !f.hidden) && (!term || f.caption.toLowerCase().includes(term)),
  );
  const isMeasureField = (f: CatalogField) =>
    f.role === "measure" || f.level === "aggregate" || f.level === "table_calc";
  const dimensions = visible.filter((f) => !isMeasureField(f) && f.kind !== "set");
  const sets = visible.filter((f) => f.kind === "set");
  const measures = visible.filter(isMeasureField);
  // 階層中的維度列在階層下（顯示的欄位中有它的階層才列出階層）
  const hierarchies = (ds?.hierarchies ?? []).filter((h) => h.fields.some((id) => dimensions.some((f) => f.id === id)));
  const inHierarchy = new Set(hierarchies.flatMap((h) => h.fields));
  const candidates = new Set(hierarchyCandidates(dimensions).map((f) => f.id));
  // 有地名角色的欄位時，與 Tableau 相同列出 Latitude／Longitude (generated)
  const geographic = (ds?.fields ?? []).some((f) => f.geo_role && f.geo_role !== "latitude" && f.geo_role !== "longitude");
  // 資料夾：資料夾中的欄位列在資料夾下（階層中的維度仍列在階層下）
  const folders = ds?.folders ?? [];
  const dimensionFolders = sectionFolders(folders, "dimension", dimensions.filter((f) => !inHierarchy.has(f.id)), !!term);
  const measureFolders = sectionFolders(folders, "measure", measures, !!term);
  const foldered = new Set([...dimensionFolders, ...measureFolders].flatMap((g) => g.fields.map((f) => f.id)));
  const paneItems: MenuItem[] = [
    { label: "建立計算欄位…", action: () => editor.openCalc(null, shown) },
    { label: "建立參數…", action: () => editor.openDialog({ kind: "parameter", parameter: null }) },
    // 把欄位拖曳到資料夾的名稱上即移進去
    { label: "新資料夾…", action: () => editor.openDialog({ kind: "folder", datasource: shown, fields: [], folder: null }) },
    SEPARATOR,
    // 資料源分頁：加入資料表、關聯、join、聯集、樞紐與欄位的設定
    ...(onEditDatasource ? [{ label: "編輯資料源…", action: () => onEditDatasource(shown) }] : []),
    // 週的起始日與會計年度的起始月（Tableau 的「日期屬性」）
    { label: "日期屬性…", action: () => editor.openDialog({ kind: "date-properties", datasource: shown }) },
    SEPARATOR,
    { label: "顯示隱藏的欄位", checked: showHidden, action: () => setShowHidden(!showHidden) },
  ];
  return (
    <aside className={`data-pane${over ? " over" : ""}`} {...handlers} aria-label="資料" hidden={hidden}>
      <select
        aria-label="資料源"
        value={shown}
        onChange={(e) => setBrowse(e.target.value)}
        title="資料窗格顯示的資料源；其他資料源的量值放到檢視上即依名稱相同的維度混合（Data Blending）"
      >
        {catalog.datasources.map((d) => (
          <option key={d.id} value={d.id}>
            {d.caption}
            {d.id === sheet.datasource ? "（主要）" : secondary.has(d.id) ? "（次要）" : ""}
          </option>
        ))}
      </select>
      {secondary.size > 0 && (
        <div className="pane-actions">
          <button type="button" title="Data Blending 的連結欄位（Tableau 的「編輯混合關係」）" onClick={() => editor.openDialog({ kind: "blends" })}>
            混合關係…
          </button>
        </div>
      )}
      {shown !== sheet.datasource && (
        <div className="pane-actions blend-actions">
          <span className="hint">量值放到檢視上即與主要資料源混合（Data Blending，依名稱相同的維度連結）</span>
          <button
            type="button"
            title="以這個資料源取代工作表的主要資料源（欄位依名稱對應）"
            onClick={() => {
              onDatasource(shown);
              setBrowse(null);
            }}
          >
            取代主要資料源
          </button>
        </div>
      )}
      <div className="pane-search">
        <input
          type="search"
          placeholder="搜尋欄位"
          aria-label="搜尋欄位"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          className="pane-menu-button"
          aria-label="資料窗格的選單"
          aria-haspopup="menu"
          title="建立計算欄位、參數、資料夾，編輯資料源，顯示隱藏的欄位"
          onClick={(e) => setPaneMenu(e.currentTarget.getBoundingClientRect())}
        >
          <Icon name="caret" size={12} />
        </button>
        {paneMenu && <PopupMenu anchor={paneMenu} items={paneItems} onClose={() => setPaneMenu(null)} />}
      </div>
      <div className="pane-section">維度</div>
      <ul className="fields">
        {hierarchies.map((h) => (
          <HierarchyGroup key={h.name} hierarchy={h} fields={dimensions} datasource={shown} />
        ))}
        {dimensionFolders.map(({ folder, fields }) => (
          <FolderGroup key={`d:${folder.name}`} folder={folder} datasource={shown}>
            {fields.map((f) => (
              <FieldRow key={f.id} field={f} datasource={shown} hierarchical={candidates.has(f.id)} />
            ))}
          </FolderGroup>
        ))}
        {dimensions
          .filter((f) => !inHierarchy.has(f.id) && !foldered.has(f.id))
          .map((f) => (
            <FieldRow key={f.id} field={f} datasource={shown} hierarchical={candidates.has(f.id)} />
          ))}
        {!term && <SpecialRow id={MEASURE_NAMES} label="Measure Names" measure={false} />}
      </ul>
      <div className="pane-section">量值</div>
      <ul className="fields">
        {measureFolders.map(({ folder, fields }) => (
          <FolderGroup key={`m:${folder.name}`} folder={folder} datasource={shown}>
            {fields.map((f) => (
              <FieldRow key={f.id} field={f} datasource={shown} />
            ))}
          </FolderGroup>
        ))}
        {measures
          .filter((f) => !foldered.has(f.id))
          .map((f) => (
            <FieldRow key={f.id} field={f} datasource={shown} />
          ))}
        {!term && geographic && (
          <>
            <SpecialRow id={LATITUDE_GENERATED} label={GENERATED_LABELS[LATITUDE_GENERATED]} measure geo />
            <SpecialRow id={LONGITUDE_GENERATED} label={GENERATED_LABELS[LONGITUDE_GENERATED]} measure geo />
          </>
        )}
        {!term && <SpecialRow id={MEASURE_VALUES} label="Measure Values" measure />}
      </ul>
      {sets.length > 0 && (
        <>
          <div className="pane-section">集合</div>
          <ul className="fields">
            {sets.map((f) => (
              <FieldRow key={f.id} field={f} datasource={shown} />
            ))}
          </ul>
        </>
      )}
      <div className="pane-section">
        參數
        <button type="button" className="link" title="建立參數" onClick={() => editor.openDialog({ kind: "parameter", parameter: null })}>
          ＋
        </button>
      </div>
      <ul className="fields">
        {catalog.parameters.map((p) => (
          <li key={p.id} className="field parameter" title={`${p.caption}：${String(p.value)}`}>
            <span className="field-icon">≡</span>
            <span className="field-name">{p.caption}</span>
            <span className="field-actions">
              <button
                type="button"
                className="link"
                aria-pressed={parameterShown(p.id)}
                title={parameterShown(p.id) ? "隱藏參數控制項" : "顯示參數：單獨檢視工作表時，檢視旁邊顯示這個參數的控制項"}
                onClick={() =>
                  editor.apply((s) => {
                    const controls = (s.controls as SheetControlIR[] | undefined) ?? [];
                    if (controls.some((c) => c.kind === "parameter" && c.parameter === p.id))
                      return { ...s, controls: controls.filter((c) => !(c.kind === "parameter" && c.parameter === p.id)) };
                    const control = p.allowed.kind === "list" ? "single_dropdown" : p.allowed.kind === "range" ? "single_slider" : "type_in";
                    return { ...s, controls: [...controls, { kind: "parameter", field: null, parameter: p.id, control, relevant: false }] };
                  })
                }
              >
                {parameterShown(p.id) ? "◉" : "○"}
              </button>
              <button type="button" className="link" title="編輯參數" onClick={() => editor.openDialog({ kind: "parameter", parameter: p.id })}>
                ✎
              </button>
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** 資料窗格作為放下的目標：架上的膠囊拖回來就移除。 */
function useRemoveTarget() {
  const editor = useEditor();
  const [over, setOver] = useState(false);
  const pill = () => {
    const item = currentDrag();
    return item?.kind === "pill" ? item : null;
  };
  const handlers = {
    onDragOver: (e: React.DragEvent) => {
      if (pill()) e.preventDefault();
    },
    onDragEnter: () => setOver(pill() !== null),
    onDragLeave: (e: React.DragEvent) => {
      if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node | null)) setOver(false);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setOver(false);
      const item = pill();
      endDrag();
      if (!item) return;
      editor.apply((sheet) => removeUse(sheet, item.shelf, item.index, item.card));
    },
  };
  return { over, handlers };
}
