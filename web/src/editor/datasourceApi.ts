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
  | { kind: "table" | "sql"; name: string }
  | {
      kind: "join";
      join: JoinType;
      left: RelationView;
      right: RelationView;
      on: { left: [string, string]; op: string; right: [string, string] }[];
    }
  | { kind: "union"; name: string; parts: RelationView[] }
  | { kind: "pivot"; name: string; source: RelationView; columns: string[] }
  | { kind: "project"; name: string; source: RelationView };

export type JoinType = "inner" | "left" | "right" | "full";
export type Comparison = "=" | "!=" | "<" | "<=" | ">" | ">=";
/** 條件：已有的欄位 id、比較、新資料表的欄名（關聯為兩邊的欄位 id）。 */
export type Condition = [string, Comparison, string];

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

export interface RelationshipView {
  left: string;
  right: string;
  on: { left: string; op: string; right: string; left_caption: string; right_caption: string }[];
}

export interface DatasourceModel {
  datasource: { id: string; caption: string; kind: string };
  editable: boolean;
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
  setRelationship: (workbook: string, datasource: string, logical: string, on: Condition[]) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}/relationships/${part(logical)}`, {
      method: "PUT",
      body: JSON.stringify({ on }),
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
  rename: (workbook: string, datasource: string, caption: string) =>
    request<DatasourceModel>(`${edit(workbook)}/datasources/${part(datasource)}`, {
      method: "PUT",
      body: JSON.stringify({ caption }),
    }),
};
