import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertsTable } from "./Alerts";
import { ConfirmDialog } from "./dialogs";
import {
  type ExtractTaskItem,
  type Frequency,
  type FrequencyKind,
  ORDINALS,
  type RunItem,
  scheduleApi,
  type ScheduleItem,
  type ScheduleOverview,
  type SubscriptionItem,
  when,
  WEEKDAYS,
} from "./schedules";
import { Modal } from "./Server";

// 站台管理的排程、工作（萃取更新與訂閱）與執行紀錄

type Run = (work: () => Promise<unknown>, done?: string) => Promise<boolean>;

/** 排程資料：各分頁共用，修改後重新讀取。 */
export function useSchedules() {
  const [data, setData] = useState<ScheduleOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    scheduleApi
      .overview()
      .then((loaded) => {
        setData(loaded);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(reload, [reload]);
  return { data, error, reload };
}

function SchedulerBanner({ data }: { data: ScheduleOverview }) {
  const status = data.scheduler;
  const active = status.active ?? [];
  return status.running ? (
    <p className="scheduler-status ok">
      排程器執行中（最後回報 {when(status.heartbeat_at, data.timezone)}）
      {active.length > 0 && `；${active.length} 個工作執行中`}
      {(status.waiting ?? 0) > 0 && `、${status.waiting} 個等候空出的位置`}
      {status.queued.length > 0 && `；${status.queued.length} 個工作排隊等待立即執行`}
    </p>
  ) : (
    <p className="scheduler-status warn">
      排程器沒有在執行：排程與「立即執行」要等排程器啟動後才會執行（
      <code>uv run tmig schedule serve --store &lt;報表庫&gt; --mail mail.toml</code>）
      {status.queued.length > 0 && `；${status.queued.length} 個工作排隊中`}
    </p>
  );
}

// ------------------------------------------------------------ 排程

export function SchedulesTab({ data, run }: { data: ScheduleOverview; run: Run }) {
  const [editing, setEditing] = useState<ScheduleItem | "new" | null>(null);
  const [removing, setRemoving] = useState<ScheduleItem | null>(null);
  return (
    <section className="admin-section">
      <SchedulerBanner data={data} />
      <TimezoneRow data={data} run={run} />
      <WorkersRow data={data} run={run} />
      <div className="admin-actions">
        <button type="button" className="primary" disabled={!data.timezone} onClick={() => setEditing("new")}>
          新增排程
        </button>
        {data.source && <span className="hint">匯入自 {data.source}</span>}
      </div>
      <table className="admin-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>類型</th>
            <th>頻率</th>
            <th>優先順序</th>
            <th>執行方式</th>
            <th>狀態</th>
            <th>工作</th>
            <th>下次執行</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.schedules.map((schedule) => (
            <tr key={schedule.id} data-schedule={schedule.id}>
              <td>{schedule.name}</td>
              <td>
                {schedule.kind === "Subscription"
                  ? "訂閱"
                  : schedule.kind === "Extract"
                    ? "萃取更新"
                    : (schedule.kind ?? "—")}
              </td>
              <td>{schedule.description ?? <span className="error">無法辨識：{schedule.problem}</span>}</td>
              <td>{schedule.priority ?? 50}</td>
              <td>{schedule.execution === "Serial" ? "依序" : "平行"}</td>
              <td>{schedule.state === "Active" ? "啟用" : <span className="warn-text">暫停</span>}</td>
              <td>
                {schedule.tasks + schedule.subscriptions}
                {schedule.subscriptions > 0 && schedule.tasks > 0 && (
                  <span className="hint">
                    （萃取 {schedule.tasks}、訂閱 {schedule.subscriptions}）
                  </span>
                )}
              </td>
              <td>{when(schedule.next_run, data.timezone)}</td>
              <td className="actions">
                <button type="button" className="link" onClick={() => setEditing(schedule)}>
                  修改
                </button>
                <button
                  type="button"
                  className="link"
                  onClick={() =>
                    void run(
                      () =>
                        scheduleApi.updateSchedule(schedule.id, {
                          state: schedule.state === "Active" ? "Suspended" : "Active",
                        }),
                      schedule.state === "Active" ? "已暫停排程" : "已繼續排程",
                    )
                  }
                >
                  {schedule.state === "Active" ? "暫停" : "繼續"}
                </button>
                <button type="button" className="link danger-link" onClick={() => setRemoving(schedule)}>
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.schedules.length === 0 && <p className="hint">還沒有排程。</p>}
      {editing && (
        <ScheduleDialog schedule={editing === "new" ? null : editing} onClose={() => setEditing(null)} run={run} />
      )}
      {removing && (
        <ConfirmDialog
          title="刪除排程"
          message={
            removing.tasks + removing.subscriptions > 0
              ? `排程「${removing.name}」還有 ${removing.tasks + removing.subscriptions} 個工作，要先移到其他排程或刪除。`
              : `刪除排程「${removing.name}」？`
          }
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => scheduleApi.deleteSchedule(removing.id), "已刪除排程")}
        />
      )}
    </section>
  );
}

function TimezoneRow({ data, run }: { data: ScheduleOverview; run: Run }) {
  const guess = useMemo(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Taipei";
    } catch {
      return "Asia/Taipei";
    }
  }, []);
  const [editing, setEditing] = useState(!data.timezone);
  const [value, setValue] = useState(data.timezone ?? guess);
  if (!editing) {
    return (
      <p>
        排程的時區：<strong>{data.timezone}</strong>{" "}
        <button type="button" className="link" onClick={() => setEditing(true)}>
          修改
        </button>
      </p>
    );
  }
  return (
    <div className="admin-actions">
      <label>
        排程的時區（IANA 名稱）{" "}
        <input aria-label="排程的時區" value={value} onChange={(e) => setValue(e.target.value)} />
      </label>
      <button
        type="button"
        onClick={() =>
          void run(() => scheduleApi.settings({ timezone: value.trim() }), "已設定時區").then(
            (ok) => ok && setEditing(false),
          )
        }
      >
        儲存
      </button>
      {!data.timezone && <span className="hint">先設定時區才能新增排程（Tableau Server 匯出的排程檔中有時區）。</span>}
    </div>
  );
}

