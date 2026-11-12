import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { DataGrid } from "../DatasourceView";
import { pickDataPath, toast } from "../desktop";
import { Modal } from "../Server";
import type { DatasourcePreview } from "../types";
import {
  datasourceApi,
  type Comparison,
  type Condition,
  type DatasourceModel,
  type InspectedTable,
  type JoinType,
  type LogicalView,
  type ModelField,
  type RelationView,
} from "./datasourceApi";

// 資料源分頁的編輯（Tableau Desktop 的「資料來源」分頁）：左邊是連線的檔案與其中的資料表，中間是邏輯資料表
// 與關聯（雙擊邏輯資料表看到的 join、聯集、樞紐），下面是欄位（改名、隱藏、資料類型、角色）與資料的預覽。

const JOINS: Record<JoinType, string> = { inner: "內部", left: "左側", right: "右側", full: "完整外部" };
const OPS: Comparison[] = ["=", "!=", "<", "<=", ">", ">="];
const TYPES: Record<string, string> = {
  string: "字串",
  integer: "整數",
  real: "小數",
  date: "日期",
  datetime: "日期時間",
  boolean: "布林",
  spatial: "空間",
};
const HOW = { relationship: "關聯（另一張邏輯資料表）", join: "Join（同一張邏輯資料表）", union: "聯集（上下相接）" };

interface Source {
  /** 報表中已有的檔案（Data/…）或作者電腦上的新檔案 */
  file?: string;
  path?: string;
  name: string;
  tables: InspectedTable[];
}

