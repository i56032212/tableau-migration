import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "./dialogs";
import { alertApi, type AlertItem, type AlertOptions, when } from "./schedules";
import { Modal } from "./Server";
import { stateFor, type ViewState } from "./state";
import type { IR } from "./types";

// 資料警示（與 Tableau 的 data-driven alert 相同）：工具列的「警示」與警示清單

function numberText(value: number): string {
  return Number.isInteger(value)
    ? value.toLocaleString("en-US")
    : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** 工具列的「警示」：選擇工作表的量值、條件與門檻。 */
export function AlertButton({
  workbook,
  view,
  ir,
  sheets,
  state,
}: {
  workbook: string;
  view: string;
  ir: IR;
  sheets: { id: string; name: string }[];
  state: ViewState;
}) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  if (sheets.length === 0) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        警示
      </button>
      {done && <span className="success">{done}</span>}
      {open && (
        <AlertDialog
          workbook={workbook}
          view={view}
          ir={ir}
          sheets={sheets}
          state={state}
          onClose={() => setOpen(false)}
          onDone={(message) => {
            setOpen(false);
            setDone(message);
          }}
        />
      )}
    </>
  );
}

function AlertDialog({
  workbook,
  view,
  ir,
  sheets,
  state,
  onClose,
  onDone,
}: {
  workbook: string;
  view: string;
  ir: IR;
  sheets: { id: string; name: string }[];
  state: ViewState;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [sheet, setSheet] = useState(sheets[0].id);
  const [options, setOptions] = useState<AlertOptions | null>(null);
  const [measure, setMeasure] = useState("");
  const [condition, setCondition] = useState("above");
  const [threshold, setThreshold] = useState("");
  const [frequency, setFrequency] = useState("data");
  const [subject, setSubject] = useState("");
  const [others, setOthers] = useState<string[]>([]);
  const [includeMe, setIncludeMe] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setOptions(null);
    setError(null);
    alertApi
      .options(workbook, sheet, stateFor(ir, sheet, state))
      .then((loaded) => {
        setOptions(loaded);
        const first = loaded.measures[0];
        setMeasure(first?.key ?? "");
        setThreshold(first ? String(Math.round(first.max)) : "");
      })
      .catch((e: Error) => setError(e.message));
  }, [workbook, sheet, ir, state]);
  const chosen = options?.measures.find((m) => m.key === measure);
  const submit = () => {
    const value = Number(threshold);
    if (!Number.isFinite(value)) {
      setError("門檻要是數字");
      return;
    }
    const recipients = options?.can_add_others ? [...(includeMe ? [options.username] : []), ...others] : undefined;
    alertApi
      .create({
        report: workbook,
        view,
        worksheet: sheet,
        measure,
        condition,
        threshold: value,
        frequency,
        subject,
        recipients,
        state: stateFor(ir, sheet, state),
      })
      .then((created) => onDone(`已建立警示：${created.description}`))
      .catch((e: Error) => setError(e.message));
  };
  return (
    <Modal title="建立資料警示" onClose={onClose}>
      <div className="publish-form">
        {sheets.length > 1 && (
          <label>
            工作表
            <select aria-label="工作表" value={sheet} onChange={(e) => setSheet(e.target.value)}>
              {sheets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {!options && !error && <p>載入中…</p>}
        {options && options.measures.length === 0 && <p className="hint">這張工作表沒有可以設定警示的量值。</p>}
        {options && options.measures.length > 0 && (
          <>
            <label>
              量值
              <select
                aria-label="量值"
                value={measure}
                onChange={(e) => {
                  setMeasure(e.target.value);
                  const next = options.measures.find((m) => m.key === e.target.value);
                  if (next) setThreshold(String(Math.round(next.max)));
                }}
              >
                {options.measures.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.name}
                  </option>
                ))}
              </select>
            </label>
            {chosen && (
              <p className="hint">
                目前的範圍：{numberText(chosen.min)} – {numberText(chosen.max)}
              </p>
            )}
            <div className="row">
              <label>
                條件{" "}
                <select aria-label="條件" value={condition} onChange={(e) => setCondition(e.target.value)}>
                  {Object.entries(options.conditions).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                門檻{" "}
                <input
                  aria-label="門檻"
                  inputMode="decimal"
                  value={threshold}
                  onChange={(e) => setThreshold(e.target.value)}
                />
              </label>
            </div>
            <label>
              寄送頻率
              <select aria-label="寄送頻率" value={frequency} onChange={(e) => setFrequency(e.target.value)}>
                {Object.entries(options.frequencies).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              主旨（選填）
              <input aria-label="警示的主旨" value={subject} onChange={(e) => setSubject(e.target.value)} />
            </label>
            {options.can_add_others && (
              <fieldset>
                <legend>收件人</legend>
                <label className="inline">
                  <input type="checkbox" checked={includeMe} onChange={(e) => setIncludeMe(e.target.checked)} /> 我自己
                </label>
                <div className="checklist">
                  {options.users
                    .filter((u) => u !== options.username)
                    .map((name) => (
                      <label key={name}>
                        <input
                          type="checkbox"
                          checked={others.includes(name)}
                          onChange={(e) =>
                            setOthers(e.target.checked ? [...others, name] : others.filter((o) => o !== name))
                          }
                        />{" "}
                        {name}
                      </label>
                    ))}
                </div>
              </fieldset>
            )}
            {!options.email && !options.can_add_others && (
              <p className="error">你在站台中沒有 email，收不到警示；請站台管理員在使用者中加上 email。</p>
            )}
            <p className="hint">
              萃取更新後檢查（Live
              連線每小時檢查）；每位收件人以自己看得到的資料判斷，條件成立時寄信。篩選與參數為目前畫面上的。
            </p>
          </>
        )}
        {error && <p className="error">{error}</p>}
      </div>
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!measure || !threshold.trim()} onClick={submit}>
          建立
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

type Run = (work: () => Promise<unknown>, done?: string) => Promise<boolean>;

/** 警示清單：我的訂閱（admin 為 false）與站台管理的工作（admin 為 true）。 */
export function AlertsTable({
  admin,
  timezone,
  run,
  showRuns,
  version = 0,
}: {
  admin: boolean;
  timezone: string | null;
  run: Run;
  showRuns?: (job: string) => void;
  version?: number;
}) {
  const [alerts, setAlerts] = useState<AlertItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<AlertItem | null>(null);
  const reload = useCallback(() => {
    (admin ? alertApi.all() : alertApi.mine())
      .then((data) => setAlerts(data.alerts))
      .catch((e: Error) => setError(e.message));
  }, [admin]);
  useEffect(reload, [reload, version]);
  const act = (work: () => Promise<unknown>, done: string) => void run(work, done).then((ok) => ok && reload());
  if (error) return <p className="error">{error}</p>;
  if (!alerts) return <p>載入中…</p>;
  if (alerts.length === 0)
    return <p className="hint">{admin ? "沒有資料警示。" : "沒有資料警示：在報表的工具列按「警示」。"}</p>;
  return (
    <>
      <table className="admin-table">
        <thead>
          <tr>
            <th>條件</th>
            <th>報表</th>
            <th>收件人</th>
            <th>頻率</th>
            <th>上次檢查</th>
            <th>狀態</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {alerts.map((alert) => {
            const manage = admin || alert.mine;
            return (
              <tr key={alert.id} data-alert={alert.id}>
                <td>{alert.subject ? `${alert.subject}（${alert.description}）` : alert.description}</td>
                <td>{alert.report}</td>
                <td>{alert.recipients.join("、")}</td>
                <td>{alert.frequency_name}</td>
                <td>{when(alert.last_checked, timezone)}</td>
                <td>
                  {alert.suspended ? (
                    <span className="warn-text">
                      暫停{alert.consecutive_failures ? `（連續失敗 ${alert.consecutive_failures} 次）` : ""}
                    </span>
                  ) : alert.consecutive_failures ? (
                    <span className="warn-text">連續失敗 {alert.consecutive_failures} 次</span>
                  ) : (
                    "正常"
                  )}
                </td>
                <td className="actions">
                  {manage && (
                    <>
                      <button
                        type="button"
                        className="link"
                        onClick={() => act(() => alertApi.check(alert.id), "已排入佇列，排程器會在幾秒內檢查")}
                      >
                        立即檢查
                      </button>
                      <button
                        type="button"
                        className="link"
                        onClick={() =>
                          act(
                            () => alertApi.update(alert.id, { suspended: !alert.suspended }),
                            alert.suspended ? "已繼續" : "已暫停",
                          )
                        }
                      >
                        {alert.suspended ? "繼續" : "暫停"}
                      </button>
                    </>
                  )}
                  {showRuns && (
                    <button type="button" className="link" onClick={() => showRuns(`alert:${alert.id}`)}>
                      紀錄
                    </button>
                  )}
                  {!manage && (
                    <button
                      type="button"
                      className="link"
                      onClick={() => act(() => alertApi.leave(alert.id), "已不再收到這個警示")}
                    >
                      不再收到
                    </button>
                  )}
                  {manage && (
                    <button type="button" className="link danger-link" onClick={() => setRemoving(alert)}>
                      刪除
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {removing && (
        <ConfirmDialog
          title="刪除資料警示"
          message={`刪除資料警示「${removing.subject || removing.description}」？`}
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => alertApi.remove(removing.id), "已刪除資料警示").then((ok) => (ok && reload(), ok))}
        />
      )}
    </>
  );
}
