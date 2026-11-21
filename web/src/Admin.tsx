import { useCallback, useEffect, useMemo, useState } from "react";
import { request } from "./api";
import { ScheduleTabs } from "./AdminSchedules";
import { datasourceApi, type DatasourceItem, PublishDatasourceDialog } from "./Datasources";
import { ChecklistDialog, ConfirmDialog, PromptDialog } from "./dialogs";
import { formatTime, Modal } from "./Server";
import type { Me } from "./types";

// 站台管理：Project、報表、使用者、群組與權限（tmig_service.admin）

type Mode = "Allow" | "Deny";
interface Grantee {
  kind: "user" | "group";
  name: string;
  domain?: string | null;
}
export interface Rule {
  grantee: Grantee;
  capabilities: Record<string, Mode>;
}
interface Project {
  id: string;
  name: string;
  parent: string | null;
  owner: string | null;
  content_permissions: string;
  rules: Rule[];
  workbook_defaults: Rule[];
  path: string;
  children: number;
}
interface SiteUser {
  name: string;
  full_name: string | null;
  domain: string | null;
  email: string | null;
  site_role: string | null;
  groups: string[];
  owns: number;
}
interface Group {
  name: string;
  description: string;
  members: string[];
}
interface Report {
  id: string;
  name: string;
  project: string;
  project_path: string;
  owner: string;
  version: number;
  published_at: string;
  tableau_workbook: string | null;
  rules: Rule[];
  locked: boolean;
}
interface TableauWorkbook {
  id: string;
  name: string;
  project: string | null;
  project_path: string | null;
  owner: string | null;
  show_tabs: boolean;
  rules: Rule[];
  views: { name: string; rules: Rule[] }[];
  published: boolean;
  locked: boolean;
}
interface Site {
  source: string | null;
  imported_at: string | null;
  updated_at: string | null;
  site_roles: string[];
  workbook_capabilities: Record<string, string>;
  project_capabilities: Record<string, string>;
  content_permissions: string[];
  projects: Project[];
  users: SiteUser[];
  groups: Group[];
  reports: Report[];
  workbooks: TableauWorkbook[];
}
interface Change {
  time: string;
  user: string;
  action: string;
  target: string;
  fields?: string[];
}

