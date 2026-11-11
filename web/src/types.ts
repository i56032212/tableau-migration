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

/** 別名：維度成員或參數值的顯示名稱（只改變顯示，篩選仍用原始值）。 */
export interface MemberAlias {
  value: Scalar;
  label: string;
}

/** 集合的定義（只列出檢視器用到的部分：層級與固定成員） */
export interface FieldSet {
  levels: FieldUse[];
  rule: { kind: "members" | "top_n" | "condition"; mode?: "include" | "exclude" | "all"; members?: Scalar[][] };
}

export interface DataField {
  id: string;
  name: string;
  caption: string;
  role: "dimension" | "measure";
  datatype: string;
  format: string | null;
  aliases?: MemberAlias[];
  /** 集合（TRUE／FALSE，顯示為 In／Out） */
  set?: FieldSet | null;
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
  aliases?: MemberAlias[];
}

export interface FieldFilter {
  kind: "categorical" | "range" | "top_n" | "condition" | "relative_date";
  id: string;
  field: FieldUse;
  stage: string;
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  ranges?: MemberRange[];
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
  /** 跨資料源的篩選動作：fields 各欄位在來源工作表的資料源中對應的欄位 id */
  source_fields?: string[] | null;
  group?: string | null;
}

export type Filter = FieldFilter | TupleFilter;

/** 篩選動作：選取（或滑過）來源工作表的標記時，以標記的欄位值篩選目標工作表 */
export interface UrlField {
  placeholder: string;
  /** 欄位鍵；參數為 param:<參數 id> */
  key: string;
}

export interface UrlAction {
  template: string;
  fields: UrlField[];
  escape: boolean;
  delimiter: string;
}

export interface ParameterAction {
  parameter: string;
  /** 來源欄位的欄位鍵 */
  field: string;
  aggregation: string;
  clearing: "keep" | "assign";
  clear_value: Scalar;
}

export interface SetAction {
  target: string;
  behavior: "assign" | "add" | "remove";
  clearing: "keep" | "add_all" | "remove_all";
}

export interface Action {
  id: string;
  name: string;
  caption: string;
  kind: "filter" | "highlight" | "url" | "parameter" | "navigate" | "set";
  url?: UrlAction | null;
  parameter?: ParameterAction | null;
  /** 集合動作：以選取的標記改變集合的成員 */
  set_action?: SetAction | null;
  /** 醒目提示動作：比對的欄位（欄位的顯示名稱）；空的表示所有欄位 */
  highlight?: { fields: string[] } | null;
  /** 切換動作的目標工作表 */
  target_worksheet?: string | null;
  /** 切換動作的目標 Story */
  target_story?: string | null;
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
  /** 頁面架：每一頁是這個維度的一個值 */
  pages?: FieldUse | null;
  /** 存檔時的頁；null 為第一頁 */
  page?: Scalar;
}

export type ZoneKind =
  | "container"
  | "worksheet"
  | "filter"
  | "parameter"
  | "legend"
  | "page"
  | "highlighter"
  | "text"
  | "image"
  | "title"
  | "blank"
  | "button"
  | "web";

/** dashboard 按鈕：導覽、顯示或隱藏區塊、匯出。 */
export interface Button {
  action: "navigate" | "toggle" | "export";
  target_dashboard: string | null;
  target_worksheet: string | null;
  /** 導覽到 Story */
  target_story?: string | null;
  zones: string[];
  export: "pdf" | "image" | "powerpoint" | null;
  caption: string | null;
  tooltip: string | null;
  /** .twbx 內的圖片；toggle 時為區塊顯示中的圖片 */
  image: string | null;
  image_hidden: string | null;
  color: string | null;
  background: string | null;
}

/** 一段文字的格式（Tableau 格式化文字的 run） */
export interface TextStyle {
  bold?: boolean | null;
  italic?: boolean | null;
  underline?: boolean | null;
  /** 字級（pt） */
  size?: number | null;
  color?: string | null;
  font?: string | null;
  align?: "left" | "center" | "right" | null;
}

/** 文字物件與標題的一段：文字或參數的值 */
export interface ZoneTextRun {
  text?: string | null;
  parameter?: string | null;
  style?: TextStyle | null;
}

/** 工作表的顯示大小（Tableau 的「調整」）。 */
export type FitMode = "standard" | "fit_width" | "fit_height" | "entire_view";

