// 伺服器（D0）：桌面版的登入、發布、從伺服器開啟；伺服器檢視器的版本（清單、比較、回復、下載）
// 與桌面版的登入管理。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, packageUrl } from "./api";
import { workbookChanged } from "./desktop";
import type {
  CheckProblem,
  DatasourcePublishResult,
  DesktopDatasource,
  DesktopDocument,
  DesktopToken,
  PublishFailure,
  PublishResult,
  ReportChange,
  ReportDetail,
  ServerDatasource,
  ServerProject,
  ServerReport,
  ServerStatus,
} from "./types";

type Go = (to: string) => void;

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("zh-TW", { hour12: false });
}

function formatSize(bytes: number): string {
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <h2>{title}</h2>
          <button type="button" className="link close" aria-label="關閉" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

function Problems({ problems }: { problems: CheckProblem[] }) {
  if (problems.length === 0) return null;
  return (
    <ul className="problems">
      {problems.map((p, i) => (
        <li key={i} className={p.level}>
          {p.level === "error" ? "錯誤：" : "提醒："}
          {p.message}
        </li>
      ))}
    </ul>
  );
}

function Changes({ title, changes }: { title: string; changes: ReportChange[] | undefined }) {
  if (!changes) return null;
  return (
    <section className="changes">
      <h3>{title}</h3>
      {changes.length === 0 ? (
        <p className="hint">沒有不同</p>
      ) : (
        <ul>
          {changes.map((c) => (
            <li key={`${c.kind}:${c.id}`} className={c.change}>
              {c.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- 桌面版：登入伺服器

/** 桌面版的伺服器：登入（開啟系統瀏覽器核准）、登入後從伺服器開啟、登出。 */
export function ServerPanel({
  onOpenFromServer,
  onChange,
}: {
  onOpenFromServer?: () => void;
  onChange?: (status: ServerStatus) => void;
}) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // onChange 常是每次重新建立的函式：放在 ref 中，避免 load 跟著改變而一直重新查詢
  const changed = useRef(onChange);
  changed.current = onChange;
  const load = useCallback(() => {
    api
      .serverStatus()
      .then((next) => {
        setStatus(next);
        if (next.url) setUrl((current) => current || next.url || "");
        changed.current?.(next);
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => load(), [load]);
  // 等使用者在瀏覽器中核准：每秒確認一次
  useEffect(() => {
    if (!status?.pending) return;
    const timer = window.setInterval(load, 1000);
    return () => window.clearInterval(timer);
  }, [status?.pending, load]);

  const signIn = (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    api
      .serverSignIn(url)
      .then(load)
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false));
  };
  const signOut = () => {
    api
      .serverSignOut()
      .then(load)
      .catch((err: Error) => setError(err.message));
  };

  if (!status) return <section className="server-panel">{error ?? "載入中…"}</section>;
  if (status.signed_in) {
    return (
      <section className="server-panel" aria-label="伺服器">
        <span>
          已登入 <b>{status.url}</b>（{status.name || status.username}）
        </span>
        {onOpenFromServer && (
          <button type="button" onClick={onOpenFromServer}>
            從伺服器開啟…
          </button>
        )}
        <button type="button" className="link" onClick={signOut}>
          登出伺服器
        </button>
        {error && <p className="error">{error}</p>}
      </section>
    );
  }
  return (
    <section className="server-panel" aria-label="伺服器">
      <form onSubmit={signIn}>
        <label>
          伺服器
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://bi.example.com"
            aria-label="伺服器網址"
          />
        </label>
        <button type="submit" disabled={busy || !url.trim()}>
          登入伺服器
        </button>
      </form>
      {status.pending && <p className="hint">已開啟瀏覽器：請在伺服器的頁面按「允許」，完成後會自動回到這裡。</p>}
      {(error || status.error) && <p className="error">{error || status.error}</p>}
    </section>
  );
}

// ---------------------------------------------------------------- 桌面版：從伺服器開啟

export function OpenFromServerDialog({ onClose, go }: { onClose: () => void; go: Go }) {
  const [reports, setReports] = useState<ServerReport[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  useEffect(() => {
    api
      .serverReports()
      .then(setReports)
      .catch((e: Error) => setError(e.message));
  }, []);
  const open = (report: ServerReport) => {
    setOpening(report.id);
    setError(null);
    api
      .serverOpen(report.id)
      .then((result) => {
        onClose();
        go(`/w/${encodeURIComponent(result.document.id)}`);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setOpening(null));
  };
  return (
    <Modal title="從伺服器開啟" onClose={onClose} wide>
      {error && <p className="error">{error}</p>}
      {!reports && !error && <p>載入中…</p>}
      {reports?.length === 0 && <p>伺服器上還沒有你看得到的報表。</p>}
      {reports && reports.length > 0 && (
        <table className="list">
          <thead>
            <tr>
              <th>Project</th>
              <th>報表</th>
              <th>版本</th>
              <th>發布者</th>
              <th>發布時間</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {reports.map((r) => (
              <tr key={r.id}>
                <td>{r.project_name}</td>
                <td>{r.name}</td>
                <td>第 {r.version} 版</td>
                <td>{r.publisher}</td>
                <td>{formatTime(r.published_at)}</td>
                <td>
                  <button
                    type="button"
                    disabled={!r.can_download || opening !== null}
                    title={r.can_download ? undefined : "沒有下載的權限（Download Workbook）"}
                    onClick={() => open(r)}
                  >
                    {opening === r.id ? "開啟中…" : "開啟"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- 桌面版：發布

type Phase = "form" | "busy" | "done" | "exists" | "conflict" | "checks" | "error";

export function PublishDialog({ workbook, onClose }: { workbook: string; onClose: () => void }) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [doc, setDoc] = useState<DesktopDocument | null>(null);
  const [projects, setProjects] = useState<ServerProject[] | null>(null);
  const [mode, setMode] = useState<"overwrite" | "new">("new");
  const [project, setProject] = useState("");
  const [name, setName] = useState("");
  const [comment, setComment] = useState("");
  const [phase, setPhase] = useState<Phase>("form");
  const [result, setResult] = useState<PublishResult | null>(null);
  const [failure, setFailure] = useState<PublishFailure | null>(null);

  const load = useCallback(() => {
    Promise.all([api.serverStatus(), api.desktopDocuments()])
      .then(([next, docs]) => {
        setStatus(next);
        const current = docs.find((d) => d.id === workbook) ?? null;
        setDoc(current);
        const server = current?.server && current.server.url === next.url ? current.server : null;
        setMode(server ? "overwrite" : "new");
        setName((value) => value || server?.name || current?.name || "");
        if (!next.signed_in) return;
        api
          .serverProjects()
          .then((found) => {
            setProjects(found);
            const allowed = found.filter((p) => p.can_publish);
            setProject(
              server && allowed.some((p) => p.id === server.project) ? server.project : (allowed[0]?.id ?? ""),
            );
          })
          .catch((e: Error) => {
            setFailure({ error: "project", message: e.message });
            setPhase("error");
          });
      })
      .catch((e: Error) => {
        setFailure({ error: "project", message: e.message });
        setPhase("error");
      });
  }, [workbook]);
  useEffect(() => load(), [load]);

  const server = doc?.server && status && doc.server.url === status.url ? doc.server : null;
  const submit = (extra: { overwrite?: boolean; force?: boolean } = {}) => {
    setPhase("busy");
    const body =
      mode === "overwrite" && server
        ? { workbook, report: server.report, comment, force: extra.force ?? false }
        : { workbook, name, project, comment, overwrite: extra.overwrite ?? false, force: extra.force ?? false };
    api
      .serverPublish(body)
      .then((r) => {
        setResult(r);
        setPhase("done");
      })
      .catch((e: Error) => {
        const detail = e instanceof ApiError && e.detail && typeof e.detail === "object" ? (e.detail as PublishFailure) : null;
        setFailure(detail ?? { error: "package", message: e.message });
        const known: Phase[] = ["exists", "conflict", "checks"];
        setPhase(detail && known.includes(detail.error as Phase) ? (detail.error as Phase) : "error");
      });
  };

  let body: React.ReactNode;
  if (!status) {
    body = <p>載入中…</p>;
  } else if (!status.signed_in) {
    body = (
      <>
        <p>發布前先登入伺服器：</p>
        <ServerPanel onChange={(next) => next.signed_in && load()} />
      </>
    );
  } else if (phase === "busy") {
    body = <p>檢查並發布中…（每張工作表都會先在這台電腦、再在伺服器上各查詢一次）</p>;
  } else if (phase === "done" && result) {
    const warnings = result.problems.filter((p) => p.level === "warning");
    body = (
      <>
        <p className="success">
          已發布「{result.report.name}」第 {result.version.version} 版到「{result.report.project_name}」。
        </p>
        <Problems problems={warnings} />
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void api.serverBrowse(result.report.url)}>
            在瀏覽器開啟
          </button>
          <button type="button" onClick={onClose}>
            關閉
          </button>
        </div>
      </>
    );
  } else if (phase === "exists" && failure) {
    body = (
      <>
        <p>{failure.message}</p>
        <p>要覆蓋它嗎？發布後是它的新版本，舊的版本仍保留在伺服器上，可以回復。</p>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => submit({ overwrite: true })}>
            覆蓋
          </button>
          <button type="button" onClick={() => setPhase("form")}>
            返回
          </button>
        </div>
      </>
    );
  } else if (phase === "conflict" && failure) {
    body = (
      <>
        <p className="error">{failure.message}</p>
        <Changes title="伺服器上別人改了" changes={failure.server_changes} />
        <Changes title="你改了" changes={failure.local_changes} />
        <Changes title="覆蓋後與伺服器上最新版本的不同" changes={failure.overwrite_changes} />
        <p className="hint">
          還不能合併兩邊的修改（編輯在下一階段）：可以覆蓋（伺服器上多一個新版本，別人發布的版本仍保留、可以回復），
          或取消後從伺服器開啟最新的版本。
        </p>
        <div className="dialog-actions">
          <button type="button" className="danger" onClick={() => submit({ force: true })}>
            仍要覆蓋
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </>
    );
  } else if ((phase === "checks" || phase === "error") && failure) {
    body = (
      <>
        <p className="error">{failure.message}</p>
        {failure.problems && <Problems problems={failure.problems} />}
        <div className="dialog-actions">
          <button type="button" onClick={() => setPhase("form")}>
            返回
          </button>
        </div>
      </>
    );
  } else {
    const allowed = (projects ?? []).filter((p) => p.can_publish);
    const ready = mode === "overwrite" ? Boolean(server) : Boolean(project && name.trim());
    body = (
      <form
        className="publish-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) submit();
        }}
      >
        <p className="hint">伺服器：{status.url}（{status.name || status.username}）</p>
        {server && (
          <fieldset>
            <label>
              <input type="radio" checked={mode === "overwrite"} onChange={() => setMode("overwrite")} />
              覆蓋「{server.name}」（{server.project_name}；這份是從第 {server.version} 版開始的）
            </label>
            <label>
              <input type="radio" checked={mode === "new"} onChange={() => setMode("new")} />
              發布成新的報表
            </label>
          </fieldset>
        )}
        {mode === "new" && (
          <>
            <label>
              Project
              <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Project">
                {allowed.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.path}
                  </option>
                ))}
              </select>
            </label>
            {projects && allowed.length === 0 && <p className="error">你沒有可以發布的 Project。</p>}
            <label>
              報表名稱
              <input value={name} onChange={(e) => setName(e.target.value)} aria-label="報表名稱" />
            </label>
          </>
        )}
        <label>
          版本說明（選填）
          <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={3} aria-label="版本說明" />
        </label>
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={!ready}>
            發布
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    );
  }
  return (
    <Modal title="發布到伺服器" onClose={onClose}>
      {body}
    </Modal>
  );
}

// ---------------------------------------------------------------- 桌面版：發布資料源

const CONNECTIONS: Record<DesktopDatasource["connection"], string> = { extract: "萃取", live: "即時連線", file: "檔案" };

/** 發布報表中的一個資料源（與 Tableau Desktop 的「發布資料源」相同）；可以讓報表改用發布的資料源。 */
export function PublishDatasourceDialog({
  workbook,
  datasource,
  onClose,
}: {
  workbook: string;
  datasource?: string;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [items, setItems] = useState<DesktopDatasource[] | null>(null);
  const [projects, setProjects] = useState<ServerProject[] | null>(null);
  const [chosen, setChosen] = useState(datasource ?? "");
  const [mode, setMode] = useState<"overwrite" | "new">("new");
  const [project, setProject] = useState("");
  const [name, setName] = useState("");
  const [comment, setComment] = useState("");
  const [link, setLink] = useState(true);
  const [phase, setPhase] = useState<"form" | "busy" | "done" | "exists" | "error">("form");
  const [result, setResult] = useState<DatasourcePublishResult | null>(null);
  const [failure, setFailure] = useState<{ message: string; datasource?: ServerDatasource | null } | null>(null);

  const fail = (e: Error) => {
    setFailure({ message: e.message });
    setPhase("error");
  };
  const load = useCallback(() => {
    api
      .serverStatus()
      .then((next) => {
        setStatus(next);
        if (!next.signed_in) return;
        Promise.all([api.serverDatasources(workbook), api.serverProjects()])
          .then(([found, list]) => {
            setItems(found.datasources);
            setProjects(list);
            setProject((current) => current || (list.find((p) => p.can_publish)?.id ?? ""));
            setChosen((current) =>
              found.datasources.some((d) => d.id === current) ? current : (found.datasources[0]?.id ?? ""),
            );
          })
          .catch(fail);
      })
      .catch(fail);
  }, [workbook]);
  useEffect(() => load(), [load]);

  const item = items?.find((d) => d.id === chosen) ?? null;
  const writable = Boolean(item?.server?.can?.Write);
  // 換資料源時：已經連到伺服器上的資料源（可以覆蓋）時預設覆蓋它；名稱預設為資料源的名稱
  useEffect(() => {
    if (!item) return;
    setMode(item.server && item.server.can?.Write ? "overwrite" : "new");
    setName(item.server?.name ?? item.caption);
  }, [item?.id, items]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = (overwrite = false) => {
    if (!item) return;
    setPhase("busy");
    const body =
      mode === "overwrite" && item.server
        ? { workbook, datasource: item.id, target: item.server.id, comment, link }
        : { workbook, datasource: item.id, name: name.trim(), project, comment, link, overwrite };
    api
      .serverPublishDatasource(body)
      .then((r) => {
        setResult(r);
        setPhase("done");
        if (r.linked) workbookChanged(workbook);
      })
      .catch((e: Error) => {
        const detail =
          e instanceof ApiError && e.detail && typeof e.detail === "object"
            ? (e.detail as { error?: string; message?: string; datasource?: ServerDatasource | null })
            : null;
        setFailure({ message: detail?.message ?? e.message, datasource: detail?.datasource });
        setPhase(detail?.error === "exists" ? "exists" : "error");
      });
  };

  let body: React.ReactNode;
  if (!status) {
    body = <p>載入中…</p>;
  } else if (!status.signed_in) {
    body = (
      <>
        <p>發布前先登入伺服器：</p>
        <ServerPanel onChange={(next) => next.signed_in && load()} />
      </>
    );
  } else if (phase === "busy") {
    body = <p>發布中…（資料源的定義與萃取一起上傳）</p>;
  } else if (phase === "done" && result) {
    body = (
      <>
        <p className="success">
          已發布資料源「{result.datasource.name}」第 {result.datasource.version} 版到「{result.datasource.project_path}」。
        </p>
        {result.linked && (
          <p className="hint">
            報表已改用發布的資料源：之後發布這份報表，伺服器上的報表直接使用伺服器上的資料源（萃取更新一次，每份報表都看到新的資料）。記得儲存報表。
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => void api.serverBrowse("/datasources")}>
            在瀏覽器開啟
          </button>
          <button type="button" onClick={onClose}>
            關閉
          </button>
        </div>
      </>
    );
  } else if (phase === "exists" && failure) {
    body = (
      <>
        <p>{failure.message}。</p>
        <p>要覆蓋它嗎？發布後是它的新版本，用到它的報表會看到新的定義與資料；或返回改用其他名稱。</p>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => submit(true)}>
            覆蓋
          </button>
          <button type="button" onClick={() => setPhase("form")}>
            返回
          </button>
        </div>
      </>
    );
  } else if (phase === "error" && failure) {
    body = (
      <>
        <p className="error">{failure.message}</p>
        <div className="dialog-actions">
          <button type="button" onClick={() => setPhase("form")}>
            返回
          </button>
        </div>
      </>
    );
  } else if (!items) {
    body = <p>載入中…</p>;
  } else {
    const allowed = (projects ?? []).filter((p) => p.can_publish);
    const ready = Boolean(item) && (mode === "overwrite" ? Boolean(item?.server) : Boolean(project && name.trim()));
    body = (
      <form
        className="publish-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) submit();
        }}
      >
        <p className="hint">伺服器：{status.url}（{status.name || status.username}）</p>
        <label>
          資料源
          <select value={chosen} onChange={(e) => setChosen(e.target.value)} aria-label="資料源">
            {items.map((d) => (
              <option key={d.id} value={d.id}>
                {d.caption}（{CONNECTIONS[d.connection]}）
              </option>
            ))}
          </select>
        </label>
        {item?.server && (
          <fieldset>
            <label>
              <input
                type="radio"
                checked={mode === "overwrite"}
                disabled={!writable}
                onChange={() => setMode("overwrite")}
              />
              覆蓋「{item.server.name}」（{item.server.project_path}；目前第 {item.server.version} 版）
              {!writable && <span className="hint">（沒有覆蓋的權限）</span>}
            </label>
            <label>
              <input type="radio" checked={mode === "new"} onChange={() => setMode("new")} />
              發布成新的資料源
            </label>
          </fieldset>
        )}
        {mode === "new" && (
          <>
            <label>
              Project
              <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Project">
                {allowed.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.path}
                  </option>
                ))}
              </select>
            </label>
            {projects && allowed.length === 0 && <p className="error">你沒有可以發布的 Project。</p>}
            <label>
              資料源名稱
              <input value={name} onChange={(e) => setName(e.target.value)} aria-label="資料源名稱" />
            </label>
          </>
        )}
        <label>
          版本說明（選填）
          <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} aria-label="版本說明" />
        </label>
        <label className="inline">
          <input type="checkbox" checked={link} onChange={(e) => setLink(e.target.checked)} />
          報表改用發布的資料源
        </label>
        {item?.connection === "live" && (
          <p className="hint">即時連線：伺服器以自己的連線設定連到資料庫（帳號密碼不會上傳）。</p>
        )}
        <div className="dialog-actions">
          <button type="submit" className="primary" disabled={!ready}>
            發布
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    );
  }
  return (
    <Modal title="發布資料源" onClose={onClose}>
      {body}
    </Modal>
  );
}

// ---------------------------------------------------------------- 伺服器：版本

/** 報表庫的報表才有「版本」按鈕（fixtures 的報表沒有）。 */
export function VersionsButton({ workbook }: { workbook: string }) {
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [open, setOpen] = useState(false);
  const load = useCallback(() => {
    api
      .report(workbook)
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [workbook]);
  useEffect(() => load(), [load]);
  if (!detail) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        版本（第 {detail.version} 版）
      </button>
      {open && (
        <VersionsDialog
          detail={detail}
          onClose={() => setOpen(false)}
          onRestored={() => {
            setOpen(false);
            window.location.reload();
          }}
        />
      )}
    </>
  );
}

function VersionsDialog({
  detail,
  onClose,
  onRestored,
}: {
  detail: ReportDetail;
  onClose: () => void;
  onRestored: () => void;
}) {
  const versions = detail.versions;
  const [base, setBase] = useState(versions[1]?.version ?? versions[0].version);
  const [target, setTarget] = useState(versions[0].version);
  const [changes, setChanges] = useState<ReportChange[] | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<CheckProblem[]>([]);
  const compare = () => {
    setError(null);
    api
      .compare(detail.id, base, target)
      .then((r) => setChanges(r.changes))
      .catch((e: Error) => setError(e.message));
  };
  const restore = (version: number) => {
    setError(null);
    api
      .restore(detail.id, version)
      .then(onRestored)
      .catch((e: Error) => {
        setConfirming(null);
        setError(e.message);
        const failure = e instanceof ApiError ? (e.detail as PublishFailure | null) : null;
        setProblems(failure?.problems ?? []);
      });
  };
  return (
    <Modal title={`「${detail.name}」的版本`} onClose={onClose} wide>
      <p className="hint">
        {detail.project_name}・擁有者 {detail.owner}。每一版都保留在伺服器上；回復舊版會發布一個內容相同的新版本。
      </p>
      {error && <p className="error">{error}</p>}
      <Problems problems={problems} />
      <table className="list">
        <thead>
          <tr>
            <th>版本</th>
            <th>發布時間</th>
            <th>發布者</th>
            <th>說明</th>
            <th>大小</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {versions.map((v, i) => (
            <tr key={v.version}>
              <td>
                第 {v.version} 版{i === 0 && "（目前）"}
              </td>
              <td>{formatTime(v.published_at)}</td>
              <td>{v.publisher}</td>
              <td>
                {v.comment}
                {v.restored_from !== null && <span className="hint">（回復自第 {v.restored_from} 版）</span>}
              </td>
              <td>{formatSize(v.size)}</td>
              <td className="actions">
                {detail.can_download && (
                  <a href={packageUrl(detail.id, v.version)} download>
                    下載
                  </a>
                )}
                {detail.can_overwrite && i > 0 && confirming !== v.version && (
                  <button type="button" onClick={() => setConfirming(v.version)}>
                    回復
                  </button>
                )}
                {confirming === v.version && (
                  <>
                    <button type="button" className="danger" onClick={() => restore(v.version)}>
                      確定回復第 {v.version} 版
                    </button>
                    <button type="button" onClick={() => setConfirming(null)}>
                      取消
                    </button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {versions.length > 1 && (
        <section className="compare">
          <label>
            比較第
            <select value={base} onChange={(e) => setBase(Number(e.target.value))} aria-label="比較的基準版本">
              {versions.map((v) => (
                <option key={v.version} value={v.version}>
                  {v.version}
                </option>
              ))}
            </select>
            版與第
            <select value={target} onChange={(e) => setTarget(Number(e.target.value))} aria-label="比較的版本">
              {versions.map((v) => (
                <option key={v.version} value={v.version}>
                  {v.version}
                </option>
              ))}
            </select>
            版
          </label>
          <button type="button" onClick={compare}>
            比較
          </button>
          {changes && changes.length === 0 && <p className="hint">兩版的報表定義相同。</p>}
          {changes && changes.length > 0 && <Changes title="不同的地方" changes={changes} />}
        </section>
      )}
    </Modal>
  );
}

// ---------------------------------------------------------------- 伺服器：桌面版的登入

export function DesktopTokensButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="link" onClick={() => setOpen(true)}>
        桌面版的登入
      </button>
      {open && <DesktopTokensDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function DesktopTokensDialog({ onClose }: { onClose: () => void }) {
  const [tokens, setTokens] = useState<DesktopToken[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api
      .desktopTokens()
      .then(setTokens)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => load(), [load]);
  const revoke = (id: string) => {
    api
      .revokeDesktopToken(id)
      .then(load)
      .catch((e: Error) => setError(e.message));
  };
  return (
    <Modal title="桌面版的登入" onClose={onClose}>
      <p className="hint">以你的身分登入這台伺服器的報表桌面版。不認得的登入請撤銷；撤銷後那台電腦要重新登入。</p>
      {error && <p className="error">{error}</p>}
      {tokens?.length === 0 && <p>沒有桌面版登入。</p>}
      {tokens && tokens.length > 0 && (
        <table className="list">
          <thead>
            <tr>
              <th>電腦</th>
              <th>登入時間</th>
              <th>最後使用</th>
              <th>到期</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id}>
                <td>{t.label || "（未命名）"}</td>
                <td>{formatTime(t.created_at)}</td>
                <td>{t.last_used_at ? formatTime(t.last_used_at) : "—"}</td>
                <td>{formatTime(t.expires_at)}</td>
                <td>
                  <button type="button" onClick={() => revoke(t.id)}>
                    撤銷
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}
