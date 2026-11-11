import type {
  Action,
  DataField,
  FieldFilter,
  FieldUse,
  FilterValue,
  IR,
  Mark,
  MemberAlias,
  Parameter,
  QueryState,
  Scalar,
  SetValue,
} from "./types";

/** 檢視器的狀態：各工作表的篩選、參數、篩選動作的選取。 */
export interface ViewState {
  sheetFilters: Record<string, Record<string, FilterValue>>;
  parameters: Record<string, Scalar>;
  /** 篩選動作 id → 傳給目標工作表的標記（清除時保留篩選的動作，清除選取後仍留著） */
  actions: Record<string, Mark[]>;
  /** 來源工作表 id → 目前選取的標記（畫面上醒目顯示） */
  selections: Record<string, Mark[]>;
  /** 集合動作改變後的成員：集合 id → 成員（沒有列出的集合維持原本的成員） */
  sets?: Record<string, SetValue>;
  /** 相對日期篩選的基準日；null 表示由查詢服務決定（cases.yaml 的基準日或今天） */
  today?: string | null;
  /** 頁面架目前的頁：工作表 id → 頁面欄位的值（沒有列出的工作表為存檔時的頁） */
  pages?: Record<string, Scalar>;
  /** 醒目提示動作 id → 觸發它的工作表與標記（目標工作表中欄位值相同的標記醒目） */
  highlights?: Record<string, { sheet: string; marks: Mark[] }>;
  /** 醒目提示控制項：工作表 id → 欄位鍵 → 醒目提示的值 */
  highlighters?: Record<string, Record<string, Scalar[]>>;
  /** 階層的切入：工作表 id → 一組階層最上層的欄位鍵 → 切入的層數 */
  drills?: Record<string, Record<string, number>>;
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
      // Top N 篩選的控制項選的是排名前先選取的成員（篩選對話框的「一般」頁）
      f.kind !== "tuple" && f.field.agg === null && fieldKey(f.field) === key,
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
  const value: FilterValue = { mode: original.mode ?? "all", values: original.values ?? [] };
  if (original.ranges?.length) value.ranges = original.ranges;
  return value;
}

/** 值是否在連續的一段成員中（與查詢相同，依值的大小比較；Null 不在任何一段中）。 */
function inRange(v: Scalar, range: { start: Scalar; end: Scalar }): boolean {
  if (v === null || range.start === null || range.end === null) return false;
  return v >= range.start && v <= range.end;
}

export function selectedValues(value: FilterValue, domain: Scalar[]): Scalar[] {
  if (value.mode === "all" || value.mode === undefined) return domain;
  const listed = new Set((value.values ?? []).map((v) => JSON.stringify(v)));
  const ranges = value.ranges ?? [];
  const member = (v: Scalar) => listed.has(JSON.stringify(v)) || ranges.some((r) => inRange(v, r));
  if (value.mode === "include") return domain.filter(member);
  return domain.filter((v) => !member(v));
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
  if (view.pages && worksheet in view.pages) state.pages = { [worksheet]: view.pages[worksheet] };
  const drill = view.drills?.[worksheet];
  if (drill && Object.keys(drill).length > 0) state.drill = drill;
  // 集合動作：只帶這張工作表的資料源中的集合（其他資料源的集合不影響它）
  const sheet = ir.worksheets.find((w) => w.id === worksheet);
  const fields = new Set(ir.datasources.find((d) => d.id === sheet?.datasource)?.fields.map((f) => f.id) ?? []);
  const sets = Object.entries(view.sets ?? {}).filter(([id]) => fields.has(id));
  if (sets.length > 0) state.sets = Object.fromEntries(sets);
  return state;
}

/** 工具提示中的工作表（Viz in Tooltip）的查詢狀態：目標工作表自己的篩選與參數，加上來源工作表滑過的標記。 */
export function tooltipStateFor(ir: IR, source: string, target: string, view: ViewState, mark: Mark): QueryState {
  return { ...stateFor(ir, target, view), tooltip: { source, mark } };
}

/** 階層的切入：工作表上一組階層（以最上層的欄位鍵）切入 delta 層後的狀態；0 為存檔時的層數。 */
export function setDrill(view: ViewState, worksheet: string, key: string, delta: number): ViewState {
  const sheet = { ...(view.drills?.[worksheet] ?? {}) };
  if (delta === 0) delete sheet[key];
  else sheet[key] = delta;
  const drills = { ...(view.drills ?? {}) };
  if (Object.keys(sheet).length > 0) drills[worksheet] = sheet;
  else delete drills[worksheet];
  return { ...view, drills };
}

function setFieldOf(ir: IR, id: string): DataField | undefined {
  return ir.datasources.flatMap((d) => d.fields).find((f) => f.id === id && f.set?.rule.kind === "members");
}

function memberKey(row: Scalar[]): string {
  return JSON.stringify(row);
}

/**
 * 集合動作：選取標記時以標記在集合各層級的值取代、加入或移除成員；清除選取時保留、加入所有值或移除所有值。
 * 來源工作表沒有集合的層級時不改變。回傳更新後的成員（沒有變化時回傳原物件）。
 */
