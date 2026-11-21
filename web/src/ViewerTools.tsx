import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "./api";
import { ConfirmDialog } from "./dialogs";
import { saveFile } from "./exporter";
import { Modal } from "./Server";
import { stateFor, type ViewState } from "./state";
import type { IR, Zone } from "./types";
import {
  type CommentItem,
  type CustomViewItem,
  downloadSheet,
  type DownloadKind,
  type UnderlyingData,
  type ViewerAccess,
  viewerApi,
} from "./viewerApi";

// 檢視器工具列：下載資料與交叉資料表、自訂檢視、留言、我的最愛（與 Tableau Server 的檢視器相同）

function sheetsIn(zones: Zone[]): string[] {
  const found: string[] = [];
  const walk = (items: Zone[]) => {
    for (const zone of items) {
      if (zone.kind === "worksheet" && zone.worksheet && !found.includes(zone.worksheet)) found.push(zone.worksheet);
      walk(zone.children ?? []);
    }
  };
  walk(zones);
  return found;
}

/** 這個 view 中可以下載的工作表：dashboard 中的每張工作表，或工作表本身。 */
export function viewSheets(ir: IR, dashboard: string | null, worksheet: string | null): { id: string; name: string }[] {
  const ids = dashboard
    ? sheetsIn(ir.dashboards.find((d) => d.id === dashboard)?.zones ?? [])
    : worksheet
      ? [worksheet]
      : [];
  return ids
    .map((id) => ir.worksheets.find((w) => w.id === id))
    .filter((w): w is NonNullable<typeof w> => !!w)
    .map((w) => ({ id: w.id, name: w.name }));
}

