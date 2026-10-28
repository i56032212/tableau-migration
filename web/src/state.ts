import type { Action, FieldFilter, FieldUse, FilterValue, IR, Mark, QueryState, Scalar } from "./types";

/** 檢視器的狀態：各工作表的篩選、參數、篩選動作的選取。 */
export interface ViewState {
  sheetFilters: Record<string, Record<string, FilterValue>>;
  parameters: Record<string, Scalar>;
  /** 篩選動作 id → 傳給目標工作表的標記（清除時保留篩選的動作，清除選取後仍留著） */
  actions: Record<string, Mark[]>;
  /** 來源工作表 id → 目前選取的標記（畫面上醒目顯示） */
  selections: Record<string, Mark[]>;
  /** 相對日期篩選的基準日；null 表示由查詢服務決定（cases.yaml 的基準日或今天） */
  today?: string | null;
}

/** 篩選狀態的欄位鍵，與查詢服務相同：f_region、year(f_order_date)、trunc_month(f_order_date)。 */
export function fieldKey(use: FieldUse): string {
  let key = use.field;
  if (use.date) {
    key = `${use.date.kind === "part" ? use.date.unit : `trunc_${use.date.unit}`}(${key})`;
  }
  return key;
}

export function findFilter(ir: IR, worksheet: string, key: string): FieldFilter | undefined {
  const sheet = ir.worksheets.find((w) => w.id === worksheet);
  return sheet?.filters.find(
    (f): f is FieldFilter =>
      f.kind !== "top_n" && f.kind !== "tuple" && f.field.agg === null && fieldKey(f.field) === key,
  );
}

/** 篩選控制項目前的值：使用者改過的狀態優先，否則為工作表原本的篩選。 */
export function currentFilter(
  ir: IR,
  worksheet: string,
  key: string,
  state: Record<string, FilterValue> | undefined,
): FilterValue {
  const changed = state?.[key];
  if (changed) return changed;
  const original = findFilter(ir, worksheet, key);
  if (!original) return { mode: "all", values: [] };
  if (original.kind === "range") return { min: original.min ?? null, max: original.max ?? null };
  return { mode: original.mode ?? "all", values: original.values ?? [] };
}

export function selectedValues(value: FilterValue, domain: Scalar[]): Scalar[] {
  if (value.mode === "all" || value.mode === undefined) return domain;
  const listed = new Set((value.values ?? []).map((v) => JSON.stringify(v)));
  if (value.mode === "include") return domain.filter((v) => listed.has(JSON.stringify(v)));
  return domain.filter((v) => !listed.has(JSON.stringify(v)));
}

/** 查詢一張工作表的狀態：只帶以它為目標的篩選動作，其他工作表的選取不會讓它重新查詢。 */
export function stateFor(ir: IR, worksheet: string, view: ViewState): QueryState {
  const actions: Record<string, Mark[]> = {};
  for (const action of ir.actions ?? []) {
    const marks = view.actions[action.id];
    if (marks && action.targets.includes(worksheet)) actions[action.id] = marks;
  }
  const state: QueryState = { filters: view.sheetFilters[worksheet] ?? {}, parameters: view.parameters, actions };
  if (view.today) state.today = view.today;
  return state;
}

const PERIODS: Record<string, [string, string, string, string]> = {
  // 單位、本期、上一期、下一期
  year: ["年", "今年", "去年", "明年"],
  quarter: ["季", "本季", "上一季", "下一季"],
  month: ["個月", "本月", "上個月", "下個月"],
  week: ["週", "本週", "上週", "下週"],
  day: ["天", "今天", "昨天", "明天"],
};

/** 相對日期篩選的說明，例如「最近 3 個月」。 */
export function describeRelative(filter: FieldFilter): string {
  const [unit, current, previous, next] = PERIODS[filter.period ?? "day"] ?? PERIODS.day;
  const first = filter.first ?? 0;
  const last = filter.last ?? 0;
  if (first === last) {
    if (first === 0) return current;
    if (first === -1) return previous;
    if (first === 1) return next;
    return first < 0 ? `${-first} ${unit}前` : `${first} ${unit}後`;
  }
  if (last === 0 && first < 0) return `最近 ${1 - first} ${unit}`;
  if (first === 0 && last > 0) return `接下來 ${last + 1} ${unit}`;
  return `第 ${first} 到 ${last} ${unit}`;
}

