// 桌面版的資料源編輯（「連線到資料」與資料源分頁）的 API：檢查資料檔、新增報表與資料源、加入資料表
// （關聯、join、聯集）、樞紐與欄位的設定（見 service 的 tmig_service.datasource_editor）。

import { request } from "../api";
import type { DesktopOpened } from "../types";

const edit = (workbook: string) => `/api/edit/${encodeURIComponent(workbook)}`;
const part = encodeURIComponent;

export interface InspectedColumn {
  name: string;
  datatype: string;
}

export interface InspectedTable {
  name: string;
  sheet: string | null;
  layer: string | null;
  rows: number;
  /** 資料解譯器找出的標題列 */
  interpreted?: boolean;
  columns: InspectedColumn[];
}

export interface InspectedFile {
  path: string;
  name: string;
  format: "excel" | "text" | "spatial";
  separator: string;
  charset: string;
  tables: InspectedTable[];
}

export type RelationView =
  /** connection：跨資料庫的 join 中資料庫的資料表所在的連線 */
  | { kind: "table"; name: string; connection?: string }
  | { kind: "sql"; name: string; sql: string; connection?: string }
  | {
      kind: "join";
      join: JoinType;
      left: RelationView;
      right: RelationView;
      on: {
        left: [string | null, string | null];
        op: string;
        right: [string | null, string | null];
        /** join 計算（以顯示名稱參照欄位） */
        left_formula?: string | null;
        right_formula?: string | null;
      }[];
    }
  | { kind: "union"; name: string; parts: RelationView[]; wildcard?: { files: string; sheets: string | null; recursive: boolean } | null }
  | { kind: "pivot"; name: string; source: RelationView; columns: string[] }
  | { kind: "project"; name: string; source: RelationView };

export type JoinType = "inner" | "left" | "right" | "full";
/** 條件的比較；intersects 是空間 join（只能用在 join）。 */
export type Comparison = "=" | "!=" | "<" | "<=" | ">" | ">=" | "intersects";
/** 條件的一邊：欄位（已有的欄位 id；新資料表為欄名），或公式（join 計算、關聯計算）。 */
export type Side = string | { formula: string };
/** 條件：已有的欄位 id、比較、新資料表的欄名（關聯為兩邊的欄位 id）；任一邊可以是公式。 */
export type Condition = [Side, Comparison, Side];

/** 條件的一邊已填好（選了欄位，或公式不是空的）。 */
export function sideFilled(side: Side): boolean {
  return typeof side === "string" ? side !== "" : side.formula.trim() !== "";
}

export interface ModelField {
  id: string;
  caption: string;
  column: string | null;
  table: string | null;
  physical_table: string | null;
  datatype: string;
  role: "dimension" | "measure";
  default_agg: string | null;
  hidden: boolean;
  calc: string | null;
}

export interface ModelFile {
  path: string;
  name: string;
  available: boolean;
  /** Excel 檔使用資料解譯器 */
  interpreted?: boolean;
  format?: InspectedFile["format"];
  tables?: InspectedTable[];
  error?: string;
}

export interface LogicalView {
  /** Relationship 模型的邏輯資料表 id；不是模型時為 null（整個資料源一張） */
  id: string | null;
  caption: string;
  relation: RelationView;
}

/** 關聯的效能選項：基數（true 為「一」）與參考完整性（true 為「所有記錄相符」） */
export interface RelationshipOptions {
  left_unique: boolean;
  right_unique: boolean;
  left_complete: boolean;
  right_complete: boolean;
}

export interface RelationshipView {
  left: string;
  right: string;
  options?: RelationshipOptions;
  on: {
    left: string;
    op: string;
    right: string;
    left_caption: string;
    right_caption: string;
    /** 關聯計算（以顯示名稱參照欄位） */
    left_formula?: string | null;
    right_formula?: string | null;
  }[];
}

/** 資料庫的連線（Live）：連線代號與伺服器、資料庫。 */
export interface ModelConnection {
  connection_id: string;
  dbclass: string;
  server: string | null;
  port: number | null;
  database: string | null;
  schema: string | null;
}

