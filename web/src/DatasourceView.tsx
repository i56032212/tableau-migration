import { useEffect, useState } from "react";
import { api } from "./api";
import type { DatasourceConnection, DatasourceField, DatasourcePreview, DatasourceSummary, Scalar } from "./types";

/** 資料格每次顯示的列數（Tableau 的資料源分頁預設 1,000 列）。 */
const LIMITS = [100, 1000];

const DATATYPES: Record<string, string> = {
  string: "Abc",
  integer: "#",
  real: "#",
  date: "日期",
  datetime: "日期時間",
  boolean: "T|F",
};
const KINDS: Record<DatasourceField["kind"], string> = { column: "欄位", calc: "計算欄位", set: "集合" };
const ROLES: Record<string, string> = { dimension: "維度", measure: "量值" };

/** 資料源分頁（Tableau Desktop 的「資料來源」分頁）：連線、資料表與關聯、資料源篩選，以及資料與欄位。 */
export function DatasourceView({ workbook, datasource }: { workbook: string; datasource: DatasourceSummary }) {
  const [table, setTable] = useState<string | null>(datasource.tables[0]?.id ?? null);
  const [limit, setLimit] = useState(LIMITS[0]);
  const [tab, setTab] = useState<"data" | "fields">("data");
  const [preview, setPreview] = useState<DatasourcePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setTable(datasource.tables[0]?.id ?? null);
  }, [datasource]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .preview(workbook, datasource.id, table, limit)
      .then((result) => {
        if (cancelled) return;
        setPreview(result);
        setError(null);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workbook, datasource.id, table, limit]);

  const tableCaption = datasource.tables.find((t) => t.id === table)?.caption;
  const fields = datasource.model && tableCaption ? datasource.fields.filter((f) => f.table === tableCaption || f.table === null) : datasource.fields;

  return (
    <div className="datasource" data-datasource={datasource.id}>
      <header className="ds-header">
        <h1>{datasource.caption}</h1>
        <span className={`ds-badge ${datasource.connection.kind}`}>
          {{ extract: "萃取", file: "檔案", live: "即時" }[datasource.connection.kind]}
        </span>
        {datasource.published && <span className="ds-badge published">已發布的資料源</span>}
      </header>
      <div className="ds-top">
        <section className="ds-side" aria-label="連線">
          <h2>連線</h2>
          <Connection connection={datasource.connection} />
          {datasource.published && (
            <dl className="ds-props">
              <dt>Tableau Server 上的資料源</dt>
              <dd>{datasource.published.content_url}</dd>
            </dl>
          )}
          <h2>資料源篩選</h2>
          {datasource.filters.length === 0 ? (
            <p className="muted">沒有</p>
          ) : (
            <ul className="ds-filters">
              {datasource.filters.map((f) => (
                <li key={f.id}>
                  {f.text}
                  {f.stage === "extract" && <span className="muted">（萃取篩選）</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="ds-canvas" aria-label="資料表">
          <h2>{datasource.model ? "邏輯資料表與關聯" : "資料表"}</h2>
          <div className="ds-tables">
            {datasource.tables.map((t) => (
              <button
                type="button"
                key={t.id ?? t.caption}
                className={`ds-table${t.id === table ? " active" : ""}`}
                onClick={() => setTable(t.id)}
                disabled={!datasource.model}
                title={datasource.model ? "預覽這張資料表" : undefined}
              >
                <strong>{t.caption}</strong>
                {t.physical.map((p) => (
                  <span key={p.name} className="ds-physical">
                    {p.sql ? "自訂 SQL" : p.source}
                  </span>
                ))}
              </button>
            ))}
          </div>
          {datasource.relationships.length > 0 && (
            <ul className="ds-links">
              {datasource.relationships.map((r, i) => (
                <li key={i}>
                  <span className="ds-link">{r.left}</span> ⟷ <span className="ds-link">{r.right}</span>：{r.on.join("、")}
                </li>
              ))}
            </ul>
          )}
          {datasource.tables.flatMap((t) => t.joins).length > 0 && (
            <ul className="ds-links">
              {datasource.tables.flatMap((t) =>
                t.joins.map((j, i) => (
                  <li key={`${t.caption}-${i}`}>
                    <span className="ds-link">{j.left.join("、")}</span> {j.label}{" "}
                    <span className="ds-link">{j.right.join("、")}</span>：{j.on.join(" 且 ")}
                  </li>
                )),
              )}
            </ul>
          )}
          {datasource.tables
            .flatMap((t) => t.physical)
            .filter((p) => p.sql)
            .map((p) => (
              <details key={p.name} className="ds-sql">
                <summary>自訂 SQL：{p.name}</summary>
                <pre>{p.sql}</pre>
              </details>
            ))}
        </section>
      </div>
      <section className="ds-grid" aria-label="資料與欄位">
        <div className="ds-grid-bar">
          <div className="ds-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === "data"} onClick={() => setTab("data")}>
              資料
            </button>
            <button type="button" role="tab" aria-selected={tab === "fields"} onClick={() => setTab("fields")}>
              欄位（{fields.length}）
            </button>
          </div>
          {tab === "data" && (
            <>
              <label>
                顯示{" "}
                <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
                  {LIMITS.map((n) => (
                    <option key={n} value={n}>
                      {n.toLocaleString()}
                    </option>
                  ))}
                </select>{" "}
                列
              </label>
              <span className="muted ds-count">
                {preview ? countText(preview) : ""}
                {tableCaption && datasource.model ? `　資料表：${tableCaption}` : ""}
              </span>
            </>
          )}
        </div>
        {tab === "data" ? (
          <DataGrid preview={preview} error={error} loading={loading} />
        ) : (
          <FieldGrid fields={fields} />
        )}
      </section>
    </div>
  );
}

function countText(preview: DatasourcePreview): string {
  const shown = preview.rows.length.toLocaleString();
  if (preview.total === null) return `前 ${shown} 列`;
  return `${shown} 列（共 ${preview.total.toLocaleString()} 列）`;
}

function Connection({ connection }: { connection: DatasourceConnection }) {
  const items: [string, string | number | null | undefined][] =
    connection.kind === "file"
      ? [
          ["類型", connection.type],
          ["檔案", connection.files?.join("、")],
        ]
      : connection.kind === "extract"
      ? [
          ["類型", connection.type],
          ["萃取檔", connection.file],
          ["資料表", connection.table],
          ["更新方式", connection.refresh],
          ["只取前幾列", connection.row_limit],
        ]
      : [
          ["類型", connection.type],
          ["伺服器", connection.server && connection.port ? `${connection.server}:${connection.port}` : connection.server],
          ["資料庫", connection.database],
          ["結構描述", connection.schema],
          ["HTTP 路徑", connection.http_path],
          ["驗證方式", connection.authentication],
          ["連線代號", connection.connection_id],
        ];
  return (
    <>
      <dl className="ds-props">
        {items
          .filter(([, value]) => value !== null && value !== undefined && value !== "")
          .map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
      </dl>
      {connection.kind === "live" && connection.supported === false && (
        <p className="error">這個連線類型尚不支援查詢</p>
      )}
      {connection.source && (
        <>
          <h3>萃取的來源</h3>
          <Connection connection={connection.source} />
        </>
      )}
    </>
  );
}

function DataGrid({ preview, error, loading }: { preview: DatasourcePreview | null; error: string | null; loading: boolean }) {
  if (error) return <div className="placeholder error">無法預覽：{error}</div>;
  if (!preview) return <div className="placeholder">{loading ? "載入中…" : ""}</div>;
  return (
    <div className="ds-table-wrap">
      <table className="ds-data">
        <thead>
          <tr>
            {preview.columns.map((c) => (
              <th key={c.field} title={c.calculated ? "計算欄位" : undefined}>
                <span className="ds-type">
                  {c.calculated ? "=" : ""}
                  {DATATYPES[c.datatype] ?? c.datatype}
                </span>
                {c.caption}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row, i) => (
            <tr key={i}>
              {row.map((value, j) => (
                <td key={j} className={cellClass(value, preview.columns[j]?.datatype)}>
                  {cellText(value)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function cellClass(value: Scalar, datatype: string | undefined): string | undefined {
  if (value === null) return "null";
  return datatype === "integer" || datatype === "real" ? "num" : undefined;
}

function cellText(value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "boolean") return value ? "True" : "False";
  return String(value);
}

function FieldGrid({ fields }: { fields: DatasourceField[] }) {
  return (
    <div className="ds-table-wrap">
      <table className="ds-data ds-fields">
        <thead>
          <tr>
            <th>欄位</th>
            <th>類型</th>
            <th>資料類型</th>
            <th>角色</th>
            <th>資料表</th>
            <th>實體欄位或公式</th>
            <th>預設彙總</th>
            <th>格式</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => (
            <tr key={f.id} className={f.hidden ? "hidden-field" : undefined}>
              <td>
                {f.caption}
                {f.hidden && <span className="muted">（隱藏）</span>}
                {f.aliases > 0 && <span className="muted">（{f.aliases} 個別名）</span>}
              </td>
              <td>{KINDS[f.kind]}</td>
              <td>{DATATYPES[f.datatype] ? `${DATATYPES[f.datatype]}（${f.datatype}）` : f.datatype}</td>
              <td>{ROLES[f.role] ?? f.role}</td>
              <td>{f.table ?? ""}</td>
              <td className="ds-formula">{f.formula ?? f.column ?? ""}</td>
              <td>{f.default_agg ?? ""}</td>
              <td>{f.format ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
