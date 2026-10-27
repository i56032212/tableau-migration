// IR v0.2 中檢視器會用到的部分（完整定義見 kernel/schema/ir-0.2.schema.json）

export type Scalar = string | number | boolean | null;

export interface DateDerivation {
  kind: "part" | "trunc";
  unit: string;
}

export interface FieldUse {
  field: string;
  agg: string | null;
  date: DateDerivation | null;
  continuous: boolean;
  table_calc: string | null;
}

export interface DataField {
  id: string;
  name: string;
  caption: string;
  role: "dimension" | "measure";
  datatype: string;
  format: string | null;
}

export interface Datasource {
  id: string;
  caption: string;
  fields: DataField[];
}

export interface Allowed {
  kind: "all" | "list" | "range";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
  step?: number | null;
}

export interface Parameter {
  id: string;
  name: string;
  caption: string;
  datatype: string;
  allowed: Allowed;
  value: Scalar;
}

export interface Filter {
  kind: "categorical" | "range" | "top_n";
  id: string;
  field: FieldUse;
  stage: string;
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
  include_null?: boolean;
}

export interface Worksheet {
  id: string;
  name: string;
  datasource: string;
  filters: Filter[];
}

export type ZoneKind =
  | "container"
  | "worksheet"
  | "filter"
  | "parameter"
  | "legend"
  | "text"
  | "image"
  | "title"
  | "blank";

export interface Zone {
  id: string;
  kind: ZoneKind;
  x: number;
  y: number;
  w: number;
  h: number;
  worksheet: string | null;
  field: FieldUse | null;
  parameter: string | null;
  control: string | null;
  legend: string | null;
  text: string | null;
  image: string | null;
  children: Zone[];
  source_ref: string;
}

export interface Dashboard {
  id: string;
  name: string;
  size: { width: number; height: number };
  zones: Zone[];
}

export interface Unsupported {
  source_ref: string;
  reason: string;
}

export interface IR {
  ir_version: string;
  workbook: { id: string; name: string };
  datasources: Datasource[];
  parameters: Parameter[];
  worksheets: Worksheet[];
  dashboards: Dashboard[];
  unsupported: Unsupported[];
}

// ---------------------------------------------------------------- 查詢服務

export interface FilterValue {
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
}

export interface QueryState {
  filters: Record<string, FilterValue>;
  parameters: Record<string, Scalar>;
}

export interface WorkbookSummary {
  id: string;
  workbook: string;
  name: string;
  dashboards: { id: string; name: string }[];
  worksheets: { id: string; name: string }[];
  unsupported: number;
}

export interface CaseInfo {
  name: string;
  note: string | null;
  url_params: Record<string, Scalar>;
  state: QueryState;
}

export interface RenderResponse {
  worksheet: string;
  name?: string;
  rows: number;
  mark?: string;
  spec: Record<string, unknown> | null;
  unsupported: string | null;
  notes: string[];
  applied?: string[];
  skipped?: string[];
  table?: { keys: string[]; headers: string[]; rows: string[][]; raw: string[][] };
}

export type Domain =
  | { kind: "values"; values: Scalar[] }
  | { kind: "range"; min: Scalar; max: Scalar };