export interface DatasourceModel {
  /** federated：跨資料庫的 join（檔案的資料源中也有資料庫的資料表） */
  datasource: { id: string; caption: string; kind: string; federated?: boolean };
  editable: boolean;
  /** 資料庫的資料源（檔案、萃取時為 null） */
  connection: ModelConnection | null;
  /** 資料庫的連線：Live 連線本身，或跨資料庫的 join 中的各資料庫 */
  connections: ModelConnection[];
  files: ModelFile[];
  logical: LogicalView[];
  relationships: RelationshipView[];
  fields: ModelField[];
}

export interface NewWorkbookResult extends DesktopOpened {
  datasource: string;
  worksheet: string;
}

export interface AddTableRequest {
  /** 作者電腦上的檔案（新的檔案） */
  path?: string;
  /** 報表中已有的檔案（Data/…） */
  file?: string;
  table: string | null;
  /** 資料庫的資料表（限定名稱的各段） */
  table_path?: string[];
  /** 資料庫的自訂 SQL */
  sql?: string;
  /** 資料庫的資料表所在的連線（省略時為資料源的資料庫）：另一個連線時成為跨資料庫的 join */
  connection?: string;
  how: "relationship" | "join" | "union";
  target?: string | null;
  join?: JoinType;
  on?: Condition[];
}

