import { useEffect, useMemo, useState } from "react";
import { confirmBox } from "../confirm";
import { toast, workbookChanged } from "../desktop";
import { Modal } from "../Server";
import {
  connectionsApi,
  datasourceApi,
  type ConnectionEntry,
  type Connector,
  type InspectedColumn,
  type RemoteTable,
} from "./datasourceApi";

// 桌面版的「連線到伺服器」（Tableau Desktop 的「連線＞到伺服器」）：選擇資料庫的類型，輸入伺服器、資料庫與
// 帳號登入，再選擇資料表或輸入自訂 SQL，建立新的報表（workbook 為 null）或報表的新資料源。
// 密碼只送到本機引擎：在引擎的記憶體中，勾選「記住密碼」時存在作業系統的認證管理員；設定檔與報表都不含密碼。
// 已建立的連線列在上面，可以直接使用、重新登入或刪除。

type Go = (to: string) => void;
/** 選好的資料：資料表（限定名稱的各段）或自訂 SQL。 */
export type RemoteSource = { table_path: string[] } | { sql: string };

const TYPES: Record<string, string> = {
  string: "字串",
  integer: "整數",
  real: "小數",
  date: "日期",
  datetime: "日期時間",
  boolean: "布林",
};

export function describeConnection(c: Pick<ConnectionEntry, "label" | "server" | "database" | "schema">): string {
  return [c.label, c.server, c.database, c.schema].filter(Boolean).join("／");
}