export function applySetActions(
  ir: IR,
  actions: Action[],
  marks: Mark[],
  current: Record<string, SetValue>,
): Record<string, SetValue> {
  let next = current;
  for (const action of actions) {
    if (action.kind !== "set" || !action.set_action) continue;
    const { target, behavior, clearing } = action.set_action;
    const field = setFieldOf(ir, target);
    if (!field?.set) continue;
    const effective = action.single_select && marks.length > 1 ? [] : marks;
    if (effective.length === 0) {
      if (clearing === "add_all") next = { ...next, [target]: { mode: "exclude", members: [] } };
      else if (clearing === "remove_all") next = { ...next, [target]: { mode: "include", members: [] } };
      continue;
    }
    const keys = field.set.levels.map((u) => fieldKey(u));
    const rows: Scalar[][] = [];
    for (const mark of effective) {
      if (!keys.every((k) => k in mark)) continue;
      const row = keys.map((k) => mark[k]);
      if (!rows.some((r) => memberKey(r) === memberKey(row))) rows.push(row);
    }
    if (rows.length === 0) continue;
    const rule = field.set.rule;
    const base: SetValue = next[target] ?? {
      mode: rule.mode === "exclude" ? "exclude" : "include",
      members: rule.members ?? [],
    };
    const picked = new Set(rows.map(memberKey));
    const without = base.members.filter((r) => !picked.has(memberKey(r)));
    const union = [...without, ...rows];
    let value: SetValue;
    if (behavior === "assign") value = { mode: "include", members: rows };
    else if ((behavior === "add") === (base.mode === "include")) value = { mode: base.mode, members: union };
    else value = { mode: base.mode, members: without };
    next = { ...next, [target]: value };
  }
  return next;
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

/** 醒目提示動作：觸發後的醒目提示（清除選取時移除）。回傳更新後的物件（沒有變化時回傳原物件）。 */
export function applyHighlights(
  actions: Action[],
  sheet: string,
  marks: Mark[],
  current: Record<string, { sheet: string; marks: Mark[] }>,
): Record<string, { sheet: string; marks: Mark[] }> {
  let next = current;
  for (const action of actions) {
    if (action.kind !== "highlight") continue;
    const effective = action.single_select && marks.length > 1 ? [] : marks;
    if (effective.length === 0) {
      if (action.id in next) {
        next = { ...next };
        delete next[action.id];
      }
    } else if (JSON.stringify(next[action.id]) !== JSON.stringify({ sheet, marks: effective })) {
      next = { ...next, [action.id]: { sheet, marks: effective } };
    }
  }
  return next;
}

/** 欄位鍵 → 欄位 id 與日期的包裝（year(f)、trunc_month(f)）。 */
function splitKey(key: string): { field: string; wrap: (id: string) => string } {
  const match = /^([a-z_]+)\((.+)\)$/.exec(key);
  if (!match) return { field: key, wrap: (id) => id };
  return { field: match[2], wrap: (id) => `${match[1]}(${id})` };
}

/**
 * 目標工作表上要醒目的標記（欄位鍵為目標工作表的鍵）：醒目提示動作與醒目提示控制項。
 * 欄位依顯示名稱對應（與 Tableau 相同，不同資料源的同名欄位也算）；動作只比對選定的欄位。
 * 沒有醒目提示時為 null；有醒目提示、但這張工作表沒有任何相同的欄位時不影響它（也是 null）。
 */
export function highlightsFor(ir: IR, worksheet: string, view: ViewState): Mark[] | null {
  const target = ir.worksheets.find((w) => w.id === worksheet);
  if (!target) return null;
  const fieldsOf = (sheetId: string) => {
    const sheet = ir.worksheets.find((w) => w.id === sheetId);
    return ir.datasources.find((d) => d.id === sheet?.datasource)?.fields ?? [];
  };
  const targetFields = fieldsOf(worksheet);
  const byCaption = new Map(targetFields.map((f) => [f.caption, f.id]));
  const result: Mark[] = [];
  for (const action of ir.actions ?? []) {
    const fired = view.highlights?.[action.id];
    if (action.kind !== "highlight" || !fired || !action.targets.includes(worksheet)) continue;
    const sourceFields = new Map(fieldsOf(fired.sheet).map((f) => [f.id, f.caption]));
    const wanted = new Set(action.highlight?.fields ?? []);
    for (const mark of fired.marks) {
      const mapped: Mark = {};
      for (const [key, value] of Object.entries(mark)) {
        const { field, wrap } = splitKey(key);
        const caption = sourceFields.get(field);
        if (caption === undefined || (wanted.size > 0 && !wanted.has(caption))) continue;
        const id = byCaption.get(caption);
        if (id !== undefined) mapped[wrap(id)] = value;
      }
      if (Object.keys(mapped).length > 0) result.push(mapped);
    }
  }
  for (const [key, values] of Object.entries(view.highlighters?.[worksheet] ?? {})) {
    for (const value of values) result.push({ [key]: value });
  }
  return result.length > 0 ? result : null;
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

function aliasOf(aliases: MemberAlias[] | undefined, value: Scalar): string | undefined {
  return aliases?.find((a) => a.value === value)?.label;
}

/** 篩選控制項中維度成員的顯示名稱：成員別名只用於維度的原始成員（沒有日期衍生與彙總）。 */
export function memberLabel(ir: IR, use: FieldUse, value: Scalar): string {
  if (use.date || use.agg || use.table_calc) return displayScalar(value);
  const field = ir.datasources.flatMap((d) => d.fields).find((f) => f.id === use.field);
  return aliasOf(field?.aliases, value) ?? displayScalar(value);
}

/** 參數值的顯示名稱（Tableau 的「顯示為」）。 */
export function parameterLabel(parameter: Parameter, value: Scalar): string {
  return aliasOf(parameter.aliases, value) ?? displayScalar(value);
}
