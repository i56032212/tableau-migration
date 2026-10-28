// IR v0.3 中檢視器會用到的部分（完整定義見 kernel/schema/ir-0.3.schema.json）

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

export interface FieldFilter {
  kind: "categorical" | "range" | "top_n" | "condition" | "relative_date";
  id: string;
  field: FieldUse;
  stage: string;
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
  include_null?: boolean;
  /** 篩選群組：同一群組的篩選一起變更（套用到選取的工作表、全域篩選） */
  group?: string | null;
  /** 條件篩選的公式 */
  condition?: string;
  /** 相對日期：期間與範圍（例如最近 3 個月為 month、-2、0） */
  period?: "year" | "quarter" | "month" | "week" | "day";
  first?: number;
  last?: number;
  anchor?: string | null;
}

/** 多欄位篩選：篩選動作的目標、排除選取的標記 */
export interface TupleFilter {
  kind: "tuple";
  id: string;
  fields: FieldUse[];
  stage: string;
  mode: "include" | "exclude" | "all";
  tuples: Scalar[][];
  action: string | null;
  group?: string | null;
}

export type Filter = FieldFilter | TupleFilter;

/** 篩選動作：選取（或滑過）來源工作表的標記時，以標記的欄位值篩選目標工作表 */
export interface Action {
  id: string;
  name: string;
  caption: string;
  kind: "filter";
  activation: "select" | "hover" | "menu";
  /** 只在這個 dashboard 上執行；null 表示任何地方都執行 */
  dashboard: string | null;
  sources: string[];
  targets: string[];
  /** 目標是另一個 dashboard 時，執行後切換過去 */
  target_dashboard: string | null;
  clearing: "show_all" | "exclude_all" | "keep";
  single_select: boolean;
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
  /** container 的排列：tiled＝各自定位，horizontal／vertical＝沿方向排列 */
  layout?: "tiled" | "horizontal" | "vertical" | null;
  /** 固定大小（像素），沿父容器的排列方向 */
  fixed?: number | null;
  hidden?: boolean;
  show_title?: boolean;
  source_ref: string;
}

export interface Size {
  width: number;
  height: number;
}

export interface Dashboard {
  id: string;
  name: string;
  size: Size;
  sizing?: "fixed" | "automatic" | "range";
  min_size?: Size | null;
  max_size?: Size | null;
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
  actions: Action[];
  unsupported: Unsupported[];
}

// ---------------------------------------------------------------- 查詢服務

export interface FilterValue {
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
}

/** 選取的一個標記：欄位鍵 → 值 */
export type Mark = Record<string, Scalar>;

export interface QueryState {
  filters: Record<string, FilterValue>;
  parameters: Record<string, Scalar>;
  /** 篩選動作的選取：動作 id → 選取的標記 */
  actions?: Record<string, Mark[]>;
  /** 相對日期篩選的基準日（yyyy-mm-dd）；省略時由查詢服務決定 */
  today?: string | null;
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