const json = (body: unknown) => ({ body: JSON.stringify(body) });
const adminApi = {
  site: () => request<Site>("/api/admin/site"),
  changes: () => request<Change[]>("/api/admin/changes"),
  createProject: (body: { name: string; parent: string | null }) =>
    request<Project>("/api/admin/projects", { method: "POST", ...json(body) }),
  updateProject: (id: string, body: Record<string, unknown>) =>
    request<Project>(`/api/admin/projects/${encodeURIComponent(id)}`, {
      method: "PATCH",
      ...json(body),
    }),
  deleteProject: (id: string) =>
    request<void>(`/api/admin/projects/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  createUser: (body: Record<string, unknown>) =>
    request<SiteUser>("/api/admin/users", { method: "POST", ...json(body) }),
  updateUser: (user: SiteUser, body: Record<string, unknown>) =>
    request<SiteUser>(userPath(user), { method: "PATCH", ...json(body) }),
  deleteUser: (user: SiteUser) => request<void>(userPath(user), { method: "DELETE" }),
  createGroup: (body: { name: string; members: string[] }) =>
    request<Group>("/api/admin/groups", { method: "POST", ...json(body) }),
  updateGroup: (name: string, body: Record<string, unknown>) =>
    request<Group & { warning?: string }>(`/api/admin/groups/${encodeURIComponent(name)}`, {
      method: "PATCH",
      ...json(body),
    }),
  deleteGroup: (name: string) =>
    request<void>(`/api/admin/groups/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  updateReport: (id: string, body: Record<string, unknown>) =>
    request<Report>(`/api/admin/reports/${encodeURIComponent(id)}`, {
      method: "PATCH",
      ...json(body),
    }),
  deleteReport: (id: string) =>
    request<void>(`/api/admin/reports/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  updateWorkbook: (id: string, body: Record<string, unknown>) =>
    request<TableauWorkbook>(`/api/admin/workbooks/${encodeURIComponent(id)}`, {
      method: "PATCH",
      ...json(body),
    }),
};

function userPath(user: SiteUser): string {
  const query = user.domain ? `?domain=${encodeURIComponent(user.domain)}` : "";
  return `/api/admin/users/${encodeURIComponent(user.name)}${query}`;
}

const ROLE_NAMES: Record<string, string> = {
  ServerAdministrator: "伺服器管理員",
  SiteAdministratorCreator: "站台管理員（Creator）",
  SiteAdministratorExplorer: "站台管理員（Explorer）",
  Creator: "Creator",
  ExplorerCanPublish: "Explorer（可以發布）",
  Explorer: "Explorer",
  Viewer: "Viewer",
  Unlicensed: "未授權",
};
const CONTENT_PERMISSION_NAMES: Record<string, string> = {
  ManagedByOwner: "由擁有者管理（報表可以各自設定權限）",
  LockedToProject: "鎖定在 Project（含巢狀的 Project）",
  LockedToProjectWithoutNested: "鎖定在 Project（不含巢狀的 Project）",
};
const ACTION_NAMES: Record<string, string> = {
  import: "匯入站台資料",
  "project.create": "建立 Project",
  "project.update": "修改 Project",
  "project.delete": "刪除 Project",
  "user.create": "新增使用者",
  "user.update": "修改使用者",
  "user.delete": "刪除使用者",
  "group.create": "建立群組",
  "group.update": "修改群組",
  "group.delete": "刪除群組",
  "report.update": "修改報表",
  "report.permissions": "設定報表權限",
  "report.delete": "刪除報表",
  "workbook.update": "修改 workbook 的權限",
  "schedule.import": "匯入排程",
  "schedule.settings": "修改排程的設定（時區、同時執行的上限）",
  "schedule.create": "新增排程",
  "schedule.update": "修改排程",
  "schedule.delete": "刪除排程",
  "task.create": "新增萃取更新",
  "task.update": "修改萃取更新",
  "task.delete": "刪除萃取更新",
  "task.run": "立即執行",
  "task.suspend": "連續失敗而暫停",
  "subscription.create": "新增訂閱",
  "subscription.update": "修改訂閱",
  "subscription.delete": "刪除訂閱",
};

type Tab = "content" | "users" | "groups" | "schedules" | "tasks" | "runs" | "changes";
const TABS: [Tab, string][] = [
  ["content", "內容與權限"],
  ["users", "使用者"],
  ["groups", "群組"],
  ["schedules", "排程"],
  ["tasks", "工作"],
  ["runs", "執行紀錄"],
  ["changes", "修改紀錄"],
];

export function AdminPage({ me, tab, go }: { me: Me; tab?: string; go: (to: string) => void }) {
  const [site, setSite] = useState<Site | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const current: Tab = TABS.some(([t]) => t === tab) ? (tab as Tab) : "content";
  const reload = useCallback(() => {
    adminApi
      .site()
      .then((data) => {
        setSite(data);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(reload, [reload]);
  /** 執行一個修改：成功後重新讀取站台資料，失敗時顯示原因。 */
  const run = useCallback(
    async (work: () => Promise<unknown>, done?: string) => {
      try {
        const result = (await work()) as { warning?: string } | undefined;
        setNotice(result?.warning ?? done ?? null);
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
  const notify = useCallback((message: string | null, failure: string | null) => {
    setNotice(message);
    setError(failure);
  }, []);
  // 換分頁時清掉上一個分頁的訊息
  useEffect(() => {
    setNotice(null);
  }, [current]);
  if (!me.user?.admin) {
    return (
      <main className="home">
        <h1>站台管理</h1>
        <p className="error">站台管理只有站台管理員可以使用。</p>
      </main>
    );
  }
  return (
    <main className="home admin">
      <div className="home-header">
        <h1>站台管理</h1>
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
      {site && (
        <p className="hint">
          {site.source ? `匯入自 ${site.source}` : "新建的站台"}
          {site.updated_at ? `；最後修改 ${formatTime(site.updated_at)}` : ""}
        </p>
      )}
      <nav className="tabs" aria-label="站台管理">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={key === current ? "active" : ""}
            onClick={() => go(`/admin/${key}`)}
          >
            {label}
          </button>
        ))}
      </nav>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && <p className="success">{notice}</p>}
      {!site && !error && <p>載入中…</p>}
      {site && current === "content" && <ContentTab site={site} run={run} />}
      {site && current === "users" && <UsersTab site={site} me={me} run={run} />}
      {site && current === "groups" && <GroupsTab site={site} run={run} />}
      {(current === "schedules" || current === "tasks" || current === "runs") && (
        <ScheduleTabs tab={current} go={go} notify={notify} />
      )}
      {current === "changes" && <ChangesTab />}
    </main>
  );
}

type Run = (work: () => Promise<unknown>, done?: string) => Promise<boolean>;

// ------------------------------------------------------------ 內容與權限

type Editing =
  | { kind: "project-permissions"; project: Project }
  | { kind: "report-permissions"; report: Report }
  | { kind: "workbook-permissions"; workbook: TableauWorkbook }
  | { kind: "new-project"; parent: Project | null }
  | { kind: "rename-project"; project: Project }
  | { kind: "move-project"; project: Project }
  | { kind: "report"; report: Report }
  | { kind: "delete-project"; project: Project }
  | { kind: "delete-report"; report: Report }
  | { kind: "publish-datasource" }
  | { kind: "datasource-permissions"; datasource: DatasourceItem }
  | { kind: "datasource"; datasource: DatasourceItem }
  | { kind: "delete-datasource"; datasource: DatasourceItem };

function ContentTab({ site, run }: { site: Site; run: Run }) {
  const [editing, setEditing] = useState<Editing | null>(null);
  const close = () => setEditing(null);
  const tree = useMemo(() => projectTree(site.projects), [site.projects]);
  // 伺服器上的 Published Data Source（資料源的修改後重新讀取）
  const [datasources, setDatasources] = useState<DatasourceItem[]>([]);
  const reloadSources = useCallback(() => {
    datasourceApi
      .list()
      .then(setDatasources)
      .catch(() => setDatasources([]));
  }, []);
  useEffect(reloadSources, [reloadSources]);
  const runSource: Run = (work, done) => run(work, done).then((ok) => (ok && reloadSources(), ok));
  return (
    <section className="admin-section">
      <div className="admin-actions">
        <button type="button" className="primary" onClick={() => setEditing({ kind: "new-project", parent: null })}>
          新增 Project
        </button>
        <button type="button" onClick={() => setEditing({ kind: "publish-datasource" })}>
          發布資料源…
        </button>
      </div>
      <table className="admin-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>擁有者</th>
            <th>權限</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {tree.map(({ project, depth }) => (
            <ProjectRows
              key={project.id}
              site={site}
              project={project}
              depth={depth}
              datasources={datasources.filter((d) => d.project === project.id)}
              edit={setEditing}
            />
          ))}
        </tbody>
      </table>
      {editing?.kind === "new-project" && (
        <PromptDialog
          title={editing.parent ? `在「${editing.parent.path}」中新增 Project` : "新增 Project"}
          label="名稱"
          onClose={close}
          onSubmit={(name) =>
            run(() =>
              adminApi.createProject({
                name,
                parent: editing.parent?.id ?? null,
              }),
            )
          }
        />
      )}
      {editing?.kind === "rename-project" && (
        <PromptDialog
          title="Project 改名"
          label="名稱"
          initial={editing.project.name}
          onClose={close}
          onSubmit={(name) => run(() => adminApi.updateProject(editing.project.id, { name }))}
        />
      )}
      {editing?.kind === "move-project" && (
        <MoveProjectDialog site={site} project={editing.project} onClose={close} run={run} />
      )}
      {editing?.kind === "delete-project" && (
        <ConfirmDialog
          title="刪除 Project"
          message={`刪除「${editing.project.path}」？Project 中要先沒有報表與子 Project。`}
          onClose={close}
          onConfirm={() => run(() => adminApi.deleteProject(editing.project.id), "已刪除 Project")}
        />
      )}
      {editing?.kind === "project-permissions" && (
        <ProjectPermissionsDialog site={site} project={editing.project} onClose={close} run={run} />
      )}
      {editing?.kind === "report-permissions" && (
        <PermissionsDialog
          title={`「${editing.report.name}」的權限`}
          site={site}
          capabilities={site.workbook_capabilities}
          rules={editing.report.rules}
          locked={editing.report.locked}
          onClose={close}
          onSave={(rules) => run(() => adminApi.updateReport(editing.report.id, { rules }), "已更新權限")}
        />
      )}
      {editing?.kind === "workbook-permissions" && (
        <WorkbookPermissionsDialog site={site} workbook={editing.workbook} onClose={close} run={run} />
      )}
      {editing?.kind === "report" && <ReportDialog site={site} report={editing.report} onClose={close} run={run} />}
      {editing?.kind === "delete-report" && (
        <ConfirmDialog
          title="刪除報表"
          message={`刪除「${editing.report.name}」與它的每一版？報表會移到報表庫的 trash/，需要時可以由管理員救回。`}
          onClose={close}
          onConfirm={() => run(() => adminApi.deleteReport(editing.report.id), "已刪除報表")}
        />
      )}
      {editing?.kind === "publish-datasource" && (
        <PublishDatasourceDialog
          projects={site.projects.map((p) => ({ id: p.id, path: p.path }))}
          onClose={close}
          onDone={(item) => {
            close();
            void runSource(() => Promise.resolve(), `已發布資料源「${item.name}」第 ${item.version} 版`);
          }}
        />
      )}
      {editing?.kind === "datasource-permissions" && (
        <PermissionsDialog
          title={`資料源「${editing.datasource.name}」的權限`}
          site={site}
          capabilities={DATASOURCE_CAPABILITIES}
          rules={editing.datasource.rules as Rule[]}
          locked={false}
          onClose={close}
          onSave={(rules) => runSource(() => datasourceApi.update(editing.datasource.id, { rules }), "已更新權限")}
        />
      )}
      {editing?.kind === "datasource" && (
        <DatasourceDialog site={site} datasource={editing.datasource} onClose={close} run={runSource} />
      )}
      {editing?.kind === "delete-datasource" && (
        <ConfirmDialog
          title="刪除資料源"
          message={`刪除資料源「${editing.datasource.name}」與它的每一版？用到它的報表改用報表裡的資料。`}
          onClose={close}
          onConfirm={() => runSource(() => datasourceApi.remove(editing.datasource.id), "已刪除資料源")}
        />
      )}
    </section>
  );
}

function projectTree(projects: Project[]): { project: Project; depth: number }[] {
  const result: { project: Project; depth: number }[] = [];
  const visit = (parent: string | null, depth: number) => {
    projects
      .filter((p) => p.parent === parent)
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach((project) => {
        result.push({ project, depth });
        visit(project.id, depth + 1);
      });
  };
  visit(null, 0);
  return result;
}

function ProjectRows({
  site,
  project,
  depth,
  datasources,
  edit,
}: {
  site: Site;
  project: Project;
  depth: number;
  datasources: DatasourceItem[];
  edit: (e: Editing) => void;
}) {
  const reports = site.reports.filter((r) => r.project === project.id);
  const workbooks = site.workbooks.filter((w) => w.project === project.id && !w.published);
  const indent = { paddingLeft: `${8 + depth * 20}px` };
  return (
    <>
      <tr className="project-row" data-project={project.id}>
        <td style={indent}>
          <strong>📁 {project.name}</strong>
        </td>
        <td>{project.owner ?? "—"}</td>
        <td className="hint">{project.content_permissions === "ManagedByOwner" ? "由擁有者管理" : "鎖定"}</td>
        <td className="actions">
          <button type="button" className="link" onClick={() => edit({ kind: "project-permissions", project })}>
            權限
          </button>
          <button type="button" className="link" onClick={() => edit({ kind: "new-project", parent: project })}>
            新增子 Project
          </button>
          <button type="button" className="link" onClick={() => edit({ kind: "rename-project", project })}>
            改名
          </button>
          <button type="button" className="link" onClick={() => edit({ kind: "move-project", project })}>
            移動
          </button>
          <button type="button" className="link danger-link" onClick={() => edit({ kind: "delete-project", project })}>
            刪除
          </button>
        </td>
      </tr>
      {reports.map((report) => (
        <tr key={report.id} className="report-row" data-report={report.id}>
          <td style={{ paddingLeft: `${28 + depth * 20}px` }}>
            {report.name} <span className="hint">第 {report.version} 版</span>
          </td>
          <td>{report.owner}</td>
          <td className="hint">{report.locked ? "依 Project" : `${report.rules.length} 筆`}</td>
          <td className="actions">
            <button type="button" className="link" onClick={() => edit({ kind: "report-permissions", report })}>
              權限
            </button>
            <button type="button" className="link" onClick={() => edit({ kind: "report", report })}>
              改名／移動／擁有者
            </button>
            <button type="button" className="link danger-link" onClick={() => edit({ kind: "delete-report", report })}>
              刪除
            </button>
          </td>
        </tr>
      ))}
      {workbooks.map((workbook) => (
        <tr key={workbook.id} className="report-row" data-workbook={workbook.id}>
          <td style={{ paddingLeft: `${28 + depth * 20}px` }}>
            {workbook.name} <span className="hint">（Tableau 匯入）</span>
          </td>
          <td>{workbook.owner ?? "—"}</td>
          <td className="hint">{workbook.locked ? "依 Project" : `${workbook.rules.length} 筆`}</td>
          <td className="actions">
            <button type="button" className="link" onClick={() => edit({ kind: "workbook-permissions", workbook })}>
              權限
            </button>
          </td>
        </tr>
      ))}
      {datasources.map((datasource) => (
        <tr key={datasource.id} className="report-row" data-datasource={datasource.id}>
          <td style={{ paddingLeft: `${28 + depth * 20}px` }}>
            🗄 {datasource.name} <span className="hint">（資料源，第 {datasource.version} 版）</span>
          </td>
          <td>{datasource.owner}</td>
          <td className="hint">{datasource.rules.length} 筆</td>
          <td className="actions">
            <button type="button" className="link" onClick={() => edit({ kind: "datasource-permissions", datasource })}>
              權限
            </button>
            <button type="button" className="link" onClick={() => edit({ kind: "datasource", datasource })}>
              改名／移動／擁有者
            </button>
            <button
              type="button"
              className="link danger-link"
              onClick={() => edit({ kind: "delete-datasource", datasource })}
            >
              刪除
            </button>
          </td>
        </tr>
      ))}
    </>
  );
}

function DatasourceDialog({
  site,
  datasource,
  onClose,
  run,
}: {
  site: Site;
  datasource: DatasourceItem;
  onClose: () => void;
  run: Run;
}) {
  const [name, setName] = useState(datasource.name);
  const [project, setProject] = useState(datasource.project);
  const [owner, setOwner] = useState(datasource.owner);
  return (
    <Modal title={`資料源「${datasource.name}」`} onClose={onClose}>
      <label className="admin-field">
        名稱
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="admin-field">
        Project
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          {site.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.path}
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        擁有者
        <select value={owner} onChange={(e) => setOwner(e.target.value)}>
          {site.users.map((u) => (
            <option key={`${u.domain ?? ""}\\${u.name}`} value={u.name}>
              {u.full_name ? `${u.full_name}（${u.name}）` : u.name}
            </option>
          ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() =>
            void run(() => datasourceApi.update(datasource.id, { name, project, owner }), "已更新資料源").then(
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

function MoveProjectDialog({
  site,
  project,
  onClose,
  run,
}: {
  site: Site;
  project: Project;
  onClose: () => void;
  run: Run;
}) {
  // 不能移到自己或自己底下的 Project
  const inside = useMemo(() => {
    const ids = new Set([project.id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const p of site.projects) {
        if (p.parent && ids.has(p.parent) && !ids.has(p.id)) {
          ids.add(p.id);
          grew = true;
        }
      }
    }
    return ids;
  }, [site.projects, project.id]);
  const [parent, setParent] = useState(project.parent ?? "");
  return (
    <Modal title={`移動「${project.path}」`} onClose={onClose}>
      <label className="admin-field">
        上層 Project
        <select value={parent} onChange={(e) => setParent(e.target.value)}>
          <option value="">（最上層）</option>
          {site.projects
            .filter((p) => !inside.has(p.id))
            .map((p) => (
              <option key={p.id} value={p.id}>
                {p.path}
              </option>
            ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() => {
            void run(() =>
              adminApi.updateProject(project.id, {
                parent: parent || null,
                move: true,
              }),
            ).then((ok) => ok && onClose());
          }}
        >
          移動
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

function ReportDialog({ site, report, onClose, run }: { site: Site; report: Report; onClose: () => void; run: Run }) {
  const [name, setName] = useState(report.name);
  const [project, setProject] = useState(report.project);
  const [owner, setOwner] = useState(report.owner);
  return (
    <Modal title={`報表「${report.name}」`} onClose={onClose}>
      <label className="admin-field">
        名稱
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="admin-field">
        Project
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          {site.projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.path}
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        擁有者
        <select value={owner} onChange={(e) => setOwner(e.target.value)}>
          {site.users.map((u) => (
            <option key={`${u.domain ?? ""}\\${u.name}`} value={u.name}>
              {u.full_name ? `${u.full_name}（${u.name}）` : u.name}
            </option>
          ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() => {
            void run(() => adminApi.updateReport(report.id, { name, project, owner }), "已更新報表").then(
              (ok) => ok && onClose(),
            );
          }}
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

function ProjectPermissionsDialog({
  site,
  project,
  onClose,
  run,
}: {
  site: Site;
  project: Project;
  onClose: () => void;
  run: Run;
}) {
  const [rules, setRules] = useState(project.rules);
  const [defaults, setDefaults] = useState(project.workbook_defaults);
  const [mode, setMode] = useState(project.content_permissions);
  const [owner, setOwner] = useState(project.owner ?? "");
  return (
    <Modal title={`「${project.path}」的權限`} onClose={onClose} wide>
      <label className="admin-field">
        擁有者
        <select value={owner} onChange={(e) => setOwner(e.target.value)}>
          <option value="">（沒有）</option>
          {site.users.map((u) => (
            <option key={`${u.domain ?? ""}\\${u.name}`} value={u.name}>
              {u.name}
            </option>
          ))}
        </select>
      </label>
      <label className="admin-field">
        報表的權限
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          {site.content_permissions.map((m) => (
            <option key={m} value={m}>
              {CONTENT_PERMISSION_NAMES[m] ?? m}
            </option>
          ))}
        </select>
      </label>
      <h3>Project</h3>
      <RulesTable site={site} capabilities={site.project_capabilities} rules={rules} onChange={setRules} />
      <h3>報表的預設權限</h3>
      <p className="hint">新發布到這個 Project 的報表沿用這些權限；鎖定時 Project 中的報表一律依這些權限。</p>
      <RulesTable site={site} capabilities={site.workbook_capabilities} rules={defaults} onChange={setDefaults} />
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() => {
            const body: Record<string, unknown> = {
              rules,
              workbook_defaults: defaults,
              content_permissions: mode,
            };
            if (owner) body.owner = owner;
            void run(() => adminApi.updateProject(project.id, body), "已更新權限").then((ok) => ok && onClose());
          }}
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

function WorkbookPermissionsDialog({
  site,
  workbook,
  onClose,
  run,
}: {
  site: Site;
  workbook: TableauWorkbook;
  onClose: () => void;
  run: Run;
}) {
  const [rules, setRules] = useState(workbook.rules);
  const [views, setViews] = useState(workbook.views);
  return (
    <Modal title={`「${workbook.name}」的權限`} onClose={onClose} wide>
      {workbook.locked && <p className="hint">Project 鎖定權限：實際以 Project 的報表預設權限為準。</p>}
      <RulesTable site={site} capabilities={site.workbook_capabilities} rules={rules} onChange={setRules} />
      {!workbook.show_tabs &&
        views.map((view, index) => (
          <div key={view.name}>
            <h3>view「{view.name}」</h3>
            <RulesTable
              site={site}
              capabilities={{ Read: "檢視" }}
              rules={view.rules}
              onChange={(next) => setViews(views.map((v, i) => (i === index ? { ...v, rules: next } : v)))}
            />
          </div>
        ))}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() => {
            const body: Record<string, unknown> = { rules };
            if (!workbook.show_tabs) body.views = views.map((v) => ({ name: v.name, rules: v.rules }));
            void run(() => adminApi.updateWorkbook(workbook.id, body), "已更新權限").then((ok) => ok && onClose());
          }}
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

function PermissionsDialog({
  title,
  site,
  capabilities,
  rules,
  locked,
  onClose,
  onSave,
}: {
  title: string;
  site: Site;
  capabilities: Record<string, string>;
  rules: Rule[];
  locked: boolean;
  onClose: () => void;
  onSave: (rules: Rule[]) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState(rules);
  return (
    <Modal title={title} onClose={onClose} wide>
      {locked ? (
        <p className="hint">這個 Project 鎖定權限：報表的權限要在 Project 上設定（報表的預設權限）。</p>
      ) : (
        <>
          <RulesTable site={site} capabilities={capabilities} rules={draft} onChange={setDraft} />
          <div className="dialog-actions">
            <button type="button" className="primary" onClick={() => void onSave(draft).then((ok) => ok && onClose())}>
              儲存
            </button>
            <button type="button" onClick={onClose}>
              取消
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

// 權限範本（與 Tableau 2021.2 相同；沒有網頁編輯、移動等這裡沒有的能力）
const VIEW = ["Read", "Filter", "ViewComments", "AddComment", "ExportImage", "ExportData"];
const EXPLORE = [...VIEW, "ViewUnderlyingData", "ShareView", "ExportXml"];
const PUBLISH = [...EXPLORE, "Write"];
const WORKBOOK_TEMPLATES: [string, string[]][] = [
  ["檢視", VIEW],
  ["探索", EXPLORE],
  ["發布", PUBLISH],
  ["管理", [...PUBLISH, "Delete", "ChangePermissions"]],
];
const PROJECT_TEMPLATES: [string, string[]][] = [
  ["檢視", ["Read"]],
  ["發布", ["Read", "Write"]],
];
const DATASOURCE_TEMPLATES: [string, string[]][] = [
  ["檢視", ["Read", "Connect"]],
  ["探索", ["Read", "Connect", "ExportXml"]],
  ["發布", ["Read", "Connect", "ExportXml", "Write"]],
  ["管理", ["Read", "Connect", "ExportXml", "Write", "Delete", "ChangePermissions"]],
];
const DATASOURCE_CAPABILITIES: Record<string, string> = {
  Read: "檢視",
  Connect: "連線",
  ExportXml: "下載",
  Write: "覆蓋",
  Delete: "刪除",
  ChangePermissions: "設定權限",
};

/** 範本控制的能力：報表為全部；Project 為檢視與發布（Project Leader 另外設定）。 */
function templatesFor(
  capabilities: Record<string, string>,
): { domain: string[]; templates: [string, string[]][] } | null {
  if ("Connect" in capabilities) return { domain: Object.keys(capabilities), templates: DATASOURCE_TEMPLATES };
  if ("ChangePermissions" in capabilities) return { domain: Object.keys(capabilities), templates: WORKBOOK_TEMPLATES };
  if ("ProjectLeader" in capabilities) return { domain: ["Read", "Write"], templates: PROJECT_TEMPLATES };
  return null;
}

function templateOf(rule: Rule, domain: string[], templates: [string, string[]][]): string {
  const set = domain.filter((c) => rule.capabilities[c]);
  if (set.length === 0) return "無";
  if (domain.every((c) => rule.capabilities[c] === "Deny")) return "拒絕";
  for (const [name, allow] of templates) {
    if (set.length === allow.length && allow.every((c) => rule.capabilities[c] === "Allow")) return name;
  }
  return "自訂";
}

function applyTemplate(rule: Rule, name: string, domain: string[], templates: [string, string[]][]): Rule {
  const next: Record<string, Mode> = {};
  for (const [capability, mode] of Object.entries(rule.capabilities)) {
    if (!domain.includes(capability)) next[capability] = mode;
  }
  if (name === "拒絕") for (const c of domain) next[c] = "Deny";
  const allow = templates.find(([n]) => n === name)?.[1] ?? [];
  for (const c of allow) next[c] = "Allow";
  return { ...rule, capabilities: next };
}

function sameGrantee(a: Grantee, b: Grantee): boolean {
  return (
    a.kind === b.kind &&
    a.name.toLowerCase() === b.name.toLowerCase() &&
    (a.domain ?? "").toLowerCase() === (b.domain ?? "").toLowerCase()
  );
}

function granteeLabel(grantee: Grantee): string {
  if (grantee.kind === "group") return `群組：${grantee.name}`;
  return `使用者：${grantee.domain ? `${grantee.domain}\\${grantee.name}` : grantee.name}`;
}

/** 權限表：每列一個使用者或群組，每欄一個能力（未設定、允許、拒絕）；報表與 Project 可以套用範本。 */
function RulesTable({
  site,
  capabilities,
  rules,
  onChange,
}: {
  site: Site;
  capabilities: Record<string, string>;
  rules: Rule[];
  onChange: (rules: Rule[]) => void;
}) {
  const [adding, setAdding] = useState("");
  const template = templatesFor(capabilities);
  // 同名的使用者在多個網域時才寫網域
  const names = new Map<string, number>();
  for (const u of site.users) names.set(u.name.toLowerCase(), (names.get(u.name.toLowerCase()) ?? 0) + 1);
  const choices: { grantee: Grantee; label: string }[] = [
    ...["All Users", ...site.groups.map((g) => g.name)].map((name) => ({
      grantee: { kind: "group" as const, name },
      label: `群組：${name}`,
    })),
    ...site.users.map((u) => {
      const grantee: Grantee = { kind: "user", name: u.name };
      if ((names.get(u.name.toLowerCase()) ?? 0) > 1) grantee.domain = u.domain;
      return { grantee, label: `${granteeLabel(grantee)}${u.full_name ? `（${u.full_name}）` : ""}` };
    }),
  ].filter((c) => !rules.some((r) => sameGrantee(r.grantee, c.grantee)));
  const update = (index: number, rule: Rule) => onChange(rules.map((r, i) => (i === index ? rule : r)));
  const set = (index: number, capability: string, value: string) => {
    const next = { ...rules[index].capabilities };
    if (value === "") delete next[capability];
    else next[capability] = value as Mode;
    update(index, { ...rules[index], capabilities: next });
  };
  const columns = Object.keys(capabilities).length + (template ? 3 : 2);
  return (
    <table className="admin-table permissions">
      <thead>
        <tr>
          <th>對象</th>
          {template && <th>範本</th>}
          {Object.entries(capabilities).map(([key, label]) => (
            <th key={key} title={key}>
              {label}
            </th>
          ))}
          <th />
        </tr>
      </thead>
      <tbody>
        {rules.map((rule, index) => (
          <tr key={`${rule.grantee.kind}:${rule.grantee.domain ?? ""}:${rule.grantee.name}`}>
            <td className="grantee">{granteeLabel(rule.grantee)}</td>
            {template && (
              <td>
                <select
                  aria-label={`${rule.grantee.name} 範本`}
                  value={templateOf(rule, template.domain, template.templates)}
                  onChange={(e) =>
                    update(index, applyTemplate(rule, e.target.value, template.domain, template.templates))
                  }
                >
                  <option value="無">無</option>
                  {template.templates.map(([name]) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                  <option value="拒絕">拒絕</option>
                  <option value="自訂" disabled>
                    自訂
                  </option>
                </select>
              </td>
            )}
            {Object.keys(capabilities).map((capability) => (
              <td key={capability}>
                <select
                  aria-label={`${rule.grantee.name} ${capabilities[capability]}`}
                  value={rule.capabilities[capability] ?? ""}
                  onChange={(e) => set(index, capability, e.target.value)}
                  className={
                    rule.capabilities[capability] === "Deny" ? "deny" : rule.capabilities[capability] ? "allow" : ""
                  }
                >
                  <option value="">—</option>
                  <option value="Allow">允許</option>
                  <option value="Deny">拒絕</option>
                </select>
              </td>
            ))}
            <td>
              <button
                type="button"
                className="link danger-link"
                onClick={() => onChange(rules.filter((_, i) => i !== index))}
              >
                移除
              </button>
            </td>
          </tr>
        ))}
        <tr>
          <td colSpan={columns} className="grantee">
            <select aria-label="新增對象" value={adding} onChange={(e) => setAdding(e.target.value)}>
              <option value="">新增使用者或群組…</option>
              {choices.map((c, i) => (
                <option key={c.label} value={String(i)}>
                  {c.label}
                </option>
              ))}
            </select>{" "}
            <button
              type="button"
              disabled={!adding}
              onClick={() => {
                const choice = choices[Number(adding)];
                if (choice) onChange([...rules, { grantee: choice.grantee, capabilities: {} }]);
                setAdding("");
              }}
            >
              新增
            </button>
            {rules.length === 0 && (
              <span className="hint">（沒有設定：只有管理員、擁有者與 Project Leader 可以使用）</span>
            )}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------ 使用者

function UsersTab({ site, me, run }: { site: Site; me: Me; run: Run }) {
  const [adding, setAdding] = useState(false);
  const [groupsOf, setGroupsOf] = useState<SiteUser | null>(null);
  const [removing, setRemoving] = useState<SiteUser | null>(null);
  const [filter, setFilter] = useState("");
  const shown = site.users.filter((u) =>
    `${u.name} ${u.full_name ?? ""} ${u.email ?? ""}`.toLowerCase().includes(filter.trim().toLowerCase()),
  );
  return (
    <section className="admin-section">
      <div className="admin-actions">
        <button type="button" className="primary" onClick={() => setAdding(true)}>
          新增使用者
        </button>
        <input
          type="search"
          placeholder="搜尋使用者"
          aria-label="搜尋使用者"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="hint">共 {site.users.length} 位</span>
      </div>
      <table className="admin-table">
        <thead>
          <tr>
            <th>使用者名稱</th>
            <th>名稱</th>
            <th>email</th>
            <th>站台角色</th>
            <th>群組</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {shown.map((user) => (
            <tr key={`${user.domain ?? ""}\\${user.name}`} data-user={user.name}>
              <td>{user.domain && user.domain !== "local" ? `${user.domain}\\${user.name}` : user.name}</td>
              <td>{user.full_name ?? ""}</td>
              <td>{user.email ?? ""}</td>
              <td>
                <select
                  aria-label={`${user.name} 的站台角色`}
                  value={user.site_role ?? ""}
                  disabled={user.name === me.user?.username}
                  onChange={(e) =>
                    void run(
                      () =>
                        adminApi.updateUser(user, {
                          site_role: e.target.value,
                        }),
                      "已更新站台角色",
                    )
                  }
                >
                  {!site.site_roles.includes(user.site_role ?? "") && (
                    <option value={user.site_role ?? ""}>{user.site_role ?? "—"}</option>
                  )}
                  {site.site_roles.map((role) => (
                    <option key={role} value={role}>
                      {ROLE_NAMES[role] ?? role}
                    </option>
                  ))}
                </select>
              </td>
              <td>{user.groups.filter((g) => g !== "All Users").join("、")}</td>
              <td className="actions">
                <button type="button" className="link" onClick={() => setGroupsOf(user)}>
                  群組
                </button>
                <button
                  type="button"
                  className="link danger-link"
                  disabled={user.name === me.user?.username}
                  onClick={() => setRemoving(user)}
                >
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {adding && <NewUserDialog site={site} onClose={() => setAdding(false)} run={run} />}
      {groupsOf && (
        <ChecklistDialog
          title={`「${groupsOf.name}」的群組`}
          options={site.groups.map((g) => g.name)}
          checked={groupsOf.groups}
          onClose={() => setGroupsOf(null)}
          onSave={(groups) => run(() => adminApi.updateUser(groupsOf, { groups }), "已更新群組")}
        />
      )}
      {removing && (
        <ConfirmDialog
          title="刪除使用者"
          message={
            removing.owns > 0
              ? `「${removing.name}」還擁有 ${removing.owns} 項內容，要先換擁有者才能刪除。`
              : `刪除「${removing.name}」？刪除後不能再登入，權限設定中的這位使用者也一併移除。`
          }
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => adminApi.deleteUser(removing), "已刪除使用者")}
        />
      )}
    </section>
  );
}

function NewUserDialog({ site, onClose, run }: { site: Site; onClose: () => void; run: Run }) {
  const [name, setName] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [domain, setDomain] = useState("");
  const [role, setRole] = useState("Viewer");
  return (
    <Modal title="新增使用者" onClose={onClose}>
      <p className="hint">使用者名稱是登入的帳號，也是報表公式 USERNAME() 的值；AD 帳號另外填網域。</p>
      <label className="admin-field">
        使用者名稱
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="admin-field">
        名稱
        <input value={fullName} onChange={(e) => setFullName(e.target.value)} />
      </label>
      <label className="admin-field">
        email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label className="admin-field">
        網域
        <input value={domain} placeholder="（本機帳號不用填）" onChange={(e) => setDomain(e.target.value)} />
      </label>
      <label className="admin-field">
        站台角色
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          {site.site_roles.map((r) => (
            <option key={r} value={r}>
              {ROLE_NAMES[r] ?? r}
            </option>
          ))}
        </select>
      </label>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={!name.trim()}
          onClick={() => {
            const body = {
              name: name.trim(),
              full_name: fullName || null,
              email: email || null,
              domain: domain || null,
              site_role: role,
            };
            void run(() => adminApi.createUser(body), "已新增使用者").then((ok) => ok && onClose());
          }}
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

// ------------------------------------------------------------ 群組

function GroupsTab({ site, run }: { site: Site; run: Run }) {
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Group | null>(null);
  const [members, setMembers] = useState<Group | null>(null);
  const [removing, setRemoving] = useState<Group | null>(null);
  return (
    <section className="admin-section">
      <div className="admin-actions">
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          新增群組
        </button>
        <span className="hint">群組名稱就是報表公式 ISMEMBEROF() 的引數；每個人都在「All Users」群組中。</span>
      </div>
      <table className="admin-table">
        <thead>
          <tr>
            <th>群組</th>
            <th>成員</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {site.groups.map((group) => (
            <tr key={group.name} data-group={group.name}>
              <td>{group.name}</td>
              <td>
                {group.members.length} 位
                <span className="hint">
                  （{group.members.slice(0, 5).join("、")}
                  {group.members.length > 5 ? "…" : ""}）
                </span>
              </td>
              <td className="actions">
                <button type="button" className="link" onClick={() => setMembers(group)}>
                  成員
                </button>
                <button type="button" className="link" onClick={() => setRenaming(group)}>
                  改名
                </button>
                <button type="button" className="link danger-link" onClick={() => setRemoving(group)}>
                  刪除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {creating && (
        <PromptDialog
          title="新增群組"
          label="名稱"
          onClose={() => setCreating(false)}
          onSubmit={(name) => run(() => adminApi.createGroup({ name, members: [] }), "已新增群組")}
        />
      )}
      {renaming && (
        <PromptDialog
          title={`群組「${renaming.name}」改名`}
          label="名稱"
          initial={renaming.name}
          hint="報表公式中的 ISMEMBEROF 以群組名稱判斷：改名後用到舊名稱的列層級權限要一併修改。"
          onClose={() => setRenaming(null)}
          onSubmit={(name) => run(() => adminApi.updateGroup(renaming.name, { name }))}
        />
      )}
      {members && (
        <ChecklistDialog
          title={`「${members.name}」的成員`}
          options={site.users.map((u) => u.name)}
          checked={members.members}
          onClose={() => setMembers(null)}
          onSave={(list) => run(() => adminApi.updateGroup(members.name, { members: list }), "已更新成員")}
        />
      )}
      {removing && (
        <ConfirmDialog
          title="刪除群組"
          message={`刪除群組「${removing.name}」？成員不會被刪除，權限設定中的這個群組一併移除。`}
          onClose={() => setRemoving(null)}
          onConfirm={() => run(() => adminApi.deleteGroup(removing.name), "已刪除群組")}
        />
      )}
    </section>
  );
}

// ------------------------------------------------------------ 修改紀錄

function ChangesTab() {
  const [changes, setChanges] = useState<Change[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    adminApi
      .changes()
      .then(setChanges)
      .catch((e: Error) => setError(e.message));
  }, []);
  if (error) return <p className="error">{error}</p>;
  if (!changes) return <p>載入中…</p>;
  return (
    <section className="admin-section">
      <table className="admin-table">
        <thead>
          <tr>
            <th>時間</th>
            <th>修改的人</th>
            <th>動作</th>
            <th>對象</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change, index) => (
            <tr key={index}>
              <td>{formatTime(change.time)}</td>
              <td>{change.user}</td>
              <td>
                {ACTION_NAMES[change.action] ?? change.action}
                {change.fields?.length ? <span className="hint">（{change.fields.join("、")}）</span> : null}
              </td>
              <td>{change.target}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {changes.length === 0 && <p className="hint">還沒有修改紀錄。</p>}
    </section>
  );
}
