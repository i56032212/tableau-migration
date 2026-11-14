// 資料源篩選（Tableau 資料來源分頁右上角的「篩選條件」）：逐列套用到這個資料源的所有工作表與控制項的可選值。
// 對話框與工作表的篩選共用（FilterDialog）：這裡給它一個只有篩選的工作表，套用時存成資料源篩選。

import { useEffect, useMemo, useState } from "react";
import { toast } from "../desktop";
import { editApi } from "./api";
import { EditorContext, type EditorContextValue, type FilterTarget } from "./context";
import { FilterDialog } from "./FilterDialog";
import { type Catalog, EDITOR_REF, fieldIndex, type FilterIR, type MarksIR, type SheetIR } from "./model";

const HOLDER = "__datasource_filters__";

function describe(filter: FilterIR): string {
  if (filter.kind === "categorical") {
    if (filter.mode === "all") return "全部";
    const values = (filter.values ?? []).map((v) => (v === null ? "Null" : String(v)));
    const shown = values.slice(0, 4).join("、") + (values.length > 4 ? `…等 ${values.length} 個` : "");
    return `${filter.mode === "exclude" ? "排除" : "保留"} ${shown}`;
  }
  if (filter.kind === "range") return `${filter.min ?? "…"} ～ ${filter.max ?? "…"}`;
  if (filter.kind === "relative_date") return "相對日期";
  if (filter.kind === "condition") return `條件：${String(filter.condition ?? "")}`;
  return filter.kind;
}

export function DatasourceFilters({ workbook, datasource, onChanged }: { workbook: string; datasource: string; onChanged: () => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [filters, setFilters] = useState<FilterIR[]>([]);
  const [target, setTarget] = useState<FilterTarget | null>(null);
  const [picking, setPicking] = useState("");
  useEffect(() => {
    let cancelled = false;
    Promise.all([editApi.catalog(workbook), editApi.datasourceFilters(workbook, datasource)])
      .then(([loaded, found]) => {
        if (cancelled) return;
        setCatalog(loaded);
        setFilters(found.filters);
      })
      .catch((e: Error) => !cancelled && toast(e.message, "error"));
    return () => {
      cancelled = true;
    };
  }, [workbook, datasource]);
  const fields = useMemo(() => (catalog ? fieldIndex(catalog, datasource) : new Map()), [catalog, datasource]);
  // 可以當作資料源篩選的欄位：這個資料源的維度（列層級；彙總、LOD 與 Table Calculation 不能逐列套用）
  const own = catalog?.datasources.find((d) => d.id === datasource)?.fields ?? [];
  const choices = own.filter((f) => f.role === "dimension" && !f.hidden && f.kind !== "set" && (f.level === null || f.level === "row"));
  const save = (next: FilterIR[]) =>
    editApi
      .saveDatasourceFilters(
        workbook,
        datasource,
        next.map((f) => ({ ...f, stage: "datasource", group: null })),
      )
      .then((saved) => {
        setFilters(saved.filters);
        onChanged();
      })
      .catch((e: Error) => toast(e.message, "error"));
  if (!catalog) return null;
  const sheet: SheetIR = {
    id: HOLDER,
    name: "",
    datasource,
    rows: [],
    columns: [],
    marks: { type: "automatic", color: null, size: null, label: [], detail: [], tooltip: [], shape: null, angle: null, path: null } as MarksIR,
    panes: [],
    measure_values: [],
    subtotals: [],
    filters,
    sorts: [],
    table_calcs: [],
    totals: [],
    fit: "standard",
    source_ref: EDITOR_REF,
  };
  const context: EditorContextValue = {
    workbook,
    sheet,
    catalog,
    fields,
    card: "all",
    setCard: () => undefined,
    apply: (change) => {
      void Promise.resolve(change(sheet)).then((next) => save(next.filters));
    },
    prepare: async (s, use) => [s, use],
    newId: (prefix, text) => editApi.newId(workbook, prefix, text),
    openFilter: setTarget,
    openCalc: () => undefined,
    openDialog: () => undefined,
    notify: (message, kind) => toast(message, kind === "error" ? "error" : "info"),
    reloadCatalog: () => undefined,
  };
  return (
    <section className="dse-filters" aria-label="資料源篩選">
      <div className="ds-grid-bar">
        <h2>資料源篩選（{filters.length}）</h2>
        <select
          aria-label="新增資料源篩選"
          value={picking}
          onChange={(e) => {
            const field = e.target.value;
            setPicking("");
            if (field) setTarget({ use: { field, agg: null, date: null, continuous: false, table_calc: null }, index: null });
          }}
        >
          <option value="">＋ 新增篩選…</option>
          {choices.map((f) => (
            <option key={f.id} value={f.id}>
              {f.caption}
            </option>
          ))}
        </select>
        <span className="muted">套用到使用這個資料源的所有工作表</span>
      </div>
      {filters.length > 0 && (
        <ul className="dse-filter-list">
          {filters.map((filter, index) => (
            <li key={filter.id} data-filter={filter.id}>
              <strong>{fields.get(filter.field.field)?.caption ?? filter.field.field}</strong>
              <span className="muted">{describe(filter)}</span>
              <button type="button" className="link" onClick={() => setTarget({ use: filter.field, index })}>
                編輯
              </button>
              <button type="button" className="link" onClick={() => void save(filters.filter((f) => f.id !== filter.id))}>
                移除
              </button>
            </li>
          ))}
        </ul>
      )}
      {target && (
        <EditorContext.Provider value={context}>
          <FilterDialog target={target} rowLevel onClose={() => setTarget(null)} />
        </EditorContext.Provider>
      )}
    </section>
  );
}