export function ConnectServerDialog({ workbook, onClose, go }: { workbook: string | null; onClose: () => void; go: Go }) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [connections, setConnections] = useState<ConnectionEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<{ connection: ConnectionEntry; tables: RemoteTable[] } | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = () =>
    connectionsApi
      .list()
      .then(setConnections)
      .catch((e: Error) => setError(e.message));
  useEffect(() => {
    connectionsApi
      .connectors()
      .then(setConnectors)
      .catch((e: Error) => setError(e.message));
    void reload();
  }, []);

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    try {
      await task();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const use = (connection: ConnectionEntry) =>
    run(async () => {
      const tables = await connectionsApi.tables(connection.id);
      setChosen({ connection, tables });
    });

  const create = (source: RemoteSource) =>
    run(async () => {
      if (!chosen) return;
      const id = chosen.connection.id;
      if (workbook === null) {
        const result = await connectionsApi.newWorkbook(id, source);
        onClose();
        go(`/w/${encodeURIComponent(result.document.id)}/edit-ds/${encodeURIComponent(result.datasource)}`);
        return;
      }
      const result = await datasourceApi.addLiveDatasource(workbook, id, source);
      workbookChanged(workbook);
      onClose();
      go(`/w/${encodeURIComponent(workbook)}/edit-ds/${encodeURIComponent(result.datasource)}`);
    });

  const title = workbook === null ? "新增報表：連線到伺服器" : "新增資料源：連線到伺服器";
  return (
    <Modal title={title} onClose={onClose} wide>
      <div className="connect-server" data-busy={busy ? "true" : "false"}>
        {error && <p className="error">{error}</p>}
        {chosen ? (
          <RemotePicker
            connection={chosen.connection}
            initial={chosen.tables}
            busy={busy}
            onBack={() => setChosen(null)}
            onChoose={(source) => void create(source)}
          />
        ) : (
          <>
            {connections.length > 0 && (
              <section className="cs-saved" aria-label="已建立的連線">
                <h3>已建立的連線</h3>
                <ul>
                  {connections.map((c) => (
                    <SavedConnection
                      key={c.id}
                      connection={c}
                      busy={busy}
                      onUse={() => void use(c)}
                      onSignIn={(password, remember) =>
                        void run(async () => {
                          const { connection } = await connectionsApi.signIn(c.id, password, remember);
                          await reload();
                          const tables = await connectionsApi.tables(connection.id);
                          setChosen({ connection, tables });
                        })
                      }
                      onRemove={() =>
                        void run(async () => {
                          if (!(await confirmBox(`刪除連線「${describeConnection(c)}」？（記住的密碼一併刪除；已建立的報表不受影響）`))) return;
                          await connectionsApi.remove(c.id);
                          await reload();
                        })
                      }
                    />
                  ))}
                </ul>
              </section>
            )}
            {connectors && (
              <NewConnection
                connectors={connectors}
                busy={busy}
                onConnect={(body) =>
                  void run(async () => {
                    const { connection, tables } = await connectionsApi.connect(body);
                    await reload();
                    setChosen({ connection, tables });
                  })
                }
              />
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

/** 資料源分頁的「加入連線＞資料庫」：選擇（或建立、登入）一個資料庫的連線，之後在資料源中加入它的資料表
 * （跨資料庫的 join）。 */
export function PickConnectionDialog({
  onClose,
  onPick,
}: {
  onClose: () => void;
  onPick: (connection: ConnectionEntry) => void;
}) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [connections, setConnections] = useState<ConnectionEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = () =>
    connectionsApi
      .list()
      .then(setConnections)
      .catch((e: Error) => setError(e.message));
  useEffect(() => {
    connectionsApi
      .connectors()
      .then(setConnectors)
      .catch((e: Error) => setError(e.message));
    void reload();
  }, []);
  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    try {
      await task();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const pick = (connection: ConnectionEntry) => {
    onPick(connection);
    onClose();
  };
  return (
    <Modal title="加入連線：資料庫" onClose={onClose} wide>
      <div className="connect-server" data-busy={busy ? "true" : "false"}>
        {error && <p className="error">{error}</p>}
        <p className="hint">不同連線的資料表在本機合併（跨資料庫的 join），與 Tableau 相同；資料庫的資料在查詢時取回。</p>
        {connections.length > 0 && (
          <section className="cs-saved" aria-label="已建立的連線">
            <h3>已建立的連線</h3>
            <ul>
              {connections.map((c) => (
                <SavedConnection
                  key={c.id}
                  connection={c}
                  busy={busy}
                  onUse={() => pick(c)}
                  onSignIn={(password, remember) =>
                    void run(async () => {
                      const { connection } = await connectionsApi.signIn(c.id, password, remember);
                      pick(connection);
                    })
                  }
                  onRemove={() =>
                    void run(async () => {
                      if (!(await confirmBox(`刪除連線「${describeConnection(c)}」？（記住的密碼一併刪除；已建立的報表不受影響）`))) return;
                      await connectionsApi.remove(c.id);
                      await reload();
                    })
                  }
                />
              ))}
            </ul>
          </section>
        )}
        {connectors && (
          <NewConnection
            connectors={connectors}
            busy={busy}
            onConnect={(body) =>
              void run(async () => {
                const { connection } = await connectionsApi.connect(body);
                pick(connection);
              })
            }
          />
        )}
      </div>
    </Modal>
  );
}

function SavedConnection({
  connection,
  busy,
  onUse,
  onSignIn,
  onRemove,
}: {
  connection: ConnectionEntry;
  busy: boolean;
  onUse: () => void;
  onSignIn: (password: string, remember: boolean) => void;
  onRemove: () => void;
}) {
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  return (
    <li className="cs-connection" data-connection={connection.id}>
      <div className="cs-connection-name">
        <strong>{connection.label}</strong> {[connection.server, connection.database, connection.schema].filter(Boolean).join("／")}
        {connection.user && <span className="muted">（{connection.user}）</span>}
      </div>
      {connection.signed_in ? (
        <button type="button" className="primary" disabled={busy} onClick={onUse}>
          使用
        </button>
      ) : (
        <form
          className="cs-sign-in"
          onSubmit={(e) => {
            e.preventDefault();
            onSignIn(password, remember);
          }}
        >
          <input
            type="password"
            aria-label={`${connection.id} 的密碼`}
            placeholder="密碼"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <label>
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> 記住密碼
          </label>
          <button type="submit" disabled={busy || !password}>
            登入
          </button>
        </form>
      )}
      <button type="button" className="link danger-link" disabled={busy} onClick={onRemove}>
        刪除
      </button>
    </li>
  );
}

function NewConnection({
  connectors,
  busy,
  onConnect,
}: {
  connectors: Connector[];
  busy: boolean;
  onConnect: (body: Parameters<typeof connectionsApi.connect>[0]) => void;
}) {
  const [dbclass, setDbclass] = useState(connectors[0]?.dbclass ?? "");
  const connector = connectors.find((c) => c.dbclass === dbclass);
  const [server, setServer] = useState("");
  const [port, setPort] = useState("");
  const [database, setDatabase] = useState("");
  const [schema, setSchema] = useState("");
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [useEnv, setUseEnv] = useState(false);
  const [passwordEnv, setPasswordEnv] = useState("");
  const [extras, setExtras] = useState<Record<string, string>>({});
  if (!connector) return null;
  const secretLabel = connector.secret === "access_token" ? "存取權杖" : "密碼";
  const serverOptional = connector.dbclass === "bigquery" || connector.dbclass === "athena";
  return (
    <form
      className="cs-new"
      aria-label="新的連線"
      onSubmit={(e) => {
        e.preventDefault();
        onConnect({
          dbclass,
          server: server.trim() || null,
          port: port.trim() ? Number(port) : null,
          database: database.trim() || null,
          schema: schema.trim() || null,
          user: user.trim() || null,
          password: connector.secret && !useEnv ? password : null,
          password_env: connector.secret && useEnv ? passwordEnv.trim() || null : null,
          remember,
          extras,
        });
      }}
    >
      <h3>新的連線</h3>
      <label className="admin-field">
        資料庫的類型
        <select aria-label="資料庫的類型" value={dbclass} onChange={(e) => setDbclass(e.target.value)}>
          {connectors.map((c) => (
            <option key={c.dbclass} value={c.dbclass}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <div className="cs-grid">
        <label className="admin-field">
          伺服器{serverOptional ? "（選用）" : ""}
          <input aria-label="伺服器" value={server} onChange={(e) => setServer(e.target.value)} />
        </label>
        <label className="admin-field">
          連接埠
          <input
            aria-label="連接埠"
            inputMode="numeric"
            placeholder={connector.port ? String(connector.port) : ""}
            value={port}
            onChange={(e) => setPort(e.target.value.replace(/[^0-9]/g, ""))}
          />
        </label>
        {connector.database && (
          <label className="admin-field">
            {connector.database}
            <input aria-label={connector.database} value={database} onChange={(e) => setDatabase(e.target.value)} />
          </label>
        )}
        {connector.schema && (
          <label className="admin-field">
            結構描述（選用）
            <input aria-label="結構描述" value={schema} onChange={(e) => setSchema(e.target.value)} />
          </label>
        )}
        {connector.extras.map((x) => (
          <label key={x.key} className="admin-field">
            {x.label}
            <input
              aria-label={x.label}
              value={extras[x.key] ?? ""}
              onChange={(e) => setExtras((all) => ({ ...all, [x.key]: e.target.value }))}
            />
          </label>
        ))}
        {connector.secret !== "" && (
          <label className="admin-field">
            使用者名稱
            <input aria-label="使用者名稱" autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} />
          </label>
        )}
        {connector.secret !== "" && !useEnv && (
          <label className="admin-field">
            {secretLabel}
            <input
              type="password"
              aria-label={secretLabel}
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        )}
        {connector.secret !== "" && useEnv && (
          <label className="admin-field">
            放{secretLabel}的環境變數
            <input aria-label="環境變數" placeholder="例如 SALES_DB_PASSWORD" value={passwordEnv} onChange={(e) => setPasswordEnv(e.target.value)} />
          </label>
        )}
      </div>
      {connector.secret !== "" && (
        <div className="cs-options">
          {!useEnv && (
            <label>
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> 記住{secretLabel}（存在作業系統的認證管理員）
            </label>
          )}
          <label>
            <input type="checkbox" checked={useEnv} onChange={(e) => setUseEnv(e.target.checked)} /> 改用環境變數中的{secretLabel}
          </label>
        </div>
      )}
      {connector.secret === "" && <p className="hint">以這台電腦上的雲端預設憑證登入（不需要密碼）。</p>}
      <p className="hint">{secretLabel === "密碼" ? "密碼" : "權杖"}不會存進報表或設定檔；建議使用唯讀的帳號。</p>
      <div className="dialog-actions">
        <button type="submit" className="primary" disabled={busy || (!serverOptional && !server.trim())}>
          登入
        </button>
      </div>
    </form>
  );
}

/** 選擇資料表或輸入自訂 SQL（連線成功之後）。 */
export function RemotePicker({
  connection,
  initial,
  busy,
  onBack,
  onChoose,
  load,
  columns,
  submitLabel = "確定",
}: {
  connection: Pick<ConnectionEntry, "id" | "label" | "server" | "database" | "schema">;
  initial: RemoteTable[] | null;
  busy: boolean;
  onBack?: () => void;
  onChoose: (source: RemoteSource) => void;
  /** 列出資料表（預設以連線代號查詢；資料源分頁改以資料源查詢） */
  load?: (schema: string | null) => Promise<RemoteTable[]>;
  /** 檢查自訂 SQL 的欄位 */
  columns?: (sql: string) => Promise<InspectedColumn[]>;
  submitLabel?: string;
}) {
  const [tables, setTables] = useState<RemoteTable[] | null>(initial);
  const [schema, setSchema] = useState(connection.schema ?? "");
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<"table" | "sql">("table");
  const [picked, setPicked] = useState<string | null>(initial?.[0] ? initial[0].path.join(".") : null);
  const [sql, setSql] = useState("");
  const [checked, setChecked] = useState<InspectedColumn[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const list = load ?? ((s: string | null) => connectionsApi.tables(connection.id, s));
  const check = columns ?? ((text: string) => connectionsApi.columns(connection.id, { sql: text }));

  const refresh = () => {
    setLoadError(null);
    list(schema.trim() || null)
      .then((found) => {
        setTables(found);
        setPicked(found[0] ? found[0].path.join(".") : null);
      })
      .catch((e: Error) => setLoadError(e.message));
  };
  useEffect(() => {
    if (initial === null) refresh();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const query = search.trim().toLowerCase();
  const shown = useMemo(
    () => (tables ?? []).filter((t) => !query || t.path.join(".").toLowerCase().includes(query)),
    [tables, query],
  );
  const table = (tables ?? []).find((t) => t.path.join(".") === picked);
  return (
    <div className="cs-picker">
      <div className="cs-picker-head">
        <strong>{describeConnection(connection)}</strong>
        {onBack && (
          <button type="button" className="link" onClick={onBack}>
            ← 其他連線
          </button>
        )}
      </div>
      <div className="cs-mode" role="radiogroup" aria-label="資料">
        <label>
          <input type="radio" name="cs-mode" checked={mode === "table"} onChange={() => setMode("table")} /> 資料表
        </label>
        <label>
          <input type="radio" name="cs-mode" checked={mode === "sql"} onChange={() => setMode("sql")} /> 自訂 SQL
        </label>
      </div>
      {mode === "table" ? (
        <>
          <div className="cs-filter">
            <input aria-label="結構描述" placeholder="結構描述（空白為預設）" value={schema} onChange={(e) => setSchema(e.target.value)} />
            <button type="button" onClick={refresh}>
              重新整理
            </button>
            <input type="search" aria-label="搜尋資料表" placeholder="搜尋資料表" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          {loadError && <p className="error">{loadError}</p>}
          {tables === null && !loadError && <p className="muted">載入中…</p>}
          {tables?.length === 0 && <p className="muted">這個結構描述中沒有資料表。</p>}
          <ul className="dse-choose cs-tables" aria-label="資料表">
            {shown.map((t) => {
              const key = t.path.join(".");
              return (
                <li key={key}>
                  <label>
                    <input type="radio" name="cs-table" checked={picked === key} onChange={() => setPicked(key)} />{" "}
                    {t.kind === "view" ? "◫" : "▦"} {t.schema ? <span className="muted">{t.schema}.</span> : null}
                    {t.name}
                    {t.kind === "view" && <span className="muted">（檢視）</span>}
                  </label>
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <>
          <textarea
            className="cs-sql"
            aria-label="自訂 SQL"
            spellCheck={false}
            rows={8}
            placeholder="SELECT …"
            value={sql}
            onChange={(e) => {
              setSql(e.target.value);
              setChecked(null);
            }}
          />
          <div className="dse-label">
            <button
              type="button"
              disabled={!sql.trim()}
              onClick={() =>
                check(sql)
                  .then(setChecked)
                  .catch((e: Error) => toast(e.message, "error"))
              }
            >
              檢查欄位
            </button>
            {checked && (
              <span className="cs-checked">
                {checked.map((c) => `${c.name}（${TYPES[c.datatype] ?? c.datatype}）`).join("、")}
              </span>
            )}
          </div>
        </>
      )}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || (mode === "table" ? !table : !sql.trim())}
          onClick={() => onChoose(mode === "table" && table ? { table_path: table.path } : { sql })}
        >
          {submitLabel}
        </button>
      </div>
    </div>
  );
}
