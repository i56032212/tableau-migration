import type { Action, FieldFilter, FieldUse, FilterValue, IR, Mark, QueryState, Scalar } from "./types";

/** 檢視器的狀態：各工作表的篩選、參數、篩選動作的選取。 */
export interface ViewState {
  sheetFilters: Record<string, Record<string, FilterValue>>;
  parameters: Record<string, Scalar>;
  /** 篩選動作 id → 傳給目標工作表的標記（清除時保留篩選的動作，清除選取後仍留著） */
  actions: Record<string, Mark[]>;
  /** 來源工作表 id → 目前選取的標記（畫面上醒目顯示） */
  selections: Record<string, Mark[]>;
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
  return { filters: view.sheetFilters[worksheet] ?? {}, parameters: view.parameters, actions };
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

/** 執行篩選動作：有選取時把標記傳給目標；清除時依動作的設定（保留篩選的動作不變）。 */
export function runActions(
  actions: Action[],
  marks: Mark[],
  current: Record<string, Mark[]>,
): { actions: Record<string, Mark[]>; navigate: string | null } {
  const next = { ...current };
  let navigate: string | null = null;
  for (const action of actions) {
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