export function DatasourceEditor({
  workbook,
  datasource,
  onOpenSheet,
  onChanged,
}: {
  workbook: string;
  datasource: string;
  onOpenSheet: () => void;
  onChanged?: () => void;
}) {
  const [model, setModel] = useState<DatasourceModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState<{ source: Source; table: string } | null>(null);
  const [preview, setPreview] = useState<DatasourcePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewTable, setPreviewTable] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    datasourceApi
      .model(workbook, datasource)
      .then((m) => {
        setModel(m);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [workbook, datasource]);

  useEffect(() => {
    let cancelled = false;
    api
      .preview(workbook, datasource, previewTable, 100)
      .then((p) => {
        if (!cancelled) {
          setPreview(p);
          setPreviewError(null);
        }
      })
      .catch((e: Error) => !cancelled && setPreviewError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook, datasource, previewTable, version]);

  /** 套用一個變更：成功時換上新的模型、重新預覽；失敗時顯示原因。 */
  const change = useCallback(
    async (run: () => Promise<DatasourceModel>, done?: string): Promise<boolean> => {
      setBusy(true);
      try {
        const updated = await run();
        setModel(updated);
        setSelected([]);
        setVersion((v) => v + 1);
        onChanged?.();
        if (done) toast(done);
        return true;
      } catch (e) {
        toast((e as Error).message, "error");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [onChanged],
  );

  const otherFile = async () => {
    const path = await pickDataPath();
    if (!path) return;
    try {
      const inspected = await datasourceApi.inspect(workbook, path);
      setAdding({ source: { path, name: inspected.name, tables: inspected.tables }, table: inspected.tables[0].name });
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  if (error) return <div className="placeholder error">無法開啟資料源：{error}</div>;
  if (!model) return <div className="placeholder">載入中…</div>;
  const fields = model.fields;
  const logicalCaption = (id: string) => model.logical.find((l) => l.id === id)?.caption ?? id;

  return (
    <div className="ds-editor" data-datasource={datasource} data-busy={busy ? "true" : "false"}>
      <header className="dse-header">
        <input
          className="dse-caption"
          aria-label="資料源名稱"
          defaultValue={model.datasource.caption}
          key={model.datasource.caption}
          onBlur={(e) => {
            const caption = e.target.value.trim();
            if (caption && caption !== model.datasource.caption)
              void change(() => datasourceApi.rename(workbook, datasource, caption));
          }}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <span className={`ds-badge ${model.datasource.kind}`}>
          {{ extract: "萃取", file: "檔案", live: "即時" }[model.datasource.kind] ?? model.datasource.kind}
        </span>
        <button type="button" className="primary" onClick={onOpenSheet}>
          前往工作表
        </button>
      </header>
      {!model.editable && (
        <p className="hint">這個資料源不是檔案的資料源（Live 連線或萃取），只能修改資料源的名稱與欄位的名稱、隱藏、角色。</p>
      )}
      <div className="dse-body">
        <aside className="dse-files" aria-label="連線">
          <h2>連線</h2>
          {model.files.map((file) => (
            <div key={file.path} className="dse-file">
              <div className="dse-file-name" title={file.path}>
                {file.name}
              </div>
              {!file.available && (
                <p className="muted">{file.error ?? "報表中沒有這個檔案的複本（以「其他檔案…」重新選擇）"}</p>
              )}
              <ul>
                {(file.tables ?? []).map((t) => (
                  <li key={t.name}>
                    <button
                      type="button"
                      className="link dse-add"
                      data-table={t.name}
                      disabled={!model.editable}
                      title="加入這張資料表"
                      onClick={() =>
                        setAdding({ source: { file: file.path, name: file.name, tables: file.tables ?? [] }, table: t.name })
                      }
                    >
                      ▦ {t.name}
                    </button>
                    <span className="muted">（{t.rows.toLocaleString()} 列）</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {model.editable && (
            <button type="button" className="dse-other" onClick={() => void otherFile()}>
              其他檔案…
            </button>
          )}
        </aside>
        <section className="dse-canvas" aria-label="資料表">
          <h2>{model.logical.length > 1 ? "邏輯資料表與關聯" : "資料表"}</h2>
          <div className="dse-logicals">
            {model.logical.map((logical) => (
              <LogicalCard
                key={logical.id ?? "single"}
                logical={logical}
                model={model}
                active={previewTable === logical.id}
                onPreview={() => setPreviewTable(logical.id)}
                onChange={change}
                workbook={workbook}
                datasource={datasource}
                removable={model.logical.length > 1}
              />
            ))}
          </div>
          {model.relationships.length > 0 && (
            <ul className="dse-relationships">
              {model.relationships.map((r) => (
                <RelationshipRow
                  key={r.right}
                  relationship={r}
                  fields={fields}
                  leftCaption={logicalCaption(r.left)}
                  rightCaption={logicalCaption(r.right)}
                  onSave={(on) => change(() => datasourceApi.setRelationship(workbook, datasource, r.right, on), "已修改關聯")}
                />
              ))}
            </ul>
          )}
        </section>
      </div>
      <section className="dse-fields" aria-label="欄位">
        <div className="ds-grid-bar">
          <h2>欄位（{fields.length}）</h2>
          {model.editable && (
            <button
              type="button"
              disabled={selected.length === 0}
              title="把選取的欄樞紐成列（Pivot Field Names、Pivot Field Values）"
              onClick={() => {
                const picked = fields.filter((f) => selected.includes(f.id));
                const table = pivotTable(model, picked);
                if (!table) {
                  toast("要選取同一張資料表的欄位（不是聯集或 join 的欄位）", "error");
                  return;
                }
                void change(
                  () => datasourceApi.pivot(workbook, datasource, table, picked.map((f) => f.column ?? "")),
                  "已樞紐",
                );
              }}
            >
              樞紐所選的欄位
            </button>
          )}
        </div>
        <div className="ds-table-wrap">
          <table className="ds-data dse-field-grid">
            <thead>
              <tr>
                <th aria-label="選取" />
                <th>欄位</th>
                <th>資料類型</th>
                <th>角色</th>
                <th>資料表</th>
                <th>隱藏</th>
              </tr>
            </thead>
            <tbody>
              {fields.map((f) => (
                <FieldRow
                  key={`${f.id}:${f.caption}:${f.datatype}:${f.role}:${f.hidden}`}
                  field={f}
                  tableCaption={f.table ? logicalCaption(f.table) : ""}
                  editable={model.editable}
                  selected={selected.includes(f.id)}
                  onSelect={(on) => setSelected((s) => (on ? [...s, f.id] : s.filter((x) => x !== f.id)))}
                  onUpdate={(body) => change(() => datasourceApi.updateField(workbook, datasource, f.id, body))}
                />
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="dse-preview" aria-label="資料">
        <div className="ds-grid-bar">
          <h2>資料</h2>
          <span className="muted">
            {preview ? `前 ${preview.rows.length.toLocaleString()} 列${preview.total !== null ? `（共 ${preview.total.toLocaleString()} 列）` : ""}` : ""}
          </span>
        </div>
        <DataGrid preview={preview} error={previewError} loading={!preview} />
      </section>
      {adding && (
        <AddTableDialog
          model={model}
          source={adding.source}
          initialTable={adding.table}
          onClose={() => setAdding(null)}
          onSubmit={(body) =>
            change(
              () =>
                datasourceApi.addTable(workbook, datasource, {
                  ...body,
                  file: adding.source.file,
                  path: adding.source.path,
                }),
              "已加入資料表",
            )
          }
        />
      )}
    </div>
  );
}

/** 樞紐的對象：選取的欄位都在同一張單一的資料表（回傳關聯樹中的名稱）。 */
function pivotTable(model: DatasourceModel, picked: ModelField[]): string | null {
  if (!picked.length || picked.some((f) => f.column === null || f.calc !== null)) return null;
  const tables = new Set(picked.map((f) => f.table));
  if (tables.size !== 1) return null;
  const [owner] = [...tables];
  const logical = model.logical.find((l) => l.id === owner) ?? (model.logical.length === 1 ? model.logical[0] : null);
  if (!logical || logical.relation.kind !== "table") return null;
  return logical.relation.name;
}

function LogicalCard({
  logical,
  model,
  active,
  onPreview,
  onChange,
  workbook,
  datasource,
  removable,
}: {
  logical: LogicalView;
  model: DatasourceModel;
  active: boolean;
  onPreview: () => void;
  onChange: (run: () => Promise<DatasourceModel>, done?: string) => Promise<boolean>;
  workbook: string;
  datasource: string;
  removable: boolean;
}) {
  const fields = model.fields.filter((f) => (logical.id ? f.table === logical.id : true) && f.column !== null);
  return (
    <div className={`dse-logical${active ? " active" : ""}`} data-logical={logical.id ?? ""}>
      <div className="dse-logical-head">
        <button type="button" className="link" onClick={onPreview} disabled={logical.id === null} title="預覽這張邏輯資料表">
          <strong>{logical.caption}</strong>
        </button>
        {removable && logical.id && (
          <button
            type="button"
            className="link danger-link"
            aria-label={`移除 ${logical.caption}`}
            onClick={() =>
              void onChange(() => datasourceApi.removeTable(workbook, datasource, logical.id as string), "已移除資料表")
            }
          >
            移除
          </button>
        )}
      </div>
      <RelationTree
        relation={logical.relation}
        fields={fields}
        editable={model.editable}
        onRemove={(name) => void onChange(() => datasourceApi.removeTable(workbook, datasource, name), "已移除資料表")}
        onUnpivot={(name) => void onChange(() => datasourceApi.unpivot(workbook, datasource, name), "已移除樞紐")}
        onSetJoin={(table, join, on) => onChange(() => datasourceApi.setJoin(workbook, datasource, table, join, on), "已修改 join")}
      />
    </div>
  );
}

function RelationTree({
  relation,
  fields,
  editable,
  onRemove,
  onUnpivot,
  onSetJoin,
}: {
  relation: RelationView;
  fields: ModelField[];
  editable: boolean;
  onRemove: (name: string) => void;
  onUnpivot: (name: string) => void;
  onSetJoin: (table: string, join: JoinType, on: Condition[]) => Promise<boolean>;
}) {
  if (relation.kind === "table" || relation.kind === "sql") {
    return <div className="dse-table">▦ {relation.name}</div>;
  }
  if (relation.kind === "join") {
    return (
      <div className="dse-join">
        <RelationTree relation={relation.left} fields={fields} editable={editable} onRemove={onRemove} onUnpivot={onUnpivot} onSetJoin={onSetJoin} />
        <JoinEditor relation={relation} fields={fields} editable={editable} onSave={onSetJoin} onRemove={onRemove} />
      </div>
    );
  }
  if (relation.kind === "union") {
    return (
      <div className="dse-union">
        <div className="dse-label">聯集（{relation.parts.length} 張資料表上下相接）</div>
        {relation.parts.map((p) => (
          <div key={"name" in p ? p.name : ""} className="dse-table">
            ▦ {"name" in p ? p.name : ""}
            {editable && relation.parts.length > 1 && "name" in p && (
              <button type="button" className="link danger-link" onClick={() => onRemove(p.name)}>
                移除
              </button>
            )}
          </div>
        ))}
      </div>
    );
  }
  if (relation.kind === "pivot") {
    return (
      <div className="dse-pivot">
        <RelationTree relation={relation.source} fields={fields} editable={editable} onRemove={onRemove} onUnpivot={onUnpivot} onSetJoin={onSetJoin} />
        <div className="dse-label">
          樞紐：{relation.columns.join("、")}
          {editable && (
            <button type="button" className="link danger-link" onClick={() => onUnpivot(relation.name)}>
              移除樞紐
            </button>
          )}
        </div>
      </div>
    );
  }
  return <div className="dse-table">{relation.name}</div>;
}

function JoinEditor({
  relation,
  fields,
  editable,
  onSave,
  onRemove,
}: {
  relation: Extract<RelationView, { kind: "join" }>;
  fields: ModelField[];
  editable: boolean;
  onSave: (table: string, join: JoinType, on: Condition[]) => Promise<boolean>;
  onRemove: (name: string) => void;
}) {
  const right = "name" in relation.right ? relation.right.name : "";
  const inRight = (f: ModelField) => (f.physical_table ?? f.table) === right;
  const leftFields = fields.filter((f) => !inRight(f));
  const rightColumns = fields.filter(inRight).map((f) => f.column ?? "");
  const initial: Condition[] = relation.on.map((c) => {
    const left = leftFields.find((f) => f.column === c.left[1] && (f.physical_table ?? f.table) === c.left[0]) ?? leftFields.find((f) => f.column === c.left[1]);
    return [left?.id ?? "", c.op as Comparison, c.right[1]];
  });
  const [join, setJoin] = useState<JoinType>(relation.join);
  const [on, setOn] = useState<Condition[]>(initial);
  const dirty = join !== relation.join || JSON.stringify(on) !== JSON.stringify(initial);
  return (
    <div className="dse-join-right" data-join={right}>
      <div className="dse-join-type">
        <select aria-label={`${right} 的 join 類型`} value={join} disabled={!editable} onChange={(e) => setJoin(e.target.value as JoinType)}>
          {(Object.keys(JOINS) as JoinType[]).map((j) => (
            <option key={j} value={j}>
              {JOINS[j]} join
            </option>
          ))}
        </select>
        <span className="dse-table">▦ {right}</span>
        {editable && (
          <button type="button" className="link danger-link" onClick={() => onRemove(right)}>
            移除
          </button>
        )}
      </div>
      <Conditions
        conditions={on}
        left={leftFields}
        right={rightColumns.map((c) => ({ value: c, label: c }))}
        editable={editable}
        onChange={setOn}
      />
      {editable && dirty && (
        <button type="button" className="primary" onClick={() => void onSave(right, join, on.filter((c) => c[0] && c[2]))}>
          套用
        </button>
      )}
    </div>
  );
}

function Conditions({
  conditions,
  left,
  right,
  editable,
  onChange,
}: {
  conditions: Condition[];
  left: ModelField[];
  right: { value: string; label: string }[];
  editable: boolean;
  onChange: (next: Condition[]) => void;
}) {
  const set = (i: number, at: 0 | 1 | 2, value: string) =>
    onChange(conditions.map((c, k) => (k === i ? (Object.assign([...c], { [at]: value }) as Condition) : c)));
  return (
    <div className="dse-conditions">
      {conditions.map((c, i) => (
        <div key={i} className="dse-condition">
          <select aria-label="左邊的欄位" value={c[0]} disabled={!editable} onChange={(e) => set(i, 0, e.target.value)}>
            <option value="">（欄位）</option>
            {left.map((f) => (
              <option key={f.id} value={f.id}>
                {f.caption}
              </option>
            ))}
          </select>
          <select aria-label="比較" value={c[1]} disabled={!editable} onChange={(e) => set(i, 1, e.target.value)}>
            {OPS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
          <select aria-label="右邊的欄位" value={c[2]} disabled={!editable} onChange={(e) => set(i, 2, e.target.value)}>
            <option value="">（欄位）</option>
            {right.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
          {editable && conditions.length > 1 && (
            <button type="button" className="link danger-link" aria-label="移除條件" onClick={() => onChange(conditions.filter((_, k) => k !== i))}>
              ×
            </button>
          )}
        </div>
      ))}
      {editable && (
        <button type="button" className="link" onClick={() => onChange([...conditions, ["", "=", ""]])}>
          ＋ 條件
        </button>
      )}
    </div>
  );
}

function RelationshipRow({
  relationship,
  fields,
  leftCaption,
  rightCaption,
  onSave,
}: {
  relationship: DatasourceModel["relationships"][number];
  fields: ModelField[];
  leftCaption: string;
  rightCaption: string;
  onSave: (on: Condition[]) => Promise<boolean>;
}) {
  const initial: Condition[] = relationship.on.map((c) => [c.left, c.op as Comparison, c.right]);
  const [on, setOn] = useState<Condition[]>(initial);
  const dirty = JSON.stringify(on) !== JSON.stringify(initial);
  const left = fields.filter((f) => f.table === relationship.left && f.column !== null);
  const right = fields
    .filter((f) => f.table === relationship.right && f.column !== null)
    .map((f) => ({ value: f.id, label: f.caption }));
  return (
    <li className="dse-relationship" data-relationship={relationship.right}>
      <div>
        <span className="ds-link">{leftCaption}</span> ⟷ <span className="ds-link">{rightCaption}</span>
      </div>
      <Conditions conditions={on} left={left} right={right} editable onChange={setOn} />
      {dirty && (
        <button type="button" className="primary" onClick={() => void onSave(on.filter((c) => c[0] && c[2]))}>
          套用
        </button>
      )}
    </li>
  );
}

function FieldRow({
  field,
  tableCaption,
  editable,
  selected,
  onSelect,
  onUpdate,
}: {
  field: ModelField;
  tableCaption: string;
  editable: boolean;
  selected: boolean;
  onSelect: (on: boolean) => void;
  onUpdate: (body: Partial<Pick<ModelField, "caption" | "hidden" | "role" | "datatype">>) => Promise<boolean>;
}) {
  const physical = field.column !== null && field.calc === null;
  return (
    <tr className={field.hidden ? "hidden-field" : undefined} data-field={field.id}>
      <td>
        {physical && (
          <input type="checkbox" aria-label={`選取 ${field.caption}`} checked={selected} onChange={(e) => onSelect(e.target.checked)} />
        )}
      </td>
      <td>
        <input
          className="dse-field-caption"
          aria-label={`${field.caption} 的名稱`}
          defaultValue={field.caption}
          onBlur={(e) => {
            const caption = e.target.value.trim();
            if (caption && caption !== field.caption) void onUpdate({ caption });
            else e.target.value = field.caption;
          }}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        {field.calc !== null && <span className="muted"> = {field.calc}</span>}
      </td>
      <td>
        {physical && editable && field.datatype !== "spatial" ? (
          <select aria-label={`${field.caption} 的資料類型`} value={field.datatype} onChange={(e) => void onUpdate({ datatype: e.target.value })}>
            {Object.entries(TYPES)
              .filter(([t]) => t !== "spatial")
              .map(([t, label]) => (
                <option key={t} value={t}>
                  {label}
                </option>
              ))}
          </select>
        ) : (
          (TYPES[field.datatype] ?? field.datatype)
        )}
      </td>
      <td>
        <select
          aria-label={`${field.caption} 的角色`}
          value={field.role}
          disabled={field.datatype === "spatial"}
          onChange={(e) => void onUpdate({ role: e.target.value as ModelField["role"] })}
        >
          <option value="dimension">維度</option>
          <option value="measure">量值</option>
        </select>
      </td>
      <td>{tableCaption}</td>
      <td>
        <input type="checkbox" aria-label={`隱藏 ${field.caption}`} checked={field.hidden} onChange={(e) => void onUpdate({ hidden: e.target.checked })} />
      </td>
    </tr>
  );
}

function AddTableDialog({
  model,
  source,
  initialTable,
  onClose,
  onSubmit,
}: {
  model: DatasourceModel;
  source: Source;
  initialTable: string;
  onClose: () => void;
  onSubmit: (body: { table: string; how: "relationship" | "join" | "union"; target: string | null; join: JoinType; on: Condition[] }) => Promise<boolean>;
}) {
  const [table, setTable] = useState(initialTable);
  const [how, setHow] = useState<"relationship" | "join" | "union">("relationship");
  const [target, setTarget] = useState<string | null>(model.logical[0]?.id ?? null);
  const [join, setJoin] = useState<JoinType>("inner");
  const columns = useMemo(() => source.tables.find((t) => t.name === table)?.columns ?? [], [source, table]);
  const candidates = model.fields.filter(
    (f) => f.column !== null && f.calc === null && (target === null || f.table === target),
  );
  /** 預設的條件：名稱相同（不分大小寫）的第一組欄位（與 Tableau 相同自動配對）。 */
  const guess = useCallback((): Condition[] => {
    for (const f of candidates) {
      const match = columns.find((c) => c.name.toLowerCase() === (f.column ?? "").toLowerCase());
      if (match) return [[f.id, "=", match.name]];
    }
    return [["", "=", ""]];
  }, [candidates, columns]);
  const [on, setOn] = useState<Condition[]>(() => guess());
  useEffect(() => setOn(guess()), [table, target]); // eslint-disable-line react-hooks/exhaustive-deps
  const needs = how !== "union";
  const valid = !needs || on.some((c) => c[0] && c[2]);
  return (
    <Modal title={`加入資料表：${source.name}`} onClose={onClose} wide>
      <label className="admin-field">
        資料表
        <select aria-label="要加入的資料表" value={table} onChange={(e) => setTable(e.target.value)}>
          {source.tables.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}（{t.rows.toLocaleString()} 列）
            </option>
          ))}
        </select>
      </label>
      <fieldset className="dse-how">
        <legend>加入的方式</legend>
        {(Object.keys(HOW) as (keyof typeof HOW)[]).map((h) => (
          <label key={h}>
            <input type="radio" name="how" value={h} checked={how === h} onChange={() => setHow(h)} /> {HOW[h]}
          </label>
        ))}
      </fieldset>
      {model.logical.length > 1 && (
        <label className="admin-field">
          {how === "relationship" ? "關聯到" : "加在"}
          <select aria-label="目標的邏輯資料表" value={target ?? ""} onChange={(e) => setTarget(e.target.value || null)}>
            {model.logical.map((l) => (
              <option key={l.id ?? ""} value={l.id ?? ""}>
                {l.caption}
              </option>
            ))}
          </select>
        </label>
      )}
      {how === "join" && (
        <label className="admin-field">
          Join 類型
          <select aria-label="join 類型" value={join} onChange={(e) => setJoin(e.target.value as JoinType)}>
            {(Object.keys(JOINS) as JoinType[]).map((j) => (
              <option key={j} value={j}>
                {JOINS[j]} join
              </option>
            ))}
          </select>
        </label>
      )}
      {needs && (
        <>
          <div className="dse-label">{how === "relationship" ? "關聯的欄位" : "Join 的條件"}</div>
          <Conditions
            conditions={on}
            left={candidates}
            right={columns.map((c) => ({ value: c.name, label: `${c.name}（${TYPES[c.datatype] ?? c.datatype}）` }))}
            editable
            onChange={setOn}
          />
        </>
      )}
      {how === "union" && <p className="hint">欄位依名稱對應，另加上 Table Name（Excel 另有 Sheet、文字檔另有 Path）。</p>}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!valid}
          onClick={() =>
            void onSubmit({ table, how, target, join, on: needs ? on.filter((c) => c[0] && c[2]) : [] }).then((ok) => ok && onClose())
          }
        >
          加入
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

/** 新增報表或資料源時選擇檔案中的資料表（只有一張時直接用它）。 */
export function ChooseTableDialog({
  title,
  tables,
  onClose,
  onChoose,
}: {
  title: string;
  tables: InspectedTable[];
  onClose: () => void;
  onChoose: (table: string) => Promise<boolean>;
}) {
  const [table, setTable] = useState(tables[0]?.name ?? "");
  const chosen = tables.find((t) => t.name === table);
  return (
    <Modal title={title} onClose={onClose} wide>
      <ul className="dse-choose">
        {tables.map((t) => (
          <li key={t.name}>
            <label>
              <input type="radio" name="table" checked={table === t.name} onChange={() => setTable(t.name)} /> ▦ {t.name}
              <span className="muted">（{t.rows.toLocaleString()} 列、{t.columns.length} 欄）</span>
            </label>
          </li>
        ))}
      </ul>
      {chosen && (
        <p className="muted dse-columns">
          欄位：{chosen.columns.map((c) => `${c.name}（${TYPES[c.datatype] ?? c.datatype}）`).join("、")}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!table} onClick={() => void onChoose(table).then((ok) => ok && onClose())}>
          確定
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}
