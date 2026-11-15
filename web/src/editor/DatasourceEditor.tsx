import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { DataGrid } from "../DatasourceView";
import { pickDataPath, pickFolderPath, toast } from "../desktop";
import { Modal } from "../Server";
import type { DatasourcePreview } from "../types";
import { PickConnectionDialog } from "./ConnectServer";
import { DatasourceFilters, ExtractSettings } from "./DatasourceFilters";
import {
  connectionsApi,
  datasourceApi,
  type Comparison,
  type Condition,
  type DatasourceModel,
  type InspectedColumn,
  type InspectedTable,
  type JoinType,
  type LogicalView,
  type ModelConnection,
  type ModelField,
  type RelationView,
  type RelationshipOptions,
  type RemoteTable,
  type Side,
  sideFilled,
} from "./datasourceApi";

// 資料源分頁的編輯（Tableau Desktop 的「資料來源」分頁）：左邊是連線的檔案與其中的資料表（資料庫的連線為
// 資料庫中的資料表與「新增自訂 SQL」），中間是邏輯資料表與關聯（雙擊邏輯資料表看到的 join、聯集、樞紐），
// 下面是欄位（改名、隱藏、資料類型、角色）與資料的預覽。「加入連線」可以再加上檔案或另一個資料庫：不同連線的
// 資料表在本機合併（跨資料庫的 join）。

const JOINS: Record<JoinType, string> = { inner: "內部", left: "左側", right: "右側", full: "完整外部" };
const OPS: Comparison[] = ["=", "!=", "<", "<=", ">", ">="];
/** join 的條件另有空間 join 的「相交」（Tableau 的 Intersects：兩邊都是空間欄位或傳回形狀的 join 計算）。 */
const JOIN_OPS: Comparison[] = [...OPS, "intersects"];
const OP_LABELS: Partial<Record<Comparison, string>> = { intersects: "相交" };
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
  /** 資料庫的資料表或自訂 SQL */
  remote?: { table_path: string[] } | { sql: string };
  /** 資料庫的資料表所在的連線 */
  connection?: string;
  name: string;
  tables: InspectedTable[];
}

/** 連線的顯示名稱：資料庫的類型與資料庫（或伺服器）。 */
function connectionLabel(connection: ModelConnection, label?: string | null): string {
  return [label ?? connection.dbclass, connection.database ?? connection.server].filter(Boolean).join("／");
}

/** 資料庫的資料表（列數不明，rows 為 -1）。 */
function remoteTable(name: string, columns: InspectedColumn[]): InspectedTable {
  return { name, sheet: null, layer: null, rows: -1, columns };
}

