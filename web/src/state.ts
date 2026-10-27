import type { FieldUse, Filter, FilterValue, IR, QueryState, Scalar } from "./types";

/** 篩選狀態的欄位鍵，與查詢服務相同：f_region、year(f_order_date)、trunc_month(f_order_date)。 */
export function fieldKey(use: FieldUse): string {
  let key = use.field;
  if (use.date) {
    key = `${use.date.kind === "part" ? use.date.unit : `trunc_${use.date.unit}`}(${key})`;
  }
  return key;
}

export function findFilter(ir: IR, worksheet: string, key: string): Filter | undefined {
  const sheet = ir.worksheets.find((w) => w.id === worksheet);
  return sheet?.filters.find((f) => f.kind !== "top_n" && f.field.agg === null && fieldKey(f.field) === key);
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

export function stateFor(
  worksheet: string,
  sheetFilters: Record<string, Record<string, FilterValue>>,
  parameters: Record<string, Scalar>,
): QueryState {
  return { filters: sheetFilters[worksheet] ?? {}, parameters };
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