/** 同時執行的工作數上限（與 Tableau Server 的背景程式數相同的作用）。 */
function WorkersRow({ data, run }: { data: ScheduleOverview; run: Run }) {
  const [editing, setEditing] = useState(false);
  const current = data.workers ?? data.default_workers;
  const [value, setValue] = useState(String(current));
  const reported = data.scheduler.running ? data.scheduler.workers : undefined;
  const save = (workers: number | null, done: string) =>
    void run(() => scheduleApi.settings({ workers }), done).then((ok) => ok && setEditing(false));
  if (!editing) {
    return (
      <p className="workers-row">
        同時執行：最多 <strong>{current}</strong> 個工作{data.workers === null && "（預設）"}{" "}
        <button
          type="button"
          className="link"
          onClick={() => {
            setValue(String(current));
            setEditing(true);
          }}
        >
          修改
        </button>
        {reported !== undefined && reported !== current && (
          <span className="hint">排程器目前用 {reported} 個（下一次檢查時改用新的設定）</span>
        )}
      </p>
    );
  }
  const number = Number(value);
  const valid = Number.isInteger(number) && number >= 1 && number <= data.max_workers;
  return (
    <div className="admin-actions">
      <label>
        同時執行的工作數上限（1–{data.max_workers}）{" "}
        <input
          type="number"
          aria-label="同時執行的工作數上限"
          min={1}
          max={data.max_workers}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <button type="button" disabled={!valid} onClick={() => save(number, "已設定同時執行的上限")}>
        儲存
      </button>
      {data.workers !== null && (
        <button type="button" onClick={() => save(null, "已改用預設的上限")}>
          使用預設（{data.default_workers}）
        </button>
      )}
      <button type="button" onClick={() => setEditing(false)}>
        取消
      </button>
      <span className="hint">
        與 Tableau Server 的背景程式數相同的作用：1 為一個接一個執行；「依序」的排程不論上限一次只執行一個工作。
      </span>
    </div>
  );
}

const INTERVALS: [string, string][] = [
  ["m15", "15 分鐘"],
  ["m30", "30 分鐘"],
  ["h1", "1 小時"],
  ["h2", "2 小時"],
  ["h4", "4 小時"],
  ["h6", "6 小時"],
  ["h8", "8 小時"],
  ["h12", "12 小時"],
];

function intervalOf(frequency: Frequency): string {
  if (frequency.every_minutes) return `m${frequency.every_minutes}`;
  if (frequency.every_hours) return `h${frequency.every_hours}`;
  return "";
}