/** 區塊的格式：外距（框線之外）、框線、背景與內距（框線之內），單位為像素；依序為上、右、下、左。 */
export interface ZoneStyle {
  background: string | null;
  border: "none" | "solid" | "dashed" | "dotted";
  border_width: number;
  border_color: string | null;
  margin: [number, number, number, number];
  padding: [number, number, number, number];
}

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
  /** 文字物件與標題的內容（參數換成目前的值、附格式）；空的時只用 text */
  content?: ZoneTextRun[];
  image: string | null;
  button?: Button | null;
  /** 網頁區塊的網址 */
  url?: string | null;
  children: Zone[];
  /** container 的排列：tiled＝各自定位，horizontal／vertical＝沿方向排列 */
  layout?: "tiled" | "horizontal" | "vertical" | null;
  /** 固定大小（像素），沿父容器的排列方向 */
  fixed?: number | null;
  hidden?: boolean;
  show_title?: boolean;
  /** 沒有時表示沒有外距、內距、框線與背景 */
  style?: ZoneStyle | null;
  /** 工作表區塊中的顯示大小；沒有時依工作表本身 */
  fit?: FitMode | null;
  source_ref: string;
}

export interface Size {
  width: number;
  height: number;
}

export type Device = "desktop" | "tablet" | "phone";

/** 裝置專屬版面：手機、平板或桌面上改用的版面（區塊參照預設版面上同樣的工作表與控制項） */
export interface DeviceLayout {
  device: Device;
  /** default：與預設版面相同；fit_width：寬度隨畫面、高度固定（上下捲動） */
  sizing: "default" | "fixed" | "automatic" | "fit_width";
  size: Size;
  auto_generated?: boolean;
  zones: Zone[];
}

export interface Dashboard {
  id: string;
  name: string;
  size: Size;
  sizing?: "fixed" | "automatic" | "range";
  min_size?: Size | null;
  max_size?: Size | null;
  /** dashboard 的背景顏色；沒有時為白色 */
  background?: string | null;
  zones: Zone[];
  layouts?: DeviceLayout[];
}

export interface Unsupported {
  source_ref: string;
  reason: string;
}

/** 故事點記下的篩選動作選取：以這些標記執行動作（篩選目標工作表） */
export interface StoryAction {
  action: string;
  marks: Mark[];
}

/** 故事點記下的工作表篩選：取代同一欄位的篩選 */
export interface StoryFilter {
  worksheet: string;
  field: FieldUse;
  mode: "include" | "exclude";
  values: Scalar[];
}

/** 故事點記下的選取：工作表上醒目顯示的標記 */
export interface StorySelection {
  worksheet: string;
  marks: Mark[];
}

/** 故事點：顯示一個 dashboard 或工作表，以及與預設不同的篩選與選取 */
export interface StoryPoint {
  id: string;
  caption: string;
  dashboard: string | null;
  worksheet: string | null;
  actions: StoryAction[];
  filters: StoryFilter[];
  selections: StorySelection[];
  /** 故事點記下的參數值 */
  parameters?: Record<string, Scalar>;
  /** 故事點改過的 dashboard 文字物件：區塊 id → 內容 */
  texts?: Record<string, ZoneTextRun[]>;
}

export interface Story {
  id: string;
  name: string;
  /** 標題的文字；null 表示不顯示標題 */
  title: string | null;
  size: { width: number; height: number };
  navigation: "caption" | "number" | "dots";
  arrows: boolean;
  points: StoryPoint[];
  /** 開啟時顯示的故事點；null 表示第一個 */
  active: string | null;
}

export interface IR {
  ir_version: string;
  workbook: { id: string; name: string };
  datasources: Datasource[];
  parameters: Parameter[];
  worksheets: Worksheet[];
  dashboards: Dashboard[];
  actions: Action[];
  stories?: Story[];
  unsupported: Unsupported[];
}

// ---------------------------------------------------------------- 查詢服務

export interface FilterValue {
  mode?: "include" | "exclude" | "all";
  values?: Scalar[];
  min?: Scalar;
  max?: Scalar;
  /** 工作表原本的篩選中連續的成員（只用於顯示控制項的勾選，不送給查詢服務） */
  ranges?: MemberRange[];
}

/** 連續的一段成員（含頭尾） */
export interface MemberRange {
  start: Scalar;
  end: Scalar;
}

/** 選取的一個標記：欄位鍵 → 值 */
export type Mark = Record<string, Scalar>;

/** 集合的成員：include 為列出的成員在集合中，exclude 為列出的以外在集合中 */
export interface SetValue {
  mode: "include" | "exclude";
  members: Scalar[][];
}