/** 下載交叉資料表（Excel、CSV）：選擇工作表與格式。 */
export function CrosstabDialog({
  workbook,
  ir,
  sheets,
  view,
  desktop,
  onClose,
  onDone,
}: {
  workbook: string;
  ir: IR;
  sheets: { id: string; name: string }[];
  view: ViewState;
  desktop: boolean;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [sheet, setSheet] = useState(sheets[0]?.id ?? "");
  const [format, setFormat] = useState<"xlsx" | "csv">("xlsx");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const download = () => {
    setBusy(true);
    setError(null);
    downloadSheet(workbook, sheet, stateFor(ir, sheet, view), "crosstab", format)
      .then(({ blob, name }) => saveFile(name, blob, desktop))
      .then((path) => {
        if (path) onDone(`已下載：${path}`);
        else onClose();
      })
      .catch((e: unknown) => setError(`無法下載：${errorText(e)}`))
      .finally(() => setBusy(false));
  };
  return (
    <Modal title="下載交叉資料表" onClose={onClose}>
      <div className="publish-form">
        <label>
          工作表
          <select
            aria-label="工作表"
            value={sheet}
            onChange={(e) => setSheet(e.target.value)}
            size={Math.min(8, Math.max(2, sheets.length))}
          >
            {sheets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>格式</legend>
          <label className="inline">
            <input type="radio" checked={format === "xlsx"} onChange={() => setFormat("xlsx")} /> Excel
          </label>
          <label className="inline">
            <input type="radio" checked={format === "csv"} onChange={() => setFormat("csv")} /> CSV
          </label>
        </fieldset>
        <p className="hint">與畫面上的篩選相同；數字與日期的格式、小計與總計照工作表的設定。</p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </div>
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!sheet || busy} onClick={download}>
          {busy ? "下載中…" : "下載"}
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

/** 檢視資料：摘要資料（每個標記一列）與完整資料（基礎資料列），可以下載 CSV。 */
export function DataDialog({
  workbook,
  ir,
  sheets,
  view,
  access,
  desktop,
  onClose,
}: {
  workbook: string;
  ir: IR;
  sheets: { id: string; name: string }[];
  view: ViewState;
  access: ViewerAccess;
  desktop: boolean;
  onClose: () => void;
}) {
  const [sheet, setSheet] = useState(sheets[0]?.id ?? "");
  const [tab, setTab] = useState<"summary" | "underlying">("summary");
  const [summary, setSummary] = useState<{ headers: string[]; rows: string[][] } | null>(null);
  const [full, setFull] = useState<UnderlyingData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!sheet) return;
    setError(null);
    const state = stateFor(ir, sheet, view);
    if (tab === "summary") {
      setSummary(null);
      import("./api")
        .then(({ api }) => api.render(workbook, sheet, state))
        .then((result) =>
          setSummary(
            result.table ? { headers: result.table.headers, rows: result.table.rows } : { headers: [], rows: [] },
          ),
        )
        .catch((e: Error) => setError(e.message));
    } else {
      setFull(null);
      viewerApi
        .underlying(workbook, sheet, state)
        .then(setFull)
        .catch((e: Error) => setError(e.message));
    }
  }, [workbook, ir, sheet, view, tab]);
  const download = (kind: DownloadKind) => {
    setError(null);
    setNotice(null);
    downloadSheet(workbook, sheet, stateFor(ir, sheet, view), kind, "csv")
      .then(({ blob, name }) => saveFile(name, blob, desktop))
      .then((path) => path && setNotice(`已下載：${path}`))
      .catch((e: unknown) => setError(`無法下載：${errorText(e)}`));
  };
  const headers = tab === "summary" ? (summary?.headers ?? []) : (full?.columns ?? []);
  const rows = tab === "summary" ? (summary?.rows ?? []) : (full?.rows ?? []);
  const loaded = tab === "summary" ? summary !== null : full !== null;
  return (
    <Modal title="檢視資料" onClose={onClose} wide>
      <div className="row data-dialog-tools">
        {sheets.length > 1 && (
          <select aria-label="工作表" value={sheet} onChange={(e) => setSheet(e.target.value)}>
            {sheets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        )}
        <nav className="tabs" aria-label="資料">
          <button type="button" className={tab === "summary" ? "active" : ""} onClick={() => setTab("summary")}>
            摘要
          </button>
          {access.underlying_data && (
            <button type="button" className={tab === "underlying" ? "active" : ""} onClick={() => setTab("underlying")}>
              完整資料
            </button>
          )}
        </nav>
        <button type="button" disabled={!loaded} onClick={() => download(tab === "summary" ? "summary" : "underlying")}>
          下載 CSV
        </button>
        {loaded && (
          <span className="hint">
            {rows.length} 列
            {tab === "underlying" && full?.truncated ? `（只顯示前 ${full.limit} 列；下載包含全部）` : ""}
          </span>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && <p className="success">{notice}</p>}
      {!loaded && !error && <p>載入中…</p>}
      {loaded && (
        <div className="data-preview">
          <table className="list">
            <thead>
              <tr>
                {headers.map((h, i) => (
                  <th key={i}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>
                      {cell === null || cell === undefined ? <span className="muted">Null</span> : String(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------ 自訂檢視

export function CustomViewsButton({
  workbook,
  view,
  state,
  onApply,
}: {
  workbook: string;
  view: string;
  state: ViewState;
  onApply: (state: ViewState | null, name: string | null) => void;
}) {
  const [data, setData] = useState<{ views: CustomViewItem[]; default: string | null; can_share: boolean } | null>(
    null,
  );
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<CustomViewItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    viewerApi
      .customViews(workbook, view)
      .then(setData)
      .catch(() => setData(null));
  }, [workbook, view]);
  useEffect(reload, [reload]);
  // 開啟 view 時套用我的預設檢視（與 Tableau 相同）
  const applied = useRef(false);
  useEffect(() => {
    if (!data || applied.current) return;
    applied.current = true;
    const chosen = data.views.find((v) => v.id === data.default);
    if (chosen) onApply(chosen.state as unknown as ViewState, chosen.name);
  }, [data, onApply]);
  if (!data) return null;
  const run = async (work: () => Promise<unknown>) => {
    try {
      await work();
      setError(null);
      reload();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  };
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        自訂檢視{data.views.length ? `（${data.views.length}）` : ""}
      </button>
      {open && (
        <Modal title="自訂檢視" onClose={() => setOpen(false)} wide>
          <div className="admin-actions">
            <button type="button" className="primary" onClick={() => setSaving(true)}>
              儲存目前的檢視…
            </button>
            <button
              type="button"
              onClick={() => {
                onApply(null, null);
                setOpen(false);
              }}
            >
              回到原始的檢視
            </button>
          </div>
          {error && <p className="error">{error}</p>}
          {data.views.length === 0 ? (
            <p className="hint">還沒有自訂檢視：調整篩選、參數或選取後按「儲存目前的檢視…」。</p>
          ) : (
            <table className="list">
              <thead>
                <tr>
                  <th>名稱</th>
                  <th>擁有者</th>
                  <th>公開</th>
                  <th>預設</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.views.map((item) => (
                  <tr key={item.id} data-custom-view={item.id}>
                    <td>
                      <button
                        type="button"
                        className="link"
                        onClick={() => {
                          onApply(item.state as unknown as ViewState, item.name);
                          setOpen(false);
                        }}
                      >
                        {item.name}
                      </button>
                    </td>
                    <td>{item.mine ? "我" : item.owner}</td>
                    <td>{item.public ? "公開" : "只有我"}</td>
                    <td>
                      <label className="inline">
                        <input
                          type="radio"
                          name="default-view"
                          aria-label={`${item.name} 設為預設`}
                          checked={data.default === item.id}
                          onChange={() => void run(() => viewerApi.setDefault(workbook, view, item.id))}
                        />
                      </label>
                    </td>
                    <td className="actions">
                      {item.mine && (
                        <>
                          {(item.public || data.can_share) && (
                            <button
                              type="button"
                              className="link"
                              onClick={() => void run(() => viewerApi.updateView(item.id, { public: !item.public }))}
                            >
                              {item.public ? "改為只有我" : "公開"}
                            </button>
                          )}
                          <button type="button" className="link danger-link" onClick={() => setRemoving(item)}>
                            刪除
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {data.default && (
            <p>
              <button
                type="button"
                className="link"
                onClick={() => void run(() => viewerApi.setDefault(workbook, view, null))}
              >
                不使用預設的自訂檢視（開啟時為原始的檢視）
              </button>
            </p>
          )}
        </Modal>
      )}
      {saving && (
        <SaveViewDialog
          canShare={data.can_share}
          onClose={() => setSaving(false)}
          onSave={(name, isPublic, makeDefault) =>
            run(() => viewerApi.saveView(workbook, view, { name, state, public: isPublic, make_default: makeDefault }))
          }
        />
      )}
      {removing && (
        <ConfirmDialog
          title="刪除自訂檢視"
          message={`刪除自訂檢視「${removing.name}」？`}
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => viewerApi.deleteView(removing.id))}
        />
      )}
    </>
  );
}

function SaveViewDialog({
  canShare,
  onClose,
  onSave,
}: {
  canShare: boolean;
  onClose: () => void;
  onSave: (name: string, isPublic: boolean, makeDefault: boolean) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [isPublic, setPublic] = useState(false);
  const [makeDefault, setDefault] = useState(false);
  return (
    <Modal title="儲存自訂檢視" onClose={onClose}>
      <div className="publish-form">
        <label>
          名稱
          <input aria-label="自訂檢視的名稱" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="inline">
          <input type="checkbox" checked={makeDefault} onChange={(e) => setDefault(e.target.checked)} />{" "}
          設為我的預設檢視
        </label>
        {canShare && (
          <label className="inline">
            <input type="checkbox" checked={isPublic} onChange={(e) => setPublic(e.target.checked)} />{" "}
            公開（其他看得到這份報表的人也看得到）
          </label>
        )}
        <p className="hint">同名的自訂檢視會被取代。</p>
      </div>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!name.trim()}
          onClick={() => void onSave(name.trim(), isPublic, makeDefault).then((ok) => ok && onClose())}
        >
          儲存
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------ 留言

export function CommentsButton({ workbook, view }: { workbook: string; view: string }) {
  const [data, setData] = useState<{ can_add: boolean; comments: CommentItem[] } | null>(null);
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    viewerApi
      .comments(workbook, view)
      .then(setData)
      .catch(() => setData(null));
  }, [workbook, view]);
  useEffect(reload, [reload]);
  if (!data) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        留言{data.comments.length ? `（${data.comments.length}）` : ""}
      </button>
      {open && (
        <Modal title="留言" onClose={() => setOpen(false)}>
          {data.comments.length === 0 && <p className="hint">還沒有留言。</p>}
          <ul className="comments">
            {data.comments.map((c) => (
              <li key={c.id}>
                <div className="comment-meta">
                  <strong>{c.user}</strong> <span className="hint">{c.created_at.replace("T", " ").slice(0, 16)}</span>
                  {c.can_delete && (
                    <button
                      type="button"
                      className="link danger-link"
                      onClick={() =>
                        void viewerApi
                          .deleteComment(c.id)
                          .then(reload)
                          .catch((e: Error) => setError(e.message))
                      }
                    >
                      刪除
                    </button>
                  )}
                </div>
                <div className="comment-text">{c.text}</div>
              </li>
            ))}
          </ul>
          {data.can_add && (
            <div className="publish-form">
              <label>
                新增留言
                <textarea aria-label="新增留言" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
              </label>
            </div>
          )}
          {error && <p className="error">{error}</p>}
          <div className="dialog-actions">
            {data.can_add && (
              <button
                type="button"
                className="primary"
                disabled={!text.trim()}
                onClick={() =>
                  void viewerApi
                    .addComment(workbook, view, text)
                    .then(() => {
                      setText("");
                      setError(null);
                      reload();
                    })
                    .catch((e: Error) => setError(e.message))
                }
              >
                送出
              </button>
            )}
            <button type="button" onClick={() => setOpen(false)}>
              關閉
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

// ------------------------------------------------------------ 我的最愛

export function FavoriteButton({ workbook, view }: { workbook: string; view: string }) {
  const [on, setOn] = useState<boolean | null>(null);
  useEffect(() => {
    viewerApi
      .favorites()
      .then((items) => setOn(items.some((f) => f.report === workbook && f.view === view)))
      .catch(() => setOn(null));
  }, [workbook, view]);
  if (on === null) return null;
  return (
    <button
      type="button"
      className={`favorite${on ? " on" : ""}`}
      aria-pressed={on}
      title={on ? "從我的最愛移除" : "加入我的最愛"}
      onClick={() =>
        void viewerApi
          .setFavorite(workbook, view, !on)
          .then((items) => setOn(items.some((f) => f.report === workbook && f.view === view)))
      }
    >
      {on ? "★" : "☆"} 我的最愛
    </button>
  );
}