function hhmm(time: string | null | undefined): string {
  return (time ?? "").slice(0, 5);
}

const DEFAULT_FREQUENCY: Frequency = { kind: "daily", start: "06:00:00", weekdays: [], month_days: [] };

function ScheduleDialog({ schedule, onClose, run }: { schedule: ScheduleItem | null; onClose: () => void; run: Run }) {
  const [name, setName] = useState(schedule?.name ?? "");
  const [kind, setKind] = useState(schedule?.kind ?? "Extract");
  const [priority, setPriority] = useState(schedule?.priority ?? 50);
  const [execution, setExecution] = useState(schedule?.execution ?? "Parallel");
  const [frequency, setFrequency] = useState<Frequency>(schedule?.frequency ?? DEFAULT_FREQUENCY);
  const save = () => {
    const body = { name: name.trim(), kind, priority, execution, frequency };
    void run(
      () => (schedule ? scheduleApi.updateSchedule(schedule.id, body) : scheduleApi.createSchedule(body)),
      schedule ? "已更新排程" : "已新增排程",
    ).then((ok) => ok && onClose());
  };
  return (
    <Modal title={schedule ? `排程「${schedule.name}」` : "新增排程"} onClose={onClose}>
      <label className="admin-field">
        名稱
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="admin-field">
        類型
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          disabled={schedule !== null && schedule.tasks + schedule.subscriptions > 0}
        >
          <option value="Extract">萃取更新</option>
          <option value="Subscription">訂閱</option>
        </select>
      </label>
      <label className="admin-field">
        優先順序（1–100，數字小的先執行）
        <input
          type="number"
          min={1}
          max={100}
          value={priority}
          onChange={(e) => setPriority(Number(e.target.value) || 50)}
        />
      </label>
      <label className="admin-field">
        執行方式
        <select value={execution} onChange={(e) => setExecution(e.target.value as "Parallel" | "Serial")}>
          <option value="Parallel">平行：工作可以同時執行（不超過同時執行的上限）</option>
          <option value="Serial">依序：這個排程的工作一次執行一個</option>
        </select>
      </label>
      <FrequencyEditor value={frequency} onChange={setFrequency} />
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!name.trim()} onClick={save}>
          儲存
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

/** 頻率：每小時（間隔、起訖時間）、每天、每週（星期）、每月（日期或第幾個星期幾）。 */
export function FrequencyEditor({ value, onChange }: { value: Frequency; onChange: (next: Frequency) => void }) {
  const set = (patch: Partial<Frequency>) => onChange({ ...value, ...patch });
  const setKind = (kind: FrequencyKind) => {
    const base: Frequency = { kind, start: value.start, weekdays: [], month_days: [] };
    if (kind === "hourly") base.every_hours = 1;
    if (kind === "weekly") base.weekdays = ["Monday"];
    if (kind === "monthly") base.month_days = [1];
    onChange(base);
  };
  const toggleDay = (day: string) =>
    set({
      weekdays: value.weekdays.includes(day) ? value.weekdays.filter((d) => d !== day) : [...value.weekdays, day],
    });
  const monthly = value.month_weekday ? "weekday" : "days";
  return (
    <fieldset className="frequency">
      <legend>頻率</legend>
      <div className="row">
        <select aria-label="頻率" value={value.kind} onChange={(e) => setKind(e.target.value as FrequencyKind)}>
          <option value="hourly">每小時</option>
          <option value="daily">每天</option>
          <option value="weekly">每週</option>
          <option value="monthly">每月</option>
        </select>
        {(value.kind === "hourly" || value.kind === "daily") && (
          <label>
            每隔{" "}
            <select
              aria-label="間隔"
              value={intervalOf(value)}
              onChange={(e) => {
                const v = e.target.value;
                if (!v) set({ every_hours: null, every_minutes: null });
                else if (v.startsWith("m")) set({ every_minutes: Number(v.slice(1)), every_hours: null });
                else set({ every_hours: Number(v.slice(1)), every_minutes: null });
              }}
            >
              {value.kind === "daily" && <option value="">（一天一次）</option>}
              {INTERVALS.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          {value.kind === "hourly" || intervalOf(value) ? "從" : "時間"}{" "}
          <input
            type="time"
            aria-label="開始時間"
            value={hhmm(value.start)}
            onChange={(e) => set({ start: `${e.target.value}:00` })}
          />
        </label>
        {(value.kind === "hourly" || (value.kind === "daily" && intervalOf(value))) && (
          <label>
            到{" "}
            <input
              type="time"
              aria-label="結束時間"
              value={hhmm(value.end)}
              onChange={(e) => set({ end: e.target.value ? `${e.target.value}:00` : null })}
            />
          </label>
        )}
      </div>
      {value.kind !== "monthly" && (
        <div className="row weekdays" role="group" aria-label="星期">
          {value.kind !== "weekly" && <span className="hint">星期（不選為每天）：</span>}
          {WEEKDAYS.map(([day, label]) => (
            <label key={day}>
              <input type="checkbox" checked={value.weekdays.includes(day)} onChange={() => toggleDay(day)} />
              {label}
            </label>
          ))}
        </div>
      )}
      {value.kind === "monthly" && (
        <div className="column">
          <div className="row">
            <label>
              <input
                type="radio"
                checked={monthly === "days"}
                onChange={() => set({ month_weekday: null, weekdays: [], month_days: [1] })}
              />
              每月的
            </label>
            <select
              aria-label="每月的日期"
              disabled={monthly !== "days"}
              value={String(value.month_days[0] ?? 1)}
              onChange={(e) => set({ month_days: [e.target.value === "LastDay" ? "LastDay" : Number(e.target.value)] })}
            >
              {Array.from({ length: 31 }, (_, i) => (
                <option key={i + 1} value={String(i + 1)}>
                  {i + 1} 日
                </option>
              ))}
              <option value="LastDay">月底</option>
            </select>
          </div>
          <div className="row">
            <label>
              <input
                type="radio"
                checked={monthly === "weekday"}
                onChange={() => set({ month_weekday: "First", weekdays: ["Monday"], month_days: [] })}
              />
              每月的
            </label>
            <select
              aria-label="第幾個"
              disabled={monthly !== "weekday"}
              value={value.month_weekday ?? "First"}
              onChange={(e) => set({ month_weekday: e.target.value })}
            >
              {ORDINALS.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
            <select
              aria-label="星期幾"
              disabled={monthly !== "weekday"}
              value={value.weekdays[0] ?? "Monday"}
              onChange={(e) => set({ weekdays: [e.target.value] })}
            >
              {WEEKDAYS.map(([day, label]) => (
                <option key={day} value={day}>
                  星期{label}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </fieldset>
  );
}

// ------------------------------------------------------------ 工作

function LastRun({ item, timezone }: { item: RunItem | null; timezone: string | null }) {
  if (!item) return <span className="hint">—</span>;
  return (
    <span title={item.detail} className={item.status === "ok" ? "run-ok" : "run-failed"}>
      {item.status === "ok" ? "成功" : "失敗"} {when(item.finished, timezone)}
    </span>
  );
}

function TaskState({ suspended, failures, problem }: { suspended: boolean; failures: number; problem: string | null }) {
  if (suspended) return <span className="warn-text">暫停{failures > 0 ? `（連續失敗 ${failures} 次）` : ""}</span>;
  if (problem) return <span className="error">{problem}</span>;
  if (failures > 0) return <span className="warn-text">連續失敗 {failures} 次</span>;
  return <span>正常</span>;
}

export function TasksTab({
  data,
  run,
  showRuns,
}: {
  data: ScheduleOverview;
  run: Run;
  showRuns: (job: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [editingTask, setEditingTask] = useState<ExtractTaskItem | null>(null);
  const [editingSub, setEditingSub] = useState<SubscriptionItem | null>(null);
  const [removingTask, setRemovingTask] = useState<ExtractTaskItem | null>(null);
  const [removingSub, setRemovingSub] = useState<SubscriptionItem | null>(null);
  const tz = data.timezone;
  const runNow = (job: string) =>
    void run(
      () => scheduleApi.run(job),
      data.scheduler.running ? "已排入佇列，排程器會在幾秒內執行" : "已排入佇列；排程器沒有在執行，啟動後才會執行",
    );
  return (
    <section className="admin-section">
      <SchedulerBanner data={data} />
      <h3>萃取更新</h3>
      <div className="admin-actions">
        <button
          type="button"
          className="primary"
          disabled={!data.schedules.some((s) => s.kind !== "Subscription")}
          onClick={() => setAdding(true)}
        >
          新增萃取更新
        </button>
        <span className="hint">連續失敗 {data.suspend_after} 次的工作會暫停，並寄信給報表的擁有者。</span>
      </div>
      <table className="admin-table">
        <thead>
          <tr>
            <th>對象</th>
            <th>排程</th>
            <th>類型</th>
            <th>上次執行</th>
            <th>下次執行</th>
            <th>狀態</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.extract_tasks.map((task) => (
            <tr key={task.id} data-task={task.id}>
              <td>{task.targets.length ? task.targets.join("、") : task.label}</td>
              <td>{task.schedule_name ?? task.schedule}</td>
              <td>{task.refresh === "incremental" ? "增量" : "完整"}</td>
              <td>
                <LastRun item={task.last_run} timezone={tz} />
              </td>
              <td>{task.suspended ? "—" : when(task.next_run, tz)}</td>
              <td>
                <TaskState suspended={task.suspended} failures={task.consecutive_failures} problem={task.problem} />
              </td>
              <td className="actions">
                <button
                  type="button"
                  className="link"
                  disabled={!!task.problem && !task.suspended}
                  onClick={() => runNow(task.job)}
                >
                  立即執行
                </button>
                <button type="button" className="link" onClick={() => setEditingTask(task)}>
                  修改
                </button>
                <button
                  type="button"
                  className="link"
                  onClick={() =>
                    void run(
                      () => scheduleApi.updateTask(task.id, { suspended: !task.suspended }),
                      task.suspended ? "已繼續" : "已暫停",
                    )
                  }
                >
                  {task.suspended ? "繼續" : "暫停"}
                </button>
                <button type="button" className="link" onClick={() => showRuns(task.job)}>
                  紀錄
                </button>
                <button type="button" className="link danger-link" onClick={() => setRemovingTask(task)}>
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.extract_tasks.length === 0 && <p className="hint">沒有萃取更新工作。</p>}

      <h3>訂閱</h3>
      <p className="hint">
        使用者在檢視器的「訂閱」中訂閱；連續失敗 {data.suspend_after} 次的訂閱會暫停，並寄信給訂閱者。
      </p>
      <table className="admin-table">
        <thead>
          <tr>
            <th>主旨</th>
            <th>訂閱者</th>
            <th>內容</th>
            <th>排程</th>
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
              <td>{sub.user}</td>
              <td>
                {sub.report_name ?? "—"}
                {sub.view ? `／${sub.view}` : "（整份報表）"}
              </td>
              <td>{sub.schedule_name ?? sub.schedule}</td>
              <td>
                <LastRun item={sub.last_run} timezone={tz} />
              </td>
              <td>{sub.suspended ? "—" : when(sub.next_run, tz)}</td>
              <td>
                <TaskState
                  suspended={sub.suspended}
                  failures={sub.consecutive_failures}
                  problem={sub.suspended ? null : sub.problem}
                />
              </td>
              <td className="actions">
                <button
                  type="button"
                  className="link"
                  disabled={!!sub.problem && !sub.suspended}
                  onClick={() => runNow(sub.job)}
                >
                  立即寄送
                </button>
                <button type="button" className="link" onClick={() => setEditingSub(sub)}>
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
                <button type="button" className="link" onClick={() => showRuns(sub.job)}>
                  紀錄
                </button>
                <button type="button" className="link danger-link" onClick={() => setRemovingSub(sub)}>
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.subscriptions.length === 0 && <p className="hint">沒有訂閱。</p>}

      <h3>資料警示</h3>
      <p className="hint">
        使用者在檢視器的「警示」中建立；萃取更新後檢查（Live 連線每小時檢查），每位收件人以自己看得到的資料判斷。
      </p>
      <AlertsTable admin timezone={tz} run={run} showRuns={showRuns} />

      {adding && <NewTaskDialog data={data} onClose={() => setAdding(false)} run={run} />}
      {editingTask && <EditTaskDialog data={data} task={editingTask} onClose={() => setEditingTask(null)} run={run} />}
      {editingSub && (
        <SubscriptionDialog
          schedules={data.schedules.filter((s) => s.kind !== "Extract")}
          subscription={editingSub}
          onClose={() => setEditingSub(null)}
          run={run}
        />
      )}
      {removingTask && (
        <ConfirmDialog
          title="刪除萃取更新工作"
          message={`刪除「${removingTask.targets.join("、") || removingTask.label}」的萃取更新工作？`}
          onClose={() => setRemovingTask(null)}
          onConfirm={() => run(() => scheduleApi.deleteTask(removingTask.id), "已刪除萃取更新工作")}
        />
      )}
      {removingSub && (
        <ConfirmDialog
          title="刪除訂閱"
          message={`刪除 ${removingSub.user} 的訂閱「${removingSub.subject}」？`}
          onClose={() => setRemovingSub(null)}
          onConfirm={() => run(() => scheduleApi.unsubscribe(removingSub.id), "已刪除訂閱")}
        />
      )}
    </section>
  );
}

function NewTaskDialog({ data, onClose, run }: { data: ScheduleOverview; onClose: () => void; run: Run }) {
  const reports = data.reports.filter((r) => r.datasources.length > 0);
  const schedules = data.schedules.filter((s) => s.kind !== "Subscription");
  const [report, setReport] = useState(reports[0]?.id ?? "");
  const [schedule, setSchedule] = useState(schedules[0]?.id ?? "");
  const [refresh, setRefresh] = useState("full");
  const chosen = reports.find((r) => r.id === report);
  return (
    <Modal title="新增萃取更新" onClose={onClose}>
      {reports.length === 0 ? (
        <p className="hint">沒有萃取型資料源的報表。</p>
      ) : (
        <>
          <label className="admin-field">
            報表
            <select value={report} onChange={(e) => setReport(e.target.value)}>
              {reports.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                  {r.published ? "" : "（fixtures）"}
                </option>
              ))}
            </select>
          </label>
          {chosen && <p className="hint">更新的資料源：{chosen.datasources.map((d) => d.caption).join("、")}</p>}
          <label className="admin-field">
            排程
            <select value={schedule} onChange={(e) => setSchedule(e.target.value)}>
              {schedules.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}（{s.description ?? "無法辨識"}）
                </option>
              ))}
            </select>
          </label>
          <label className="admin-field">
            更新方式
            <select value={refresh} onChange={(e) => setRefresh(e.target.value)}>
              <option value="full">完整更新</option>
              <option value="incremental">增量更新</option>
            </select>
          </label>
        </>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!report || !schedule}
          onClick={() =>
            void run(() => scheduleApi.createTask({ report, schedule, refresh }), "已新增萃取更新").then(
              (ok) => ok && onClose(),
            )
          }
        >
          新增
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

function EditTaskDialog({
  data,
  task,
  onClose,
  run,
}: {
  data: ScheduleOverview;
  task: ExtractTaskItem;
  onClose: () => void;
  run: Run;
}) {
  const schedules = data.schedules.filter((s) => s.kind !== "Subscription");
  const [schedule, setSchedule] = useState(task.schedule);
  const [refresh, setRefresh] = useState(task.refresh);
  const [priority, setPriority] = useState<number | null>(task.priority);
  return (
    <Modal title={`萃取更新「${task.targets.join("、") || task.label}」`} onClose={onClose}>
      <label className="admin-field">
        排程
        <select value={schedule} onChange={(e) => setSchedule(e.target.value)}>
          {schedules.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}（{s.description ?? "無法辨識"}）
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        更新方式
        <select value={refresh} onChange={(e) => setRefresh(e.target.value as "full" | "incremental")}>
          <option value="full">完整更新</option>
          <option value="incremental">增量更新</option>
        </select>
      </label>
      <label className="admin-field">
        優先順序（空白時用排程的）
        <input
          type="number"
          min={1}
          max={100}
          value={priority ?? ""}
          onChange={(e) => setPriority(e.target.value ? Number(e.target.value) : null)}
        />
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() =>
            void run(() => scheduleApi.updateTask(task.id, { schedule, refresh, priority }), "已更新萃取更新").then(
              (ok) => ok && onClose(),
            )
          }
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

/** 修改訂閱：主旨、排程、格式（管理員與「我的訂閱」共用）。 */
export function SubscriptionDialog({
  schedules,
  subscription,
  onClose,
  run,
}: {
  schedules: ScheduleItem[];
  subscription: SubscriptionItem;
  onClose: () => void;
  run: Run;
}) {
  const [subject, setSubject] = useState(subscription.subject);
  const [schedule, setSchedule] = useState(subscription.schedule);
  const [format, setFormat] = useState(
    subscription.attach_image && subscription.attach_pdf ? "both" : subscription.attach_pdf ? "pdf" : "image",
  );
  const [skipEmpty, setSkipEmpty] = useState(!subscription.send_if_view_empty);
  return (
    <Modal title={`訂閱「${subscription.subject}」`} onClose={onClose}>
      <label className="admin-field">
        主旨
        <input value={subject} onChange={(e) => setSubject(e.target.value)} />
      </label>
      <label className="admin-field">
        排程
        <select value={schedule} onChange={(e) => setSchedule(e.target.value)}>
          {schedules.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}（{s.description ?? "無法辨識"}）
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        格式
        <select value={format} onChange={(e) => setFormat(e.target.value)}>
          <option value="image">圖片</option>
          <option value="pdf">PDF</option>
          <option value="both">圖片與 PDF</option>
        </select>
      </label>
      <label>
        <input type="checkbox" checked={skipEmpty} onChange={(e) => setSkipEmpty(e.target.checked)} /> view
        沒有資料時不寄送
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() =>
            void run(
              () =>
                scheduleApi.updateSubscription(subscription.id, {
                  subject,
                  schedule,
                  attach_image: format !== "pdf",
                  attach_pdf: format !== "image",
                  send_if_view_empty: !skipEmpty,
                }),
              "已更新訂閱",
            ).then((ok) => ok && onClose())
          }
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

// ------------------------------------------------------------ 執行紀錄

export function RunsTab({ data, job, setJob }: { data: ScheduleOverview; job: string; setJob: (job: string) => void }) {
  const [runs, setRuns] = useState<RunItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    scheduleApi
      .runs(job || undefined)
      .then((loaded) => {
        setRuns(loaded);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [job]);
  useEffect(load, [load]);
  const labels = new Map<string, string>([
    ...data.extract_tasks.map((t): [string, string] => [t.job, `萃取更新：${t.targets.join("、") || t.label}`]),
    ...data.subscriptions.map((s): [string, string] => [s.job, `訂閱「${s.subject}」：${s.user}`]),
  ]);
  return (
    <section className="admin-section">
      <div className="admin-actions">
        <select aria-label="工作" value={job} onChange={(e) => setJob(e.target.value)}>
          <option value="">全部的工作</option>
          {[...labels].map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
        <button type="button" onClick={load}>
          重新整理
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {runs && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>開始</th>
              <th>結束</th>
              <th>工作</th>
              <th>觸發</th>
              <th>結果</th>
              <th>說明</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((item, index) => (
              <tr key={index} className={item.status === "ok" ? "" : "failed-row"}>
                <td>{when(item.started, data.timezone)}</td>
                <td>{when(item.finished, data.timezone)}</td>
                <td>
                  {labels.get(item.job) ?? (item.job.startsWith("alert:") ? `資料警示 ${item.job.slice(6)}` : item.job)}
                </td>
                <td>{item.trigger === "manual" ? `立即執行（${item.requested_by ?? "—"}）` : "排程"}</td>
                <td className={item.status === "ok" ? "run-ok" : "run-failed"}>
                  {item.status === "ok" ? "成功" : "失敗"}
                </td>
                <td className="run-detail">{item.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {runs?.length === 0 && <p className="hint">還沒有執行紀錄。</p>}
    </section>
  );
}

/** 站台管理中排程相關的分頁：共用排程資料與執行紀錄的篩選。 */
export function ScheduleTabs({
  tab,
  go,
  notify,
}: {
  tab: "schedules" | "tasks" | "runs";
  go: (to: string) => void;
  notify: (notice: string | null, error: string | null) => void;
}) {
  const { data, error, reload } = useSchedules();
  const [job, setJob] = useState("");
  const run: Run = useCallback(
    async (work, done) => {
      try {
        const result = (await work()) as { warning?: string } | undefined;
        notify(result?.warning ?? done ?? null, null);
        reload();
        return true;
      } catch (e) {
        notify(null, (e as Error).message);
        return false;
      }
    },
    [notify, reload],
  );
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>載入中…</p>;
  if (tab === "schedules") return <SchedulesTab data={data} run={run} />;
  if (tab === "tasks")
    return (
      <TasksTab
        data={data}
        run={run}
        showRuns={(id) => {
          setJob(id);
          go("/admin/runs");
        }}
      />
    );
  return <RunsTab data={data} job={job} setJob={setJob} />;
}
