import { useCallback, useEffect, useState } from "react";
import { ApiError, request, UNAUTHORIZED_EVENT } from "./api";
import { ConfirmDialog } from "./dialogs";
import { Modal } from "./Server";

// 伺服器上的 Published Data Source（tmig_service.datasource_api）：清單、發布、詳細資料與下載

export interface DatasourceItem {
  id: string;
  name: string;
  content_url: string;
  project: string;
  project_path: string;
  owner: string;
  connection: string;
  extract: boolean;
  refreshed_at: string | null;
  version: number;
  published_at: string;
  rules: {
    grantee: { kind: "user" | "group"; name: string; domain?: string | null };
    capabilities: Record<string, "Allow" | "Deny">;
  }[];
  used_by: { id: string; name: string }[];
  can: Record<string, boolean>;
}

export interface DatasourceDetail extends DatasourceItem {
  versions: { version: number; published_at: string; publisher: string; comment: string; size: number }[];
  fields: { caption: string; role: string; datatype: string; formula: string | null; hidden: boolean }[];
  capabilities: Record<string, string>;
}

const json = (method: string, body: unknown) => ({ method, body: JSON.stringify(body) });
const path = (id: string) => `/api/datasources/${encodeURIComponent(id)}`;

export const datasourceApi = {
  list: () => request<DatasourceItem[]>("/api/datasources"),
  detail: (id: string) => request<DatasourceDetail>(path(id)),
  update: (id: string, body: Record<string, unknown>) => request<DatasourceItem>(path(id), json("PATCH", body)),
  remove: (id: string) => request<void>(path(id), { method: "DELETE" }),
  downloadUrl: (id: string, version: number) => `${path(id)}/versions/${version}/download`,
  async publish(file: File, name: string, project: string, comment: string): Promise<DatasourceItem> {
    const query = new URLSearchParams({ name, project, comment });
    const response = await fetch(`/api/datasources/publish?${query}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    if (!response.ok) {
      let text = response.statusText;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") text = body.detail;
      } catch {
        // 回應不是 JSON 時沿用狀態文字
      }
      throw new ApiError(response.status, text, `${response.status} ${text}`);
    }
    return (await response.json()) as DatasourceItem;
  },
};

export function PublishDatasourceDialog({
  projects,
  onClose,
  onDone,
}: {
  projects: { id: string; path: string }[];
  onClose: () => void;
  onDone: (item: DatasourceItem) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [project, setProject] = useState(projects[0]?.id ?? "default");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title="發布資料源" onClose={onClose}>
      <div className="publish-form">
        <label>
          資料源檔案（.tdsx 或 .tds）
          <input
            type="file"
            aria-label="資料源檔案"
            accept=".tdsx,.tds"
            onChange={(e) => {
              const chosen = e.target.files?.[0] ?? null;
              setFile(chosen);
              if (chosen && !name) setName(chosen.name.replace(/\.tdsx?$/i, ""));
            }}
          />
        </label>
        <label>
          名稱
          <input aria-label="資料源名稱" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        {projects.length > 0 && (
          <label>
            Project
            <select aria-label="Project" value={project} onChange={(e) => setProject(e.target.value)}>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.path}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          版本說明（選填）
          <textarea aria-label="版本說明" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
        </label>
        <p className="hint">
          與 Tableau
          相同，同一個網址名稱的資料源再發布為新版本；連到它的報表改用伺服器上的萃取（萃取更新一次，每份報表都看到新的資料）。
        </p>
        {error && <p className="error">{error}</p>}
      </div>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!file || !name.trim() || busy}
          onClick={() => {
            if (!file) return;
            setBusy(true);
            datasourceApi
              .publish(file, name.trim(), project, comment)
              .then(onDone)
              .catch((e: Error) => setError(e.message))
              .finally(() => setBusy(false));
          }}
        >
          {busy ? "發布中…" : "發布"}
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

function DetailDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const [detail, setDetail] = useState<DatasourceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    datasourceApi
      .detail(id)
      .then(setDetail)
      .catch((e: Error) => setError(e.message));
  }, [id]);
  return (
    <Modal title={detail ? `資料源「${detail.name}」` : "資料源"} onClose={onClose} wide>
      {error && <p className="error">{error}</p>}
      {!detail && !error && <p>載入中…</p>}
      {detail && (
        <>
          <p className="hint">
            {detail.project_path}；擁有者 {detail.owner}；{detail.connection}
            {detail.refreshed_at ? `；萃取更新於 ${detail.refreshed_at.replace("T", " ").slice(0, 16)}` : ""}
          </p>
          <h3>欄位</h3>
          {detail.fields.length === 0 ? (
            <p className="hint">沒有「連線」的權限時不顯示欄位。</p>
          ) : (
            <table className="list">
              <thead>
                <tr>
                  <th>名稱</th>
                  <th>角色</th>
                  <th>型別</th>
                  <th>公式</th>
                </tr>
              </thead>
              <tbody>
                {detail.fields
                  .filter((f) => !f.hidden)
                  .map((f) => (
                    <tr key={f.caption}>
                      <td>{f.caption}</td>
                      <td>{f.role === "measure" ? "量值" : "維度"}</td>
                      <td>{f.datatype}</td>
                      <td className="formula">{f.formula ?? ""}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
          <h3>版本</h3>
          <table className="list">
            <thead>
              <tr>
                <th>版本</th>
                <th>發布</th>
                <th>發布的人</th>
                <th>說明</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {detail.versions.map((v) => (
                <tr key={v.version}>
                  <td>第 {v.version} 版</td>
                  <td>{v.published_at.replace("T", " ").slice(0, 16)}</td>
                  <td>{v.publisher}</td>
                  <td>{v.comment}</td>
                  <td>
                    {detail.can.ExportXml && (
                      <a href={datasourceApi.downloadUrl(detail.id, v.version)} download>
                        下載
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Modal>
  );
}

/** 資料源頁面：看得到的 Published Data Source、用到它的報表；可以發布與下載。 */
export function DatasourcesPage({ go }: { go: (to: string) => void }) {
  const [items, setItems] = useState<DatasourceItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [showing, setShowing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<DatasourceItem | null>(null);
  const [projects, setProjects] = useState<{ id: string; path: string; can_publish: boolean }[]>([]);
  const reload = useCallback(() => {
    datasourceApi
      .list()
      .then(setItems)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(reload, [reload]);
  useEffect(() => {
    request<{ id: string; path: string; can_publish: boolean }[]>("/api/projects")
      .then(setProjects)
      .catch(() => setProjects([]));
  }, []);
  const publishable = projects.filter((p) => p.can_publish);
  return (
    <main className="home admin">
      <div className="home-header">
        <h1>資料源</h1>
        <a
          href="/"
          onClick={(e) => {
            e.preventDefault();
            go("/");
          }}
        >
          回到報表
        </a>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}
      <section className="admin-section standalone">
        {publishable.length > 0 && (
          <div className="admin-actions">
            <button type="button" className="primary" onClick={() => setPublishing(true)}>
              發布資料源…
            </button>
          </div>
        )}
        {items && items.length === 0 && <p className="hint">還沒有發布到伺服器的資料源。</p>}
        {items && items.length > 0 && (
          <table className="admin-table">
            <thead>
              <tr>
                <th>名稱</th>
                <th>Project</th>
                <th>擁有者</th>
                <th>連線</th>
                <th>萃取更新</th>
                <th>用到的報表</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} data-datasource={item.id}>
                  <td>
                    <button type="button" className="link" onClick={() => setShowing(item.id)}>
                      {item.name}
                    </button>{" "}
                    <span className="hint">第 {item.version} 版</span>
                  </td>
                  <td>{item.project_path}</td>
                  <td>{item.owner}</td>
                  <td>{item.connection}</td>
                  <td>{item.refreshed_at ? item.refreshed_at.replace("T", " ").slice(0, 16) : "—"}</td>
                  <td>
                    {item.used_by.map((r, i) => (
                      <span key={r.id}>
                        {i > 0 && "、"}
                        <a
                          href={`/w/${encodeURIComponent(r.id)}`}
                          onClick={(e) => {
                            e.preventDefault();
                            go(`/w/${encodeURIComponent(r.id)}`);
                          }}
                        >
                          {r.name}
                        </a>
                      </span>
                    ))}
                    {item.used_by.length === 0 && <span className="hint">—</span>}
                  </td>
                  <td className="actions">
                    {item.can.ExportXml && (
                      <a href={datasourceApi.downloadUrl(item.id, item.version)} download>
                        下載
                      </a>
                    )}
                    {item.can.Delete && (
                      <button type="button" className="link danger-link" onClick={() => setRemoving(item)}>
                        刪除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {publishing && (
        <PublishDatasourceDialog
          projects={publishable}
          onClose={() => setPublishing(false)}
          onDone={(item) => {
            setPublishing(false);
            setNotice(`已發布「${item.name}」第 ${item.version} 版`);
            reload();
          }}
        />
      )}
      {showing && <DetailDialog id={showing} onClose={() => setShowing(null)} />}
      {removing && (
        <ConfirmDialog
          title="刪除資料源"
          message={`刪除資料源「${removing.name}」與它的每一版？用到它的報表改用報表裡的資料。`}
          onClose={() => setRemoving(null)}
          onConfirm={() =>
            datasourceApi
              .remove(removing.id)
              .then(() => {
                setNotice("已刪除資料源");
                reload();
                return true;
              })
              .catch((e: Error) => {
                setError(e.message);
                return false;
              })
          }
        />
      )}
    </main>
  );
}