export interface QueryState {
  filters: Record<string, FilterValue>;
  parameters: Record<string, Scalar>;
  /** 篩選動作的選取：動作 id → 選取的標記 */
  actions?: Record<string, Mark[]>;
  /** 集合動作改變後的成員：集合 id → 成員 */
  sets?: Record<string, SetValue>;
  /** 相對日期篩選的基準日（yyyy-mm-dd）；省略時由查詢服務決定 */
  today?: string | null;
  /** 頁面架目前的頁：工作表 id → 頁面欄位的值 */
  pages?: Record<string, Scalar>;
  /** 階層的切入：欄、列架上一組階層欄位最上層的欄位鍵 → 向下多顯示的層數（負數收合存檔時的下層） */
  drill?: Record<string, number>;
  /** 在另一張工作表的工具提示中顯示（Viz in Tooltip）：來源工作表與滑過的標記 */
  tooltip?: { source: string; mark: Mark } | null;
}

/** 工具提示中的工作表（Viz in Tooltip）：滑過標記時顯示，以標記的值篩選 */
export interface TooltipSheetInfo {
  worksheet: string;
  name: string;
  max_width: number;
  max_height: number;
}

/** 檢視器的切入控制項：工作表欄、列架上可以向下切入或向上收合的一組階層 */
export interface DrillControl {
  /** 這組階層最上層的欄位鍵（QueryState.drill 的鍵） */
  key: string;
  shelf: "rows" | "columns";
  /** 目前顯示的最下層，例如 Sub-Category、QUARTER(Order Date) */
  label: string;
  /** 最上層 */
  head: string;
  /** 向下切入時加入（或日期值改成）的下一層 */
  next: string | null;
  /** 目前切入的層數 */
  delta: number;
  can_down: boolean;
  can_up: boolean;
}

export interface WorkbookSummary {
  id: string;
  workbook: string;
  name: string;
  dashboards: { id: string; name: string }[];
  worksheets: { id: string; name: string }[];
  stories?: { id: string; name: string }[];
  unsupported: number;
  /** 發布到報表庫的報表（fixtures 的報表為 null） */
  published?: {
    project: string;
    project_name: string;
    version: number;
    published_at: string;
    publisher: string;
    can_download: boolean;
    can_overwrite: boolean;
  } | null;
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
  /** 工作表的標題：自訂標題代入目前的參數值；沒有自訂標題時為工作表名稱 */
  title?: string;
  rows: number;
  mark?: string;
  spec: Record<string, unknown> | null;
  unsupported: string | null;
  notes: string[];
  applied?: string[];
  skipped?: string[];
  /** 階層的切入控制項（依存檔的工作表） */
  drill?: DrillControl[];
  /** 工具提示中的工作表 */
  tooltip_sheets?: TooltipSheetInfo[];
  table?: { keys: string[]; headers: string[]; rows: string[][]; raw: string[][] };
}

export type Domain =
  | { kind: "values"; values: Scalar[] }
  | { kind: "range"; min: Scalar; max: Scalar };

// ---------------------------------------------------------------- 身分驗證

export interface UserInfo {
  username: string;
  full_name: string | null;
  domain: string | null;
  groups: string[];
  /** Tableau 的站台角色（使用者目錄中有時） */
  site_role?: string | null;
  /** 站台管理員：可以使用站台管理 */
  admin?: boolean;
}

/** /api/me：身分驗證的模式與目前的登入者 */
export interface Me {
  /** desktop：桌面版的本機引擎（登入者為作業系統的帳號） */
  mode: "none" | "dev" | "proxy" | "oidc" | "desktop";
  user: UserInfo | null;
  /** oidc：登入的網址 */
  login?: string;
  /** dev：可以切換的使用者 */
  users?: string[];
}

/** 桌面版開啟中的檔案 */
export interface DesktopDocument {
  id: string;
  name: string;
  /** 開啟的檔案 */
  source: string;
  kind: "twbx" | "twb" | "package";
  /** 最近一次存成的報表套件 */
  saved: string | null;
  /** 編輯器改過、還沒儲存 */
  modified?: boolean;
  /** 從伺服器開啟或發布到伺服器的報表；version 為之後發布時的基準版本 */
  server?: {
    url: string;
    report: string;
    version: number;
    project: string;
    project_name: string;
    name: string;
  } | null;
}

export interface DesktopOpened {
  document: DesktopDocument;
  already_open: boolean;
  unsupported_count: number;
  unsupported: string[];
}

export interface DesktopSaved {
  document: DesktopDocument;
  path: string;
  size: number;
  datasources: string[];
  files: string[];
  missing_files: string[];
}

/** 桌面版連到的伺服器 */
export interface ServerStatus {
  url: string | null;
  username: string | null;
  name: string | null;
  signed_in: boolean;
  /** 正在等使用者在瀏覽器中核准 */
  pending: boolean;
  error: string | null;
}

export interface ServerProject {
  id: string;
  name: string;
  path: string;
  can_publish: boolean;
}

