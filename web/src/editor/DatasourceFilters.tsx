// 資料源篩選（Tableau 資料來源分頁右上角的「篩選條件」）：逐列套用到這個資料源的所有工作表與控制項的可選值。
// 對話框與工作表的篩選共用（FilterDialog）：這裡給它一個只有篩選的工作表，套用時存成資料源篩選。

import { useEffect, useMemo, useState } from "react";
import { toast } from "../desktop";
import { editApi, type ExtractInfo } from "./api";
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

/** stage：datasource＝資料源篩選；extract＝萃取篩選（建立、更新萃取時套用在來源的資料上，下次更新才生效）。 */
export function DatasourceFilters({
  workbook,
  datasource,
  onChanged,
  stage = "datasource",
}: {
  workbook: string;
  datasource: string;
  onChanged: () => void;
  stage?: "datasource" | "extract";
}) {
  const title = stage === "extract" ? "萃取篩選" : "資料源篩選";
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [filters, setFilters] = useState<FilterIR[]>([]);
  const [target, setTarget] = useState<FilterTarget | null>(null);
  const [picking, setPicking] = useState("");
  useEffect(() => {
    let cancelled = false;
    Promise.all([editApi.catalog(workbook), editApi.datasourceFilters(workbook, datasource, stage)])
      .then(([loaded, found]) => {
        if (cancelled) return;
        setCatalog(loaded);
        setFilters(found.filters);
      })
      .catch((e: Error) => !cancelled && toast(e.message, "error"));
    return () => {
      cancelled = true;
    };
  }, [workbook, datasource, stage]);
  const fields = useMemo(() => (catalog ? fieldIndex(catalog, datasource) : new Map()), [catalog, datasource]);
  // 可以當作資料源篩選的欄位：這個資料源的維度（列層級；彙總、LOD 與 Table Calculation 不能逐列套用）
  const own = catalog?.datasources.find((d) => d.id === datasource)?.fields ?? [];
  const choices = own.filter((f) => f.role === "dimension" && !f.hidden && f.kind !== "set" && (f.level === null || f.level === "row"));
  const save = (next: FilterIR[]) =>
    editApi
      .saveDatasourceFilters(
        workbook,
        datasource,
        next.map((f) => ({ ...f, stage, group: null })),
        stage,
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
    refreshCatalog: () => Promise.resolve(fields),
    reloadSheet: () => undefined,
  };
  return (
    <section className="dse-filters" aria-label={title}>
      <div className="ds-grid-bar">
        <h2>
          {title}（{filters.length}）
        </h2>
        <select
          aria-label={`新增${title}`}
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
        <span className="muted">{stage === "extract" ? "下次更新萃取時套用在來源的資料上" : "套用到使用這個資料源的所有工作表"}</span>
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
          <FilterDialog target={target} rowLevel fromSource={stage === "extract"} onClose={() => setTarget(null)} />
        </EditorContext.Provider>
      )}
    </section>
  );
}

/**
 * 即時與萃取（Tableau 資料來源分頁右上角的「連線：即時／萃取」與萃取的設定）：切換模式（改成萃取時立即從來源
 * 建立資料）、增量更新的欄位、萃取篩選與「立即更新萃取」。
 */
export function ExtractSettings({ workbook, datasource, onChanged }: { workbook: string; datasource: string; onChanged: () => void }) {
  const [info, setInfo] = useState<ExtractInfo | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    editApi
      .extract(workbook, datasource)
      .then((found) => !cancelled && setInfo(found))
      .catch((e: Error) => !cancelled && toast(e.message, "error"));
    return () => {
      cancelled = true;
    };
  }, [workbook, datasource]);
  if (!info) return null;
  const run = (label: string, action: () => Promise<ExtractInfo>, done: string) => {
    setBusy(label);
    action()
      .then((next) => {
        setInfo(next);
        toast(done);
        onChanged();
      })
      .catch((e: Error) => toast(e.message, "error"))
      .finally(() => setBusy(null));
  };
  const switchTo = (mode: "live" | "extract") => {
    if (mode === info.mode) return;
    run(
      mode === "extract" ? "正在從來源建立萃取…" : "正在改成即時連線…",
      () => editApi.extractMode(workbook, datasource, mode),
      mode === "extract" ? "已建立萃取" : "已改成即時連線",
    );
  };
  return (
    <section className="dse-extract" aria-label="連線模式" data-mode={info.mode} data-busy={busy ? "true" : "false"}>
      <div className="ds-grid-bar">
        <h2>連線</h2>
        {info.mode === "file" ? (
          <span className="muted">檔案型的資料源一律匯入</span>
        ) : (
          <>
            <label className="inline">
              <input type="radio" name="extract-mode" checked={info.mode === "live"} disabled={!info.switchable || busy !== null} onChange={() => switchTo("live")} /> 即時
            </label>
            <label className="inline">
              <input type="radio" name="extract-mode" checked={info.mode === "extract"} disabled={!info.switchable || busy !== null} onChange={() => switchTo("extract")} /> 萃取
            </label>
            {!info.switchable && <span className="muted">{info.reason}</span>}
          </>
        )}
        {busy && <span className="muted">{busy}</span>}
      </div>
      {info.mode === "extract" && info.refreshable && (
        <div className="dse-extract-row">
          <label className="inline" title="增量更新時只取這個欄位大於現有最大值的列，附加在現有的資料之後">
            增量更新的欄位
            <select
              aria-label="增量更新的欄位"
              value={info.increment ?? ""}
              disabled={busy !== null}
              onChange={(e) => {
                const increment = e.target.value || null;
                run("儲存中…", () => editApi.saveExtract(workbook, datasource, increment), "已儲存萃取設定");
              }}
            >
              <option value="">（無：只能完整更新）</option>
              {info.increment_fields.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.caption}
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={busy !== null} onClick={() => run("正在更新萃取…", () => editApi.refreshExtract(workbook, datasource, false), "已更新萃取")}>
            立即更新萃取
          </button>
          {info.increment && (
            <button type="button" disabled={busy !== null} onClick={() => run("正在增量更新…", () => editApi.refreshExtract(workbook, datasource, true), "已增量更新萃取")}>
              增量更新
            </button>
          )}
          <span className="muted">上次更新：{info.refreshed ? new Date(info.refreshed).toLocaleString() : "（取自報表中的萃取）"}</span>
        </div>
      )}
      {info.mode === "extract" && info.refreshable && <DatasourceFilters workbook={workbook} datasource={datasource} stage="extract" onChanged={() => undefined} />}
      {info.mode === "extract" && !info.refreshable && info.filters.length > 0 && (
        <p className="muted">這份萃取有 {info.filters.length} 個萃取篩選（已在建立萃取時套用；來源不明，不能更新）。</p>
      )}
    </section>
  );
}
