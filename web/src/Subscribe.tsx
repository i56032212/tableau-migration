import { useCallback, useEffect, useState } from "react";
import { SubscriptionDialog } from "./AdminSchedules";
import { AlertsTable } from "./Alerts";
import { ConfirmDialog } from "./dialogs";
import {
  PAPER_SIZES,
  scheduleApi,
  type ScheduleItem,
  type SubscriptionItem,
  type SubscriptionOptions,
  when,
} from "./schedules";
import { Modal } from "./Server";

// 訂閱（與 Tableau 的「訂閱」相同）：檢視器工具列的「訂閱」與「我的訂閱」頁面

/** 工具列的「訂閱」：伺服器沒有排程管理或沒有權限時不顯示。 */
export function SubscribeButton({ workbook, view }: { workbook: string; view: string | null }) {
  const [options, setOptions] = useState<SubscriptionOptions | null>(null);
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  useEffect(() => {
    scheduleApi
      .options(workbook)
      .then(setOptions)
      .catch(() => setOptions(null));
  }, [workbook]);
  if (!options) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        訂閱
      </button>
      {done && <span className="success">{done}</span>}
      {open && (
        <SubscribeDialog
          workbook={workbook}
          view={view}
          options={options}
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

function SubscribeDialog({
  workbook,
  view,
  options,
  onClose,
  onDone,
}: {
  workbook: string;
  view: string | null;
  options: SubscriptionOptions;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [content, setContent] = useState(view ? "view" : "workbook");
  const [subject, setSubject] = useState(view ?? "");
  const [schedule, setSchedule] = useState(options.schedules[0]?.id ?? "");
  const [format, setFormat] = useState("image");
  const [paper, setPaper] = useState("A4");
  const [orientation, setOrientation] = useState("Portrait");
  const [skipEmpty, setSkipEmpty] = useState(false);
  const [message, setMessage] = useState("");
  const [others, setOthers] = useState<string[]>([]);
  const [includeMe, setIncludeMe] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  if (options.schedules.length === 0) {
    return (
      <Modal title="訂閱" onClose={onClose}>
        <p>還沒有訂閱的排程，請站台管理員在「站台管理 → 排程」中建立。</p>
      </Modal>
    );
  }
  const submit = () => {
    const body: Record<string, unknown> = {
      report: workbook,
      view: content === "view" ? view : null,
      subject,
      schedule,
      message: message || null,
      attach_image: format !== "pdf",
      attach_pdf: format !== "image",
      send_if_view_empty: !skipEmpty,
    };
    if (format !== "image") {
      body.page_size = paper;
      body.page_orientation = orientation;
    }
    if (options.can_subscribe_others) {
      const users = [...(includeMe ? [options.username] : []), ...others];
      if (users.length === 0) {
        setError("請選擇訂閱者");
        return;
      }
      body.users = users;
    }
    scheduleApi
      .subscribe(body)
      .then((result) =>
        onDone(result.subscriptions.length > 1 ? `已訂閱（${result.subscriptions.length} 位）` : "已訂閱"),
      )
      .catch((e: Error) => setError(e.message));
  };
  const shown = options.users.filter(
    (u) => u !== options.username && u.toLowerCase().includes(filter.trim().toLowerCase()),
  );
  return (
    <Modal title="訂閱" onClose={onClose}>
      {!options.email && !options.can_subscribe_others ? (
        <p className="error">你在站台中沒有 email，無法訂閱；請站台管理員在使用者中加上 email。</p>
      ) : (
        <div className="publish-form">
          {options.can_subscribe_others && (
            <fieldset>
              <legend>訂閱者</legend>
              <label>
                <input type="checkbox" checked={includeMe} onChange={(e) => setIncludeMe(e.target.checked)} /> 我自己
                {options.email ? `（${options.email}）` : "（沒有 email）"}
              </label>
              {options.users.length > 8 && (
                <input
                  type="search"
                  placeholder="搜尋使用者"
                  aria-label="搜尋使用者"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              )}
              <div className="checklist">
                {shown.map((name) => (
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
          {view && (
            <fieldset>
              <legend>內容</legend>
              <label>
                <input type="radio" checked={content === "view"} onChange={() => setContent("view")} /> 這個 view「
                {view}」
              </label>
              <label>
                <input type="radio" checked={content === "workbook"} onChange={() => setContent("workbook")} /> 整份報表
              </label>
            </fieldset>
          )}
          <label>
            主旨
            <input aria-label="主旨" value={subject} onChange={(e) => setSubject(e.target.value)} />
          </label>
          <label>
            排程
            <select aria-label="排程" value={schedule} onChange={(e) => setSchedule(e.target.value)}>
              {options.schedules.map((s: ScheduleItem) => (
                <option key={s.id} value={s.id}>
                  {s.name}（{s.description}）
                </option>
              ))}
            </select>
          </label>
          <label>
            格式
            <select aria-label="格式" value={format} onChange={(e) => setFormat(e.target.value)}>
              <option value="image">圖片</option>
              <option value="pdf">PDF</option>
              <option value="both">圖片與 PDF</option>
            </select>
          </label>
          {format !== "image" && (
            <div className="row">
              <label>
                紙張{" "}
                <select aria-label="紙張大小" value={paper} onChange={(e) => setPaper(e.target.value)}>
                  {PAPER_SIZES.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                方向{" "}
                <select aria-label="紙張方向" value={orientation} onChange={(e) => setOrientation(e.target.value)}>
                  <option value="Portrait">直向</option>
                  <option value="Landscape">橫向</option>
                </select>
              </label>
            </div>
          )}
          <label>
            訊息（選填）
            <textarea aria-label="訊息" rows={3} value={message} onChange={(e) => setMessage(e.target.value)} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={skipEmpty} onChange={(e) => setSkipEmpty(e.target.checked)} /> view
            沒有資料時不寄送
          </label>
          {options.timezone && <p className="hint">排程的時間為 {options.timezone}。</p>}
          {error && <p className="error">{error}</p>}
        </div>
      )}
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!schedule} onClick={submit}>
          訂閱
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

/** 我的訂閱：修改主旨、排程與格式，暫停、繼續或取消訂閱。 */
export function MySubscriptionsPage({ go }: { go: (to: string) => void }) {
  const [data, setData] = useState<{
    timezone: string | null;
    schedules: ScheduleItem[];
    subscriptions: SubscriptionItem[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<SubscriptionItem | null>(null);
  const [removing, setRemoving] = useState<SubscriptionItem | null>(null);
  const reload = useCallback(() => {
    scheduleApi
      .mine()
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(reload, [reload]);
  const run = useCallback(
    async (work: () => Promise<unknown>, done?: string) => {
      try {
        await work();
        setNotice(done ?? null);
        setError(null);
        reload();
        return true;
      } catch (e) {
        setError((e as Error).message);
        return false;
      }
    },
    [reload],
  );
  return (
    <main className="home admin">
      <div className="home-header">
        <h1>我的訂閱</h1>
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
      {!data && !error && <p>載入中…</p>}
      {data && (
        <section className="admin-section standalone">
          {data.subscriptions.length === 0 ? (
            <p className="hint">還沒有訂閱：在報表的工具列按「訂閱」。</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>主旨</th>
                  <th>內容</th>
                  <th>排程</th>
                  <th>格式</th>
                  <th>上次寄送</th>
                  <th>下次寄送</th>
                  <th>狀態</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.subscriptions.map((sub) => (
                  <tr key={sub.id} data-subscription={sub.id}>
                    <td>{sub.subject}</td>
                    <td>
                      {sub.report_name ?? "—"}
                      {sub.view ? `／${sub.view}` : "（整份報表）"}
                    </td>
                    <td>{sub.schedule_name ?? sub.schedule}</td>
                    <td>{sub.attach_image && sub.attach_pdf ? "圖片與 PDF" : sub.attach_pdf ? "PDF" : "圖片"}</td>
                    <td>
                      {sub.last_run
                        ? `${sub.last_run.status === "ok" ? "成功" : "失敗"} ${when(sub.last_run.finished, data.timezone)}`
                        : "—"}
                    </td>
                    <td>{sub.suspended ? "—" : when(sub.next_run, data.timezone)}</td>
                    <td>
                      {sub.suspended ? (
                        <span className="warn-text">暫停</span>
                      ) : sub.problem ? (
                        <span className="error">{sub.problem}</span>
                      ) : (
                        "正常"
                      )}
                    </td>
                    <td className="actions">
                      <button type="button" className="link" onClick={() => setEditing(sub)}>
                        修改
                      </button>
                      <button
                        type="button"
                        className="link"
                        onClick={() =>
                          void run(
                            () => scheduleApi.updateSubscription(sub.id, { suspended: !sub.suspended }),
                            sub.suspended ? "已繼續" : "已暫停",
                          )
                        }
                      >
                        {sub.suspended ? "繼續" : "暫停"}
                      </button>
                      <button type="button" className="link danger-link" onClick={() => setRemoving(sub)}>
                        取消訂閱
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
      {data && (
        <section className="admin-section standalone">
          <h3>資料警示</h3>
          <AlertsTable admin={false} timezone={data.timezone} run={run} />
        </section>
      )}
      {editing && data && (
        <SubscriptionDialog
          schedules={data.schedules}
          subscription={editing}
          onClose={() => setEditing(null)}
          run={run}
        />
      )}
      {removing && (
        <ConfirmDialog
          title="取消訂閱"
          message={`取消訂閱「${removing.subject}」？`}
          confirm="取消訂閱"
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => scheduleApi.unsubscribe(removing.id), "已取消訂閱")}
        />
      )}
    </main>
  );
}