/** 伺服器上的 Published Data Source（桌面版用到的部分） */
export interface ServerDatasource {
  id: string;
  name: string;
  content_url: string;
  project: string;
  project_path: string;
  version: number;
  extract: boolean;
  can: Record<string, boolean>;
}

/** 桌面版：報表的資料源，與已經連到的伺服器上的資料源（看得到時） */
export interface DesktopDatasource {
  id: string;
  caption: string;
  connection: "extract" | "live" | "file";
  /** 連到的伺服器上的資料源的網址名稱 */
  content_url: string | null;
  server: ServerDatasource | null;
}

export interface DatasourcePublishResult {
  datasource: ServerDatasource;
  /** 報表改用了發布的資料源 */
  linked: boolean;
  address: string;
}

export interface ServerReport {
  id: string;
  name: string;
  project: string;
  project_name: string;
  owner: string;
  version: number;
  published_at: string;
  publisher: string;
  comment: string;
  url: string;
  can_download: boolean;
  can_overwrite: boolean;
}

export interface VersionRecord {
  version: number;
  published_at: string;
  publisher: string;
  comment: string;
  size: number;
  sha256: string;
  restored_from: number | null;
}

export interface ReportDetail extends ServerReport {
  /** 由新到舊 */
  versions: VersionRecord[];
}

export interface ReportChange {
  kind: "datasource" | "field" | "parameter" | "worksheet" | "dashboard" | "action" | "story";
  id: string;
  name: string;
  change: "added" | "removed" | "changed";
  parent: string | null;
  text: string;
}

export interface CheckProblem {
  level: "error" | "warning";
  message: string;
  worksheet: string | null;
}

export interface PublishResult {
  report: ServerReport;
  version: VersionRecord;
  problems: CheckProblem[];
  /** 伺服器上的報表網址（桌面版） */
  address?: string;
}

/** 發布或回復失敗時伺服器回應的 detail */
export interface PublishFailure {
  error: "exists" | "conflict" | "checks" | "package" | "name" | "project" | "comment" | "too_large";
  message: string;
  problems?: CheckProblem[];
  report?: ServerReport | null;
  latest?: VersionRecord;
  base_version?: number | null;
  server_changes?: ReportChange[];
  local_changes?: ReportChange[];
  overwrite_changes?: ReportChange[];
}

export interface DesktopToken {
  id: string;
  label: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  current: boolean;
}

/** 資料源分頁：連線、資料表與關聯、欄位與資料源篩選（與完整的報表定義相同要下載 workbook 的權限）。 */
export interface DatasourceConnection {
  kind: "extract" | "live" | "file";
  /** 檔案型資料源（Excel、文字檔）在 .twbx 內的檔案 */
  files?: string[];
  /** 連線類型的顯示名稱，例如「Tableau 萃取（Hyper）」「PostgreSQL」 */
  type: string;
  file?: string;
  table?: string;
  /** 萃取的來源資料庫 */
  source?: DatasourceConnection | null;
  refresh?: string | null;
  row_limit?: number | null;
  dbclass?: string;
  connection_id?: string;
  server?: string | null;
  port?: number | null;
  database?: string | null;
  schema?: string | null;
  http_path?: string | null;
  authentication?: string | null;
  supported?: boolean;
}

export interface DatasourceTable {
  /** Relationship 模型的邏輯資料表 id；單一資料表或 join 樹時為 null */
  id: string | null;
  caption: string;
  physical: { name: string; source: string; sql: string | null }[];
  joins: { type: string; label: string; left: string[]; right: string[]; on: string[] }[];
}

export interface DatasourceField {
  id: string;
  caption: string;
  name: string;
  role: "dimension" | "measure";
  datatype: string;
  kind: "column" | "calc" | "set";
  table: string | null;
  column: string | null;
  formula: string | null;
  default_agg: string | null;
  format: string | null;
  hidden: boolean;
  aliases: number;
}

export interface DatasourceSummary {
  id: string;
  caption: string;
  name: string;
  connection: DatasourceConnection;
  published: { content_url: string; site: string | null; revision: string | null } | null;
  model: boolean;
  tables: DatasourceTable[];
  relationships: { left: string; right: string; on: string[] }[];
  fields: DatasourceField[];
  filters: { id: string; stage: "extract" | "datasource"; text: string }[];
}

/** 資料源分頁的資料預覽：前 limit 列原始資料（套用資料源篩選與列層級權限）。 */
export interface DatasourcePreview {
  datasource: string;
  table: string | null;
  columns: { field: string; caption: string; datatype: string; role: string; calculated: boolean }[];
  rows: Scalar[][];
  /** 資料源篩選後的總列數（萃取）；Live 連線為 null */
  total: number | null;
  limit: number;
}
