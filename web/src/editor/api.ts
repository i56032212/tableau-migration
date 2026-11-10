// 桌面版編輯器的 API（/api/edit）：資料窗格、工作表、計算欄位與篩選的可選值。

import { request } from "../api";
import type { Domain, FieldUse } from "../types";
import type { Catalog, CatalogField, SheetIR } from "./model";

const edit = (workbook: string) => `/api/edit/${encodeURIComponent(workbook)}`;
const part = encodeURIComponent;

export interface FormulaCheck {
  ok: boolean;
  datatype?: string;
  level?: string;
  role?: "dimension" | "measure";
  message?: string;
}

export const editApi = {
  catalog: (workbook: string) => request<Catalog>(`${edit(workbook)}/catalog`),
  worksheet: (workbook: string, worksheet: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}`).then((r) => r.worksheet),
  saveWorksheet: (workbook: string, sheet: SheetIR) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(sheet.id)}`, {
      method: "PUT",
      body: JSON.stringify({ worksheet: sheet }),
    }).then((r) => r.worksheet),
  addWorksheet: (workbook: string, name?: string, datasource?: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets`, {
      method: "POST",
      body: JSON.stringify({ name: name ?? null, datasource: datasource ?? null }),
    }).then((r) => r.worksheet),
  duplicateWorksheet: (workbook: string, worksheet: string) =>
    request<{ worksheet: SheetIR }>(`${edit(workbook)}/worksheets/${part(worksheet)}/duplicate`, {
      method: "POST",
      body: "{}",
    }).then((r) => r.worksheet),
  removeWorksheet: (workbook: string, worksheet: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/worksheets/${part(worksheet)}`, { method: "DELETE" }),
  checkFormula: (workbook: string, datasource: string, formula: string, field?: string | null) =>
    request<FormulaCheck>(`${edit(workbook)}/formula`, {
      method: "POST",
      body: JSON.stringify({ datasource, formula, field: field ?? null }),
    }),
  saveCalc: (workbook: string, datasource: string, caption: string, formula: string, field?: string | null) =>
    request<{ field: CatalogField }>(`${edit(workbook)}/calcs`, {
      method: "POST",
      body: JSON.stringify({ datasource, caption, formula, field: field ?? null }),
    }).then((r) => r.field),
  removeCalc: (workbook: string, datasource: string, field: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/datasources/${part(datasource)}/calcs/${part(field)}`, {
      method: "DELETE",
    }),
  saveParameter: (workbook: string, parameter: Record<string, unknown>) =>
    request<{ parameter: Record<string, unknown> }>(`${edit(workbook)}/parameters`, {
      method: "POST",
      body: JSON.stringify({ parameter }),
    }),
  removeParameter: (workbook: string, parameter: string) =>
    request<{ ok: boolean }>(`${edit(workbook)}/parameters/${part(parameter)}`, { method: "DELETE" }),
  newId: (workbook: string, prefix: string, text: string) =>
    request<{ id: string }>(`${edit(workbook)}/ids`, {
      method: "POST",
      body: JSON.stringify({ prefix, text }),
    }).then((r) => r.id),
  /** 篩選對話框：維度的值或範圍；量值為工作表上各標記的範圍（不套用 excludeFilter 這個篩選） */
  domain: (
    workbook: string,
    use: FieldUse,
    options: { kind?: "values" | "range"; worksheet?: string; excludeFilter?: string | null } = {},
  ) =>
    request<Domain>(`${edit(workbook)}/domain`, {
      method: "POST",
      body: JSON.stringify({
        use,
        kind: options.kind ?? null,
        worksheet: options.worksheet ?? null,
        exclude_filter: options.excludeFilter ?? null,
      }),
    }),
};
