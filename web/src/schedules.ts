import { request } from "./api";

// 排程、萃取更新工作與訂閱（tmig_service.schedule_api）

export type FrequencyKind = "hourly" | "daily" | "weekly" | "monthly";
export interface Frequency {
  kind: FrequencyKind;
  start: string;
  end?: string | null;
  every_hours?: number | null;
  every_minutes?: number | null;
  weekdays: string[];
  month_days: (number | "LastDay")[];
  month_weekday?: string | null;
}
export interface ScheduleItem {
  id: string;
  name: string;
  kind: string | null;
  state: string;
  priority: number | null;
  frequency: Frequency | null;
  problem: string | null;
  description: string | null;
  next_run: string | null;
  tasks: number;
  subscriptions: number;
}
export interface RunItem {
  job: string;
  kind: string;
  due: string;
  started: string;
  finished: string;
  status: "ok" | "failed";
  detail: string;
  trigger?: string;
  requested_by?: string | null;
}
export interface ExtractTaskItem {
  id: string;
  job: string;
  label: string;
  schedule: string;
  schedule_name: string | null;
  refresh: "full" | "incremental";
  priority: number | null;
  consecutive_failures: number;
  suspended: boolean;
  report: string | null;
  reports: string[];
  targets: string[];
  problem: string | null;
  next_run: string | null;
  last_run: RunItem | null;
}
export interface SubscriptionItem {
  id: string;
  job: string;
  subject: string;
  message: string | null;
  user: string;
  schedule: string;
  schedule_name: string | null;
  content_kind: "view" | "workbook";
  view: string | null;
  report_id: string | null;
  report_name: string | null;
  attach_image: boolean;
  attach_pdf: boolean;
  page_size: string | null;
  page_orientation: string | null;
  send_if_view_empty: boolean;
  suspended: boolean;
  consecutive_failures: number;
  problem: string | null;
  next_run: string | null;
  last_run: RunItem | null;
}
export interface SchedulerStatus {
  running: boolean;
  heartbeat_at: string | null;
  started_at?: string;
  queued: { id: string; job: string; user: string; requested_at: string }[];
}
export interface SchedulableReport {
  id: string;
  name: string;
  published: boolean;
  datasources: { id: string; caption: string }[];
  views: string[];
}
export interface ScheduleOverview {
  timezone: string | null;
  source: string | null;
  scheduler: SchedulerStatus;
  suspend_after: number;
  schedules: ScheduleItem[];
  extract_tasks: ExtractTaskItem[];
  subscriptions: SubscriptionItem[];
  reports: SchedulableReport[];
}
export interface SubscriptionOptions {
  /** 登入者 */
  username: string;
  schedules: ScheduleItem[];
  timezone: string | null;
  email: string | null;
  can_subscribe_others: boolean;
  users: string[];
}

const json = (method: string, body: unknown) => ({ method, body: JSON.stringify(body) });

export const scheduleApi = {
  overview: () => request<ScheduleOverview>("/api/admin/schedules"),
  settings: (timezone: string) =>
    request<{ timezone: string }>("/api/admin/schedule-settings", json("PATCH", { timezone })),
  createSchedule: (body: Record<string, unknown>) => request<ScheduleItem>("/api/admin/schedules", json("POST", body)),
  updateSchedule: (id: string, body: Record<string, unknown>) =>
    request<ScheduleItem>(`/api/admin/schedules/${encodeURIComponent(id)}`, json("PATCH", body)),
  deleteSchedule: (id: string) => request<void>(`/api/admin/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }),
  createTask: (body: Record<string, unknown>) =>
    request<ExtractTaskItem>("/api/admin/extract-tasks", json("POST", body)),
  updateTask: (id: string, body: Record<string, unknown>) =>
    request<ExtractTaskItem>(`/api/admin/extract-tasks/${encodeURIComponent(id)}`, json("PATCH", body)),
  deleteTask: (id: string) => request<void>(`/api/admin/extract-tasks/${encodeURIComponent(id)}`, { method: "DELETE" }),
  run: (job: string) =>
    request<{ queued: string; scheduler: SchedulerStatus }>(`/api/admin/jobs/${encodeURIComponent(job)}/run`, {
      method: "POST",
    }),
  runs: (job?: string) => request<RunItem[]>(`/api/admin/runs${job ? `?job=${encodeURIComponent(job)}` : ""}`),
  options: (report: string) =>
    request<SubscriptionOptions>(`/api/subscription-options?report=${encodeURIComponent(report)}`),
  mine: () =>
    request<{ timezone: string | null; schedules: ScheduleItem[]; subscriptions: SubscriptionItem[] }>(
      "/api/subscriptions",
    ),
  subscribe: (body: Record<string, unknown>) =>
    request<{ subscriptions: SubscriptionItem[] }>("/api/subscriptions", json("POST", body)),
  updateSubscription: (id: string, body: Record<string, unknown>) =>
    request<SubscriptionItem>(`/api/subscriptions/${encodeURIComponent(id)}`, json("PATCH", body)),
  unsubscribe: (id: string) => request<void>(`/api/subscriptions/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/** 排程時區的日期與時間（例如 2026/11/12 06:00）。 */
export function when(iso: string | null | undefined, timezone: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat("zh-TW", {
      timeZone: timezone || undefined,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

export const WEEKDAYS: [string, string][] = [
  ["Monday", "一"],
  ["Tuesday", "二"],
  ["Wednesday", "三"],
  ["Thursday", "四"],
  ["Friday", "五"],
  ["Saturday", "六"],
  ["Sunday", "日"],
];
export const ORDINALS: [string, string][] = [
  ["First", "第一個"],
  ["Second", "第二個"],
  ["Third", "第三個"],
  ["Fourth", "第四個"],
  ["Fifth", "第五個"],
  ["Last", "最後一個"],
];
export const PAPER_SIZES = ["A4", "A3", "A5", "Letter", "Legal", "Ledger", "Tabloid"];