export const datasourceApi = {
  inspectNew: (path: string) =>
    request<InspectedFile>("/api/desktop/inspect", { method: "POST", body: JSON.stringify({ path }) }),
  newWorkbook: (path: string, table: string | null) =>
    request<NewWorkbookResult>("/api/desktop/new", { method: "POST", body: JSON.stringify({ path, table }) }),
  inspect: (workbook: string, path: string) =>
    request<InspectedFile>(`${edit(workbook)}/inspect-file`, { method: "POST", body: JSON.stringify({ path }) }),
  addDatasource: (workbook: string, path: string, table: string | null) =>
    request<{ datasource: string }>(`${edit(workbook)}/datasources`, {
      method: "POST",
      body: JSON.stringify({ path, table }),
    }),
  model: (workbook: string, datasource: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/model`),
  addTable: (workbook: string, datasource: string, body: AddTableRequest) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/tables`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  removeTable: (workbook: string, datasource: string, table: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/tables/${part(table)}`, {
      method: "DELETE",
    }),
  setJoin: (workbook: string, datasource: string, table: string, join: JoinType, on: Condition[]) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/joins/${part(table)}`, {
      method: "PUT",
      body: JSON.stringify({ join, on }),
    }),
  setRelationship: (workbook: string, datasource: string, logical: string, on: Condition[], options?: RelationshipOptions) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/relationships/${part(logical)}`, {
      method: "PUT",
      body: JSON.stringify(options ? { on, options } : { on }),
    }),
  pivot: (workbook: string, datasource: string, table: string, columns: string[]) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/pivot`, {
      method: "POST",
      body: JSON.stringify({ table, columns }),
    }),
  unpivot: (workbook: string, datasource: string, name: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/pivot/${part(name)}`, {
      method: "DELETE",
    }),
  updateField: (
    workbook: string,
    datasource: string,
    field: string,
    body: Partial<Pick<ModelField, "caption" | "hidden" | "role" | "default_agg" | "datatype">>,
  ) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/fields/${part(field)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 連到資料庫的資料源（資料表或自訂 SQL） */
  addLiveDatasource: (workbook: string, connection: string, source: { table_path?: string[]; sql?: string }) =>
    request<{ datasource: string }>(`${edit(workbook)}/datasources`, {
      method: "POST",
      body: JSON.stringify({ connection, ...source }),
    }),
  remoteTables: (workbook: string, datasource: string, schema?: string | null, connection?: string | null) => {
    const query = new URLSearchParams();
    if (schema) query.set("schema", schema);
    if (connection) query.set("connection", connection);
    const text = query.toString();
    return request<{ tables: RemoteTable[] }>(
      `${edit(workbook)}/datasources/${part(datasource)}/remote-tables${text ? `?${text}` : ""}`,
    );
  },
  remoteColumns: (workbook: string, datasource: string, source: { table?: string[]; sql?: string; connection?: string | null }) =>
    request<{ columns: InspectedColumn[] }>(`${edit(workbook)}/datasources/${part(datasource)}/remote-columns`, {
      method: "POST",
      body: JSON.stringify(source),
    }).then((r) => r.columns),
  setCustomSql: (workbook: string, datasource: string, name: string, sql: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/custom-sql/${part(name)}`, {
      method: "PUT",
      body: JSON.stringify({ sql }),
    }),
  setInterpreter: (workbook: string, datasource: string, file: string, enabled: boolean) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/interpreter`, {
      method: "PUT",
      body: JSON.stringify({ file, enabled }),
    }),
  wildcardUnion: (
    workbook: string,
    datasource: string,
    body: { table: string; folder: string; files: string; sheets: string | null; recursive: boolean },
  ) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/wildcard-union`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  rename: (workbook: string, datasource: string, caption: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}`, {
      method: "PUT",
      body: JSON.stringify({ caption }),
    }),
};

// ---------------------------------------------------------------- 連線到伺服器（資料庫）

export interface Connector {
  dbclass: string;
  label: string;
  dialect: string;
  port: number | null;
  /** 資料庫欄位的名稱（Oracle 為服務名稱等）；null 表示不需要 */
  database: string | null;
  schema: boolean;
  /** 密碼的種類：password、access_token；空字串表示不需要密碼（雲端的預設憑證） */
  secret: string;
  extras: { key: string; label: string }[];
}

export interface ConnectionEntry {
  id: string;
  dbclass: string;
  label: string;
  server: string | null;
  port: number | null;
  database: string | null;
  schema: string | null;
  user: string | null;
  /** 密碼已在記憶體中（或不需要密碼） */
  signed_in: boolean;
}

export interface RemoteTable {
  schema: string | null;
  name: string;
  kind: "table" | "view";
  path: string[];
}

export interface ConnectRequest {
  dbclass: string;
  server?: string | null;
  port?: number | null;
  database?: string | null;
  schema?: string | null;
  user?: string | null;
  password?: string | null;
  password_env?: string | null;
  remember?: boolean;
  extras?: Record<string, string>;
}

const desktop = "/api/desktop";

export const connectionsApi = {
  connectors: () => request<{ connectors: Connector[] }>(`${desktop}/connectors`).then((r) => r.connectors),
  list: () => request<{ connections: ConnectionEntry[] }>(`${desktop}/connections`).then((r) => r.connections),
  connect: (body: ConnectRequest) =>
    request<{ connection: ConnectionEntry; tables: RemoteTable[] }>(`${desktop}/connections`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  signIn: (id: string, password: string, remember: boolean) =>
    request<{ connection: ConnectionEntry }>(`${desktop}/connections/${part(id)}/sign-in`, {
      method: "POST",
      body: JSON.stringify({ password, remember }),
    }),
  remove: (id: string) => request<{ ok: boolean }>(`${desktop}/connections/${part(id)}`, { method: "DELETE" }),
  tables: (id: string, schema?: string | null) =>
    request<{ tables: RemoteTable[] }>(
      `${desktop}/connections/${part(id)}/tables${schema ? `?schema=${encodeURIComponent(schema)}` : ""}`,
    ).then((r) => r.tables),
  columns: (id: string, source: { table?: string[]; sql?: string }) =>
    request<{ columns: InspectedColumn[] }>(`${desktop}/connections/${part(id)}/columns`, {
      method: "POST",
      body: JSON.stringify(source),
    }).then((r) => r.columns),
  newWorkbook: (connection: string, source: { table_path?: string[]; sql?: string }) =>
    request<NewWorkbookResult>(`${desktop}/new-live`, {
      method: "POST",
      body: JSON.stringify({ connection, ...source }),
    }),
};
