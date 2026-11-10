// 資料窗格：資料源的維度、量值、集合、計算欄位與參數；拖曳到架上，連按兩下加入檢視。

import type React from "react";
import { useState } from "react";
import { editApi } from "./api";
import { currentDrag, endDrag, startDrag, useEditor } from "./context";
import { type CatalogField, defaultUse, insertUse, isDate, isMeasure, MEASURE_NAMES, MEASURE_VALUES, removeUse } from "./model";

/** 欄位的圖示（與 Tableau 相同：Abc、#、日曆、T|F、集合）。 */
function fieldIcon(field: CatalogField): string {
  if (field.kind === "set") return "◎";
  if (field.datatype === "integer" || field.datatype === "real") return "#";
  if (isDate(field)) return field.datatype === "datetime" ? "◷" : "▦";
  if (field.datatype === "boolean") return "T|F";
  return "Abc";
}

function FieldRow({ field }: { field: CatalogField }) {
  const editor = useEditor();
  const measure = field.role === "measure" || field.level === "aggregate" || field.level === "table_calc";
  const add = () =>
    editor.apply(async (sheet) => {
      // 連按兩下：維度放到列，量值放到欄（列上已有量值時）或列
      const use = defaultUse(field);
      const [prepared, ready] = await editor.prepare(sheet, use);
      const rowsHaveMeasure = prepared.rows.some((u) => isMeasure(u, editor.fields));
      const shelf = measure ? (prepared.rows.length > 0 && !rowsHaveMeasure ? "columns" : "rows") : "rows";
      return insertUse(prepared, shelf, ready);
    });
  return (
    <li
      className={`field ${measure ? "measure" : "dimension"}`}
      draggable
      data-field={field.id}
      onDragStart={(e) => startDrag(e, { kind: "field", field: field.id })}
      onDragEnd={endDrag}
      onDoubleClick={add}
      title={field.formula ? `${field.caption}\n${field.formula}` : field.caption}
    >
      <span className="field-icon">{fieldIcon(field)}</span>
      <span className="field-name">
        {field.kind === "calc" ? "=" : ""}
        {field.caption}
      </span>
      {field.kind === "calc" && (
        <span className="field-actions">
          <button type="button" className="link" title="編輯計算欄位" onClick={() => editor.openCalc(field)}>
            ✎
          </button>
          <button
            type="button"
            className="link"
            title="刪除計算欄位"
            onClick={() => {
              const ds = editor.sheet.datasource;
              editApi
                .removeCalc(editor.workbook, ds, field.id)
                .then(() => editor.notify(`已刪除「${field.caption}」`))
                .catch((e: Error) => editor.notify(e.message, "error"));
            }}
          >
            ✕
          </button>
        </span>
      )}
    </li>
  );
}

function SpecialRow({ id, label, measure }: { id: string; label: string; measure: boolean }) {
  return (
    <li
      className={`field ${measure ? "measure" : "dimension"} special`}
      draggable
      data-field={id}
      onDragStart={(e) => startDrag(e, { kind: "field", field: id })}
      onDragEnd={endDrag}
    >
      <span className="field-icon">{measure ? "#" : "Abc"}</span>
      <span className="field-name">{label}</span>
    </li>
  );
}

export function DataPane({ onDatasource }: { onDatasource: (id: string) => void }) {
  const editor = useEditor();
  const { catalog, sheet } = editor;
  const [search, setSearch] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  // 膠囊拖回資料窗格＝從架上移除
  const { over, handlers } = useRemoveTarget();
  const ds = catalog.datasources.find((d) => d.id === sheet.datasource);
  const term = search.trim().toLowerCase();
  const visible = (ds?.fields ?? []).filter(
    (f) => (showHidden || !f.hidden) && (!term || f.caption.toLowerCase().includes(term)),
  );
  const isMeasureField = (f: CatalogField) =>
    f.role === "measure" || f.level === "aggregate" || f.level === "table_calc";
  const dimensions = visible.filter((f) => !isMeasureField(f) && f.kind !== "set");
  const sets = visible.filter((f) => f.kind === "set");
  const measures = visible.filter(isMeasureField);
  return (
    <aside className={`data-pane${over ? " over" : ""}`} {...handlers} aria-label="資料">
      <div className="pane-title">資料</div>
      <select
        aria-label="資料源"
        value={sheet.datasource}
        onChange={(e) => onDatasource(e.target.value)}
        title="工作表使用的資料源"
      >
        {catalog.datasources.map((d) => (
          <option key={d.id} value={d.id}>
            {d.caption}
          </option>
        ))}
      </select>
      <input
        type="search"
        placeholder="搜尋欄位"
        aria-label="搜尋欄位"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="pane-actions">
        <button type="button" onClick={() => editor.openCalc(null)}>
          建立計算欄位…
        </button>
        <label className="hint">
          <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} /> 顯示隱藏的欄位
        </label>
      </div>
      <div className="pane-section">維度</div>
      <ul className="fields">
        {dimensions.map((f) => (
          <FieldRow key={f.id} field={f} />
        ))}
        {!term && <SpecialRow id={MEASURE_NAMES} label="Measure Names" measure={false} />}
      </ul>
      {sets.length > 0 && (
        <>
          <div className="pane-section">集合</div>
          <ul className="fields">
            {sets.map((f) => (
              <FieldRow key={f.id} field={f} />
            ))}
          </ul>
        </>
      )}
      <div className="pane-section">量值</div>
      <ul className="fields">
        {measures.map((f) => (
          <FieldRow key={f.id} field={f} />
        ))}
        {!term && <SpecialRow id={MEASURE_VALUES} label="Measure Values" measure />}
      </ul>
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