const CUSTOM_SQL = "Custom SQL Query";

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
  /** 自訂 SQL 的對話框：name 為 null 時是新增；connection 為資料庫的連線 */
  const [sqlDialog, setSqlDialog] = useState<{ name: string | null; sql: string; connection?: string } | null>(null);
  /** 「加入連線＞資料庫」選了、還沒有加入資料表的連線 */
  const [extra, setExtra] = useState<ModelConnection[]>([]);
  const [picking, setPicking] = useState(false);
  const [labels, setLabels] = useState<Record<string, string>>({});
  useEffect(() => {
    connectionsApi
      .connectors()
      .then((all) => setLabels(Object.fromEntries(all.map((c) => [c.dbclass, c.label]))))
      .catch(() => setLabels({}));
  }, []);
  /** 萬用字元聯集的對話框（檔案） */
  const [wildcard, setWildcard] = useState<{ file: string; name: string; format?: string } | null>(null);
  const [initialSql, setInitialSql] = useState(false);

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

  /** 加入資料庫的資料表：先取得欄位（選擇關聯、join 的欄位）。connection：資料表所在的連線。 */
  const addRemote = async (table: RemoteTable, connection: string) => {
    try {
      const columns = await datasourceApi.remoteColumns(workbook, datasource, { table: table.path, connection });
      setAdding({
        source: {
          remote: { table_path: table.path },
          connection,
          name: table.path.join("."),
          tables: [remoteTable(table.name, columns)],
        },
        table: table.name,
      });
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  if (error) return <div className="placeholder error">無法開啟資料源：{error}</div>;
  if (!model) return <div className="placeholder">載入中…</div>;
  const fields = model.fields;
  const logicalCaption = (id: string) => model.logical.find((l) => l.id === id)?.caption ?? id;
  const connections = model.connections ?? (model.connection ? [model.connection] : []);
  const known = new Set(connections.map((c) => c.connection_id));
  const panes = [...connections, ...extra.filter((c) => !known.has(c.connection_id))];
  const sourceLabel = (id: string) => {
    const found = panes.find((c) => c.connection_id === id);
    return found ? connectionLabel(found, labels[found.dbclass]) : id;
  };
  const sources = model.sources ?? [];
  const hasInitialSql = sources.some((c) => !!c.initial_sql);
  const kindLabel = model.datasource.federated
    ? "跨資料庫"
    : ({ extract: "萃取", file: "檔案", live: "即時" }[model.datasource.kind] ?? model.datasource.kind);

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
        <span className={`ds-badge ${model.datasource.kind}`} title={model.datasource.federated ? "不同連線的資料表在本機合併" : undefined}>
          {kindLabel}
        </span>
        {sources.length > 0 && (
          <button
            type="button"
            className="dse-initial-sql"
            data-set={hasInitialSql ? "true" : "false"}
            title="連上資料庫時先執行的 SQL（Tableau 的「初始 SQL」），例如設定工作階段或建立暫存資料表"
            onClick={() => setInitialSql(true)}
          >
            Initial SQL…{hasInitialSql ? " ✓" : ""}
          </button>
        )}
        <button type="button" className="primary" onClick={onOpenSheet}>
          前往工作表
        </button>
      </header>
      {!model.editable && (
        <p className="hint">這個資料源是萃取（或連線沒有資料表的定義），只能修改資料源的名稱與欄位的名稱、隱藏、角色。</p>
      )}
      <div className="dse-body">
        <aside className="dse-files" aria-label="連線">
          <h2>連線</h2>
          {panes.map((c) => (
            <LivePane
              key={c.connection_id}
              workbook={workbook}
              datasource={datasource}
              model={model}
              connection={c}
              label={labels[c.dbclass] ?? null}
              onAdd={(t) => void addRemote(t, c.connection_id)}
              onCustomSql={() => setSqlDialog({ name: null, sql: "", connection: c.connection_id })}
            />
          ))}
          {model.files.map((file) => (
            <div key={file.path} className="dse-file">
              <div className="dse-file-name" title={file.path}>
                {file.name}
              </div>
              {!file.available && (
                <p className="muted">{file.error ?? "報表中沒有這個檔案的複本（以「其他檔案…」重新選擇）"}</p>
              )}
              {model.editable && (file.format === "excel" || file.format === "text") && (
                <label className="dse-interpreter" title="去掉工作表或文字檔上面的標題、說明與空白的列，找出標題列（Tableau 的資料解譯器）">
                  <input
                    type="checkbox"
                    checked={!!file.interpreted}
                    onChange={(e) =>
                      void change(
                        () => datasourceApi.setInterpreter(workbook, datasource, file.path, e.target.checked),
                        e.target.checked ? "已使用資料解譯器" : "已停用資料解譯器",
                      )
                    }
                  />{" "}
                  使用資料解譯器
                </label>
              )}
              {model.editable && (file.format === "excel" || file.format === "text") && (
                <button
                  type="button"
                  className="link dse-wildcard"
                  title="資料夾中檔名符合模式的檔案接在這個檔案的資料表下面（Tableau 的萬用字元聯集）"
                  onClick={() => setWildcard({ file: file.path, name: file.name, format: file.format })}
                >
                  萬用字元聯集…
                </button>
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
                    <span className="muted">（{t.rows.toLocaleString()} 列{t.interpreted ? "，已解譯" : ""}）</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {model.editable && (
            <div className="dse-add-connection" role="group" aria-label="加入連線">
              <span className="muted">加入連線：</span>
              <button type="button" className="dse-other" onClick={() => void otherFile()}>
                {model.files.length ? "其他檔案…" : "檔案…"}
              </button>
              <button type="button" className="dse-other" onClick={() => setPicking(true)}>
                資料庫…
              </button>
            </div>
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
                onEditSql={(name, sql, connection) => setSqlDialog({ name, sql, connection })}
                sourceLabel={model.datasource.federated ? sourceLabel : undefined}
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
                  onSave={(on, options) => change(() => datasourceApi.setRelationship(workbook, datasource, r.right, on, options), "已修改關聯")}
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
      <ExtractSettings key={`x${version}`} workbook={workbook} datasource={datasource} onChanged={() => setVersion((v) => v + 1)} />
      <DatasourceFilters workbook={workbook} datasource={datasource} onChanged={() => setVersion((v) => v + 1)} />
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
                datasourceApi.addTable(
                  workbook,
                  datasource,
                  adding.source.remote
                    ? { ...body, table: null, ...adding.source.remote, connection: adding.source.connection }
                    : { ...body, file: adding.source.file, path: adding.source.path },
                ),
              "已加入資料表",
            )
          }
        />
      )}
      {picking && (
        <PickConnectionDialog
          onClose={() => setPicking(false)}
          onPick={(entry) =>
            setExtra((all) => [
              ...all.filter((c) => c.connection_id !== entry.id),
              {
                connection_id: entry.id,
                dbclass: entry.dbclass,
                server: entry.server,
                port: entry.port,
                database: entry.database,
                schema: entry.schema,
              },
            ])
          }
        />
      )}
      {initialSql && (
        <InitialSqlDialog
          sources={sources}
          label={(c) => connectionLabel(c, labels[c.dbclass])}
          extract={model.datasource.kind === "extract"}
          onClose={() => setInitialSql(false)}
          onSubmit={async (sql, connection) => {
            let failure: string | null = null;
            const saved = await change(async () => {
              const updated = await datasourceApi.setInitialSql(workbook, datasource, sql, connection);
              failure = updated.initial_sql_error ?? null;
              return updated;
            }, sql.trim() ? "已設定 Initial SQL" : "已清除 Initial SQL");
            return saved ? { error: failure } : null;
          }}
        />
      )}
      {wildcard && (
        <WildcardDialog
          model={model}
          file={wildcard}
          onClose={() => setWildcard(null)}
          onSubmit={(body) => change(() => datasourceApi.wildcardUnion(workbook, datasource, body), "已建立萬用字元聯集")}
        />
      )}
      {sqlDialog && (
        <CustomSqlDialog
          initial={sqlDialog.sql}
          editing={sqlDialog.name}
          check={(sql) => datasourceApi.remoteColumns(workbook, datasource, { sql, connection: sqlDialog.connection })}
          onClose={() => setSqlDialog(null)}
          onSubmit={async (sql, columns) => {
            if (sqlDialog.name === null) {
              setSqlDialog(null);
              setAdding({
                source: {
                  remote: { sql },
                  connection: sqlDialog.connection,
                  name: "自訂 SQL",
                  tables: [remoteTable(CUSTOM_SQL, columns)],
                },
                table: CUSTOM_SQL,
              });
              return true;
            }
            const name = sqlDialog.name;
            return change(() => datasourceApi.setCustomSql(workbook, datasource, name, sql), "已修改自訂 SQL");
          }}
        />
      )}
    </div>
  );
}

/** 萬用字元聯集（Tableau 的「萬用字元聯集」）：資料夾、檔名與工作表名稱的模式；符合的檔案複製進報表。 */
function WildcardDialog({
  model,
  file,
  onClose,
  onSubmit,
}: {
  model: DatasourceModel;
  file: { file: string; name: string; format?: string };
  onClose: () => void;
  onSubmit: (body: { table: string; folder: string; files: string; sheets: string | null; recursive: boolean }) => Promise<boolean>;
}) {
  // 可以接上的資料表：這個檔案的資料表（單一資料表或已經是聯集）
  const targets = useMemo(() => {
    const found: string[] = [];
    const visit = (relation: RelationView) => {
      if (relation.kind === "table") found.push(relation.name);
      else if (relation.kind === "union") found.push(relation.name);
      else if (relation.kind === "join") {
        visit(relation.left);
        visit(relation.right);
      }
    };
    model.logical.forEach((l) => visit(l.relation));
    return found;
  }, [model]);
  const excel = file.format === "excel";
  // 預設的模式：檔名中的數字換成 *（例如 Sales 2023.xlsx → Sales *.xlsx）
  const [files, setFiles] = useState(file.name.replace(/\d+/g, "*").replace(/\*+/g, "*"));
  const [sheets, setSheets] = useState("");
  const [folder, setFolder] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [table, setTable] = useState(targets[0] ?? "");
  return (
    <Modal title={`萬用字元聯集：${file.name}`} onClose={onClose}>
      <label className="admin-field">
        接在資料表下面
        <select aria-label="接在哪張資料表下面" value={table} onChange={(e) => setTable(e.target.value)}>
          {targets.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        搜尋的資料夾（作者電腦上的完整路徑）
        <span className="dse-folder">
          <input aria-label="搜尋的資料夾" value={folder} onChange={(e) => setFolder(e.target.value)} />
          <button type="button" onClick={() => void pickFolderPath().then((path) => path && setFolder(path))}>
            選擇…
          </button>
        </span>
      </label>
      <label className="admin-field">
        檔名的模式（* 為任意字元）
        <input aria-label="檔名的模式" value={files} onChange={(e) => setFiles(e.target.value)} />
      </label>
      {excel && (
        <label className="admin-field">
          工作表名稱的模式（空白為全部）
          <input aria-label="工作表名稱的模式" value={sheets} onChange={(e) => setSheets(e.target.value)} />
        </label>
      )}
      <label>
        <input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} /> 包含子資料夾
      </label>
      <p className="hint">欄位依名稱對應，另加上 Path（檔案的路徑）{excel ? "與 Sheet" : ""}；符合的檔案複製進報表（建立時的快照）。</p>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!table || !folder.trim() || !files.trim()}
          onClick={() =>
            void onSubmit({ table, folder: folder.trim(), files: files.trim(), sheets: sheets.trim() || null, recursive }).then(
              (ok) => ok && onClose(),
            )
          }
        >
          聯集
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

/** 資料庫的連線：伺服器與資料庫、結構描述中的資料表（點選加入）與「新增自訂 SQL」。 */
function LivePane({
  workbook,
  datasource,
  model,
  connection,
  label,
  onAdd,
  onCustomSql,
}: {
  workbook: string;
  datasource: string;
  model: DatasourceModel;
  /** 資料源中的資料庫，或「加入連線＞資料庫」選的連線 */
  connection: ModelConnection;
  label: string | null;
  onAdd: (table: RemoteTable) => void;
  onCustomSql: () => void;
}) {
  const id = connection.connection_id;
  const [schema, setSchema] = useState(connection.schema ?? "");
  const [tables, setTables] = useState<RemoteTable[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const load = useCallback(
    (wanted: string | null) => {
      setLoadError(null);
      datasourceApi
        .remoteTables(workbook, datasource, wanted, id)
        .then((r) => setTables(r.tables))
        .catch((e: Error) => {
          setTables([]);
          setLoadError(e.message);
        });
    },
    [workbook, datasource, id],
  );
  useEffect(() => load(connection.schema ?? null), [load, connection.schema]);
  const query = search.trim().toLowerCase();
  const shown = (tables ?? []).filter((t) => !query || t.path.join(".").toLowerCase().includes(query));
  return (
    <div className="dse-file dse-server" data-connection={connection.connection_id}>
      <div className="dse-file-name">{label ?? connection.dbclass}</div>
      <div className="muted">{[connection.server, connection.database].filter(Boolean).join("／")}</div>
      <div className="dse-schema">
        <input aria-label="結構描述" placeholder="結構描述" value={schema} onChange={(e) => setSchema(e.target.value)} />
        <button type="button" className="dse-other" onClick={() => load(schema.trim() || null)}>
          重新整理
        </button>
      </div>
      <input type="search" className="dse-search" aria-label="搜尋資料表" placeholder="搜尋資料表" value={search} onChange={(e) => setSearch(e.target.value)} />
      {loadError && (
        <p className="error">
          {loadError}
          <br />
          <span className="muted">（還沒登入這個連線時，請從「加入連線：資料庫…」登入）</span>
        </p>
      )}
      {tables === null && !loadError && <p className="muted">載入中…</p>}
      <ul>
        {shown.map((t) => (
          <li key={t.path.join(".")}>
            <button
              type="button"
              className="link dse-add"
              data-table={t.path.join(".")}
              disabled={!model.editable}
              title="加入這張資料表"
              onClick={() => onAdd(t)}
            >
              {t.kind === "view" ? "◫" : "▦"} {t.name}
            </button>
            {t.kind === "view" && <span className="muted">（檢視）</span>}
          </li>
        ))}
      </ul>
      {model.editable && (
        <button type="button" className="dse-other" onClick={onCustomSql}>
          新增自訂 SQL…
        </button>
      )}
    </div>
  );
}

const INITIAL_SQL_PARAMETERS: [string, string][] = [
  ["[TableauServerUser]", "登入伺服器的帳號"],
  ["[TableauServerUserFull]", "帶網域的帳號（網域\\帳號）"],
  ["[TableauApp]", "應用程式（Tableau Server 或 Tableau Desktop Professional）"],
  ["[TableauVersion]", "版本"],
  ["[WorkbookName]", "報表的名稱"],
];

/** 連線的 Initial SQL（Tableau 的「資料＞初始 SQL」）：連上資料庫時執行一次。確定後存檔並在這個連線上
 *  試一次；失敗時留在對話框顯示原因（已經存檔，可以修改後再按確定）。 */
function InitialSqlDialog({
  sources,
  label,
  extract,
  onClose,
  onSubmit,
}: {
  sources: ModelConnection[];
  label: (connection: ModelConnection) => string;
  extract: boolean;
  onClose: () => void;
  onSubmit: (sql: string, connection: string) => Promise<{ error: string | null } | null>;
}) {
  const [connection, setConnection] = useState(sources[0].connection_id);
  const current = sources.find((c) => c.connection_id === connection) ?? sources[0];
  const [sql, setSql] = useState(current.initial_sql ?? "");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);
  /** 插入之後游標要放的位置：新的內容畫上去的同時設定（之後的輸入接在插入的文字後面） */
  const cursor = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (cursor.current === null || !area.current) return;
    area.current.focus();
    area.current.setSelectionRange(cursor.current, cursor.current);
    cursor.current = null;
  }, [sql]);
  const insert = (text: string) => {
    const start = area.current?.selectionStart ?? sql.length;
    const end = area.current?.selectionEnd ?? sql.length;
    cursor.current = start + text.length;
    setSql(sql.slice(0, start) + text + sql.slice(end));
    setFailure(null);
  };
  return (
    <Modal title="Initial SQL" onClose={onClose} wide>
      {sources.length > 1 && (
        <label className="dse-initial-connection">
          連線{" "}
          <select
            aria-label="Initial SQL 的連線"
            value={connection}
            onChange={(e) => {
              const next = sources.find((c) => c.connection_id === e.target.value) ?? sources[0];
              setConnection(next.connection_id);
              setSql(next.initial_sql ?? "");
              setFailure(null);
            }}
          >
            {sources.map((c) => (
              <option key={c.connection_id} value={c.connection_id}>
                {label(c)}
                {c.initial_sql ? " ✓" : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      <textarea
        ref={area}
        className="cs-sql"
        aria-label="Initial SQL"
        spellCheck={false}
        rows={10}
        placeholder={"例如：SET search_path TO sales;\nCREATE TEMP TABLE my_regions AS SELECT … WHERE owner = '[TableauServerUser]';"}
        value={sql}
        onChange={(e) => {
          setSql(e.target.value);
          setFailure(null);
        }}
      />
      <div className="dse-initial-insert">
        <select
          aria-label="插入參數"
          value=""
          onChange={(e) => {
            if (e.target.value) insert(e.target.value);
          }}
        >
          <option value="">插入參數…</option>
          {INITIAL_SQL_PARAMETERS.map(([name, text]) => (
            <option key={name} value={name}>
              {name}　{text}
            </option>
          ))}
        </select>
        <button type="button" className="link" title="只在伺服器上執行的部分（桌面版不執行）" onClick={() => insert("<ServerOnly></ServerOnly>")}>
          插入 &lt;ServerOnly&gt;
        </button>
      </div>
      <p className="hint">
        {extract
          ? "這個資料源是萃取：Initial SQL 在更新萃取、連到來源資料庫時執行。"
          : "連上資料庫時執行一次（開啟報表、更新資料時），多句以分號分開。"}
        發布到伺服器後，參數以每位檢視者的帳號代入；用到帳號的 Initial SQL 每位檢視者各自一條連線。
      </p>
      {failure && (
        <p className="error" role="alert">
          已經存檔，但在這個連線上執行失敗：{failure}
          <br />
          <span className="muted">（桌面版不執行 &lt;ServerOnly&gt; 的部分，沒有登入伺服器時帳號是空的；可以修改後再按「確定」，或直接關閉）</span>
        </p>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={() =>
            void (async () => {
              setBusy(true);
              const result = await onSubmit(sql, connection);
              setBusy(false);
              if (!result) return;
              if (result.error) setFailure(result.error);
              else onClose();
            })()
          }
        >
          確定
        </button>
        <button type="button" onClick={onClose}>
          {failure ? "關閉" : "取消"}
        </button>
      </div>
    </Modal>
  );
}

/** 新增或修改自訂 SQL：先檢查欄位（在資料庫執行，取前幾列判斷型別）。 */
function CustomSqlDialog({
  initial,
  editing,
  check,
  onClose,
  onSubmit,
}: {
  initial: string;
  editing: string | null;
  check: (sql: string) => Promise<InspectedColumn[]>;
  onClose: () => void;
  onSubmit: (sql: string, columns: InspectedColumn[]) => Promise<boolean>;
}) {
  const [sql, setSql] = useState(initial);
  const [columns, setColumns] = useState<InspectedColumn[] | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const verify = async (): Promise<InspectedColumn[] | null> => {
    setBusy(true);
    setCheckError(null);
    try {
      const found = await check(sql);
      setColumns(found);
      return found;
    } catch (e) {
      setCheckError((e as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={editing ? `編輯自訂 SQL：${editing}` : "新增自訂 SQL"} onClose={onClose} wide>
      <textarea
        className="cs-sql"
        aria-label="自訂 SQL"
        spellCheck={false}
        rows={10}
        value={sql}
        onChange={(e) => {
          setSql(e.target.value);
          setColumns(null);
        }}
      />
      {checkError && <p className="error">{checkError}</p>}
      {columns && (
        <p className="muted dse-columns">欄位：{columns.map((c) => `${c.name}（${TYPES[c.datatype] ?? c.datatype}）`).join("、")}</p>
      )}
      {editing && <p className="hint">欄位依名稱對應：名稱相同的欄位保留（含名稱、角色與計算的參照），新的欄加入，不在查詢中的欄位移除（有參照時不能移除）。</p>}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !sql.trim()}
          onClick={() =>
            void (async () => {
              const found = columns ?? (await verify());
              if (!found) return;
              if (await onSubmit(sql, found)) onClose();
            })()
          }
        >
          {editing ? "套用" : "下一步"}
        </button>
        <button type="button" disabled={busy || !sql.trim()} onClick={() => void verify()}>
          檢查欄位
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
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
  onEditSql,
  sourceLabel,
}: {
  logical: LogicalView;
  model: DatasourceModel;
  active: boolean;
  onPreview: () => void;
  onChange: (run: () => Promise<DatasourceModel>, done?: string) => Promise<boolean>;
  workbook: string;
  datasource: string;
  removable: boolean;
  onEditSql: (name: string, sql: string, connection?: string) => void;
  /** 跨資料庫的 join：資料表所在的連線的名稱 */
  sourceLabel?: (connection: string) => string;
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
        onEditSql={onEditSql}
        sourceLabel={sourceLabel}
      />
    </div>
  );
}

/** 跨資料庫的 join：資料表所在的連線（檔案的資料表沒有）。 */
function SourceTag({ connection, label }: { connection?: string; label?: (connection: string) => string }) {
  if (!connection || !label) return null;
  return <span className="muted dse-source">（{label(connection)}）</span>;
}

function RelationTree({
  relation,
  fields,
  editable,
  onRemove,
  onUnpivot,
  onSetJoin,
  onEditSql,
  sourceLabel,
}: {
  relation: RelationView;
  fields: ModelField[];
  editable: boolean;
  onRemove: (name: string) => void;
  onUnpivot: (name: string) => void;
  onSetJoin: (table: string, join: JoinType, on: Condition[]) => Promise<boolean>;
  onEditSql: (name: string, sql: string, connection?: string) => void;
  sourceLabel?: (connection: string) => string;
}) {
  const nested = { fields, editable, onRemove, onUnpivot, onSetJoin, onEditSql, sourceLabel };
  if (relation.kind === "sql") {
    return (
      <div className="dse-table dse-sql" data-sql={relation.name}>
        ▦ {relation.name}
        <SourceTag connection={relation.connection} label={sourceLabel} />
        {editable && (
          <button type="button" className="link" onClick={() => onEditSql(relation.name, relation.sql, relation.connection)}>
            編輯 SQL…
          </button>
        )}
      </div>
    );
  }
  if (relation.kind === "table") {
    return (
      <div className="dse-table" data-table={relation.name}>
        ▦ {relation.name}
        <SourceTag connection={relation.connection} label={sourceLabel} />
      </div>
    );
  }
  if (relation.kind === "join") {
    return (
      <div className="dse-join">
        <RelationTree relation={relation.left} {...nested} />
        <JoinEditor
          relation={relation}
          fields={fields}
          editable={editable}
          onSave={onSetJoin}
          onRemove={onRemove}
          onEditSql={onEditSql}
          sourceLabel={sourceLabel}
        />
      </div>
    );
  }
  if (relation.kind === "union") {
    return (
      <div className="dse-union">
        <div className="dse-label">
          {relation.wildcard
            ? `萬用字元聯集「${relation.wildcard.files}」${relation.wildcard.sheets ? `／工作表「${relation.wildcard.sheets}」` : ""}（${relation.parts.length} 張資料表）`
            : `聯集（${relation.parts.length} 張資料表上下相接）`}
        </div>
        {relation.parts.map((p) => (
          <div key={"name" in p ? p.name : ""} className="dse-table">
            ▦ {"name" in p ? p.name : ""}
            {(p.kind === "sql" || p.kind === "table") && <SourceTag connection={p.connection} label={sourceLabel} />}
            {p.kind === "sql" && editable && (
              <button type="button" className="link" onClick={() => onEditSql(p.name, p.sql, p.connection)}>
                編輯 SQL…
              </button>
            )}
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
        <RelationTree relation={relation.source} {...nested} />
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
  onEditSql,
  sourceLabel,
}: {
  relation: Extract<RelationView, { kind: "join" }>;
  fields: ModelField[];
  editable: boolean;
  onSave: (table: string, join: JoinType, on: Condition[]) => Promise<boolean>;
  onRemove: (name: string) => void;
  onEditSql?: (name: string, sql: string, connection?: string) => void;
  sourceLabel?: (connection: string) => string;
}) {
  const right = "name" in relation.right ? relation.right.name : "";
  const rightSql = relation.right.kind === "sql" ? relation.right.sql : null;
  const rightConnection =
    relation.right.kind === "sql" || relation.right.kind === "table" ? relation.right.connection : undefined;
  const inRight = (f: ModelField) => (f.physical_table ?? f.table) === right;
  const leftFields = fields.filter((f) => !inRight(f));
  const rightColumns = fields.filter(inRight).map((f) => f.column ?? "");
  const initial: Condition[] = relation.on.map((c) => {
    const left = leftFields.find((f) => f.column === c.left[1] && (f.physical_table ?? f.table) === c.left[0]) ?? leftFields.find((f) => f.column === c.left[1]);
    const leftSide: Side = c.left_formula ? { formula: c.left_formula } : (left?.id ?? "");
    const rightSide: Side = c.right_formula ? { formula: c.right_formula } : (c.right[1] ?? "");
    return [leftSide, c.op as Comparison, rightSide];
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
        <span className="dse-table" data-table={right}>
          ▦ {right}
          <SourceTag connection={rightConnection} label={sourceLabel} />
        </span>
        {editable && rightSql !== null && onEditSql && (
          <button type="button" className="link" onClick={() => onEditSql(right, rightSql, rightConnection)}>
            編輯 SQL…
          </button>
        )}
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
        calcLabel="建立 Join 計算…"
        ops={JOIN_OPS}
      />
      {editable && dirty && (
        <button type="button" className="primary" onClick={() => void onSave(right, join, on.filter((c) => sideFilled(c[0]) && sideFilled(c[2])))}>
          套用
        </button>
      )}
    </div>
  );
}

const CALC = "__calc__";

/** 條件的一邊：選欄位，或「建立計算…」改成輸入公式（Tableau 的 join 計算、關聯計算）。 */
function SidePicker({
  side,
  options,
  label,
  calcLabel,
  editable,
  onChange,
}: {
  side: Side;
  options: { value: string; label: string }[];
  label: string;
  calcLabel: string;
  editable: boolean;
  onChange: (side: Side) => void;
}) {
  if (typeof side !== "string") {
    return (
      <span className="dse-calc">
        <input
          aria-label={`${label}的計算`}
          className="dse-calc-input"
          spellCheck={false}
          placeholder="例如 LEFT([欄位], 3)"
          value={side.formula}
          disabled={!editable}
          onChange={(e) => onChange({ formula: e.target.value })}
        />
        {editable && (
          <button type="button" className="link" title="改回欄位" aria-label={`${label}改回欄位`} onClick={() => onChange("")}>
            ↺
          </button>
        )}
      </span>
    );
  }
  return (
    <select
      aria-label={label}
      value={side}
      disabled={!editable}
      onChange={(e) => {
        if (e.target.value !== CALC) {
          onChange(e.target.value);
          return;
        }
        // 以目前選的欄位開始公式（與 Tableau 相同）
        const current = options.find((o) => o.value === side);
        onChange({ formula: current ? `[${current.label.replace(/（.*）$/, "").replace(/]/g, "]]")}]` : "" });
      }}
    >
      <option value="">（欄位）</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
      {editable && <option value={CALC}>{calcLabel}</option>}
    </select>
  );
}

function Conditions({
  conditions,
  left,
  right,
  editable,
  onChange,
  calcLabel,
  ops = OPS,
}: {
  conditions: Condition[];
  left: ModelField[];
  right: { value: string; label: string }[];
  editable: boolean;
  onChange: (next: Condition[]) => void;
  /** 「建立計算」的選項名稱（join 計算或關聯計算） */
  calcLabel: string;
  /** 可選的比較（join 另有空間 join 的相交） */
  ops?: Comparison[];
}) {
  const set = (i: number, at: 0 | 1 | 2, value: Side) =>
    onChange(conditions.map((c, k) => (k === i ? (Object.assign([...c], { [at]: value }) as Condition) : c)));
  const leftOptions = left.map((f) => ({ value: f.id, label: f.caption }));
  return (
    <div className="dse-conditions">
      {conditions.map((c, i) => (
        <div key={i} className="dse-condition">
          <SidePicker side={c[0]} options={leftOptions} label="左邊的欄位" calcLabel={calcLabel} editable={editable} onChange={(v) => set(i, 0, v)} />
          <select aria-label="比較" value={c[1]} disabled={!editable} onChange={(e) => set(i, 1, e.target.value as Comparison)}>
            {ops.map((op) => (
              <option key={op} value={op} title={op === "intersects" ? "Intersects：空間 join（兩邊都是空間欄位）" : undefined}>
                {OP_LABELS[op] ?? op}
              </option>
            ))}
          </select>
          <SidePicker side={c[2]} options={right} label="右邊的欄位" calcLabel={calcLabel} editable={editable} onChange={(v) => set(i, 2, v)} />
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
  onSave: (on: Condition[], options: RelationshipOptions) => Promise<boolean>;
}) {
  const initial: Condition[] = relationship.on.map((c) => [
    c.left_formula ? { formula: c.left_formula } : c.left,
    c.op as Comparison,
    c.right_formula ? { formula: c.right_formula } : c.right,
  ]);
  const [on, setOn] = useState<Condition[]>(initial);
  const saved: RelationshipOptions = relationship.options ?? { left_unique: false, right_unique: false, left_complete: false, right_complete: false };
  const [options, setOptions] = useState<RelationshipOptions>(saved);
  const dirty = JSON.stringify(on) !== JSON.stringify(initial) || JSON.stringify(options) !== JSON.stringify(saved);
  const option = (key: keyof RelationshipOptions, label: string, off: string, onLabel: string) => (
    <label className="inline">
      {label}
      <select aria-label={label} value={options[key] ? "1" : "0"} onChange={(e) => setOptions({ ...options, [key]: e.target.value === "1" })}>
        <option value="0">{off}</option>
        <option value="1">{onLabel}</option>
      </select>
    </label>
  );
  const left = fields.filter((f) => f.table === relationship.left && f.column !== null);
  const right = fields
    .filter((f) => f.table === relationship.right && f.column !== null)
    .map((f) => ({ value: f.id, label: f.caption }));
  return (
    <li className="dse-relationship" data-relationship={relationship.right}>
      <div>
        <span className="ds-link">{leftCaption}</span> ⟷ <span className="ds-link">{rightCaption}</span>
      </div>
      <Conditions conditions={on} left={left} right={right} editable onChange={setOn} calcLabel="建立關聯計算…" />
      <details className="dse-performance">
        <summary title="Tableau 的「效能選項」：資料表之間的基數與參考完整性。這裡只是記下設定——查詢一律以最保守的方式計算（多對多、部分記錄相符），設定與資料相符時數字相同">
          效能選項
        </summary>
        <div className="row">
          {option("left_unique", `${leftCaption} 的基數`, "多", "一")}
          {option("right_unique", `${rightCaption} 的基數`, "多", "一")}
        </div>
        <div className="row">
          {option("left_complete", `${leftCaption} 的參考完整性`, "部分記錄相符", "所有記錄相符")}
          {option("right_complete", `${rightCaption} 的參考完整性`, "部分記錄相符", "所有記錄相符")}
        </div>
      </details>
      {dirty && (
        <button type="button" className="primary" onClick={() => void onSave(on.filter((c) => sideFilled(c[0]) && sideFilled(c[2])), options)}>
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
  const valid = !needs || on.some((c) => sideFilled(c[0]) && sideFilled(c[2]));
  /** 自訂 SQL 不能聯集（與 Tableau 相同） */
  const ways = (Object.keys(HOW) as (keyof typeof HOW)[]).filter((h) => !(h === "union" && source.remote && "sql" in source.remote));
  return (
    <Modal title={`加入資料表：${source.name}`} onClose={onClose} wide>
      <label className="admin-field">
        資料表
        <select aria-label="要加入的資料表" value={table} onChange={(e) => setTable(e.target.value)}>
          {source.tables.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
              {t.rows >= 0 ? `（${t.rows.toLocaleString()} 列）` : ""}
            </option>
          ))}
        </select>
      </label>
      <fieldset className="dse-how">
        <legend>加入的方式</legend>
        {ways.map((h) => (
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
            calcLabel={how === "relationship" ? "建立關聯計算…" : "建立 Join 計算…"}
          />
        </>
      )}
      {how === "union" && (
        <p className="hint">
          {source.remote ? "欄位依名稱對應，另加上 Table Name。" : "欄位依名稱對應，另加上 Table Name（Excel 另有 Sheet、文字檔另有 Path）。"}
        </p>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!valid}
          onClick={() =>
            void onSubmit({ table, how, target, join, on: needs ? on.filter((c) => sideFilled(c[0]) && sideFilled(c[2])) : [] }).then(
              (ok) => ok && onClose(),
            )
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