/** 這張工作表在這個 dashboard 上、以某種方式觸發的篩選動作。 */
export function sourceActions(
  ir: IR,
  worksheet: string,
  dashboard: string | null,
  activation: Action["activation"],
): Action[] {
  return (ir.actions ?? []).filter(
    (a) =>
      a.activation === activation &&
      a.sources.includes(worksheet) &&
      (a.dashboard === null || a.dashboard === dashboard),
  );
}

export function sameMark(a: Mark, b: Mark): boolean {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((k) => k in b && a[k] === b[k]);
}

/** 網址動作的網址：以標記的值（多個標記以分隔字元連接）與參數值代入 <[欄位]>。 */
export function actionUrl(
  action: Action,
  values: Record<string, Scalar>[],
  parameters: Record<string, Scalar>,
): string {
  const spec = action.url;
  if (!spec) return "";
  let url = spec.template;
  for (const field of spec.fields) {
    let raw: Scalar[];
    if (field.key.startsWith("param:")) raw = [parameters[field.key.slice(6)] ?? null];
    else raw = values.map((v) => (field.key in v ? v[field.key] : null));
    // 數字去掉浮點誤差（12 位有效數字），例如 678781.2399999991 → 678781.24
    const unique = raw
      .filter((v, i) => v !== null && raw.indexOf(v) === i)
      .map((v) => (typeof v === "number" ? String(Number(v.toPrecision(12))) : String(v)));
    const text = unique.join(spec.delimiter);
    url = url.split(field.placeholder).join(spec.escape ? encodeURIComponent(text) : text);
  }
  // 找不到欄位的參照代入空字串
  return url.replace(/<[^<>]+>/g, "");
}

/** 參數動作設給參數的值：選取標記時為標記的欄位值；清除選取時保留或設成指定的值（undefined 表示不變）。 */
export function parameterValue(action: Action, values: Record<string, Scalar>[]): Scalar | undefined {
  const spec = action.parameter;
  if (!spec) return undefined;
  if (values.length === 0) return spec.clearing === "assign" ? spec.clear_value : undefined;
  const raw = values.map((v) => (spec.field in v ? v[spec.field] : null)).filter((v) => v !== null);
  if (raw.length === 0) return null;
  const numbers = raw.filter((v): v is number => typeof v === "number");
  switch (spec.aggregation) {
    case "sum":
      return numbers.reduce((a, b) => a + b, 0);
    case "avg":
      return numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : null;
    case "min":
      return raw.reduce((a, b) => (b! < a! ? b : a));
    case "max":
      return raw.reduce((a, b) => (b! > a! ? b : a));
    case "count":
      return raw.length;
    case "countd":
      return new Set(raw.map((v) => JSON.stringify(v))).size;
    default:
      // attr：所有值相同時為該值
      return raw.every((v) => v === raw[0]) ? raw[0] : null;
  }
}

/** 執行篩選動作：有選取時把標記傳給目標；清除時依動作的設定（保留篩選的動作不變）。 */
export function runActions(
  actions: Action[],
  marks: Mark[],
  current: Record<string, Mark[]>,
): { actions: Record<string, Mark[]>; navigate: string | null } {
  const next = { ...current };
  let navigate: string | null = null;
  for (const action of actions) {
    if (action.kind !== "filter") continue;
    const effective = action.single_select && marks.length > 1 ? [] : marks;
    if (effective.length === 0) {
      if (action.clearing !== "keep") delete next[action.id];
      continue;
    }
    next[action.id] = effective;
    if (action.target_dashboard) navigate = action.target_dashboard;
  }
  return { actions: next, navigate };
}

/** 點選標記後的選取：一般點選取代，按住 Ctrl／⌘／Shift 加入或移除。 */
export function nextSelection(current: Mark[], mark: Mark | null, additive: boolean): Mark[] {
  if (!mark) return [];
  if (!additive) return current.length === 1 && sameMark(current[0], mark) ? [] : [mark];
  return current.some((m) => sameMark(m, mark))
    ? current.filter((m) => !sameMark(m, mark))
    : [...current, mark];
}

export function fieldCaption(ir: IR, use: FieldUse): string {
  const field = ir.datasources.flatMap((d) => d.fields).find((f) => f.id === use.field);
  const name = field?.caption ?? use.field;
  return use.date ? `${use.date.unit.toUpperCase()}(${name})` : name;
}

export function displayScalar(value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "boolean") return value ? "True" : "False";
  return String(value);
}
