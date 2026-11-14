// 篩選對話框：類別（一般、條件、頂部）、範圍（量值、連續的數字與日期）、相對日期。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi } from "./api";
import { type FilterTarget, useEditor } from "./context";
import {
  AGGREGATIONS,
  type CatalogField,
  EDITOR_REF,
  type FilterIR,
  formulaRef,
  isDate,
  isMeasure,
  isNumeric,
  measureFormula,
  pillLabel,
  type SheetIR,
} from "./model";

type Mode = "values" | "range" | "relative";
type Tab = "general" | "wildcard" | "condition" | "top";

/** 萬用字元的比對方式（Tableau 篩選對話框的「萬用字元」頁）。 */
const WILDCARDS: [string, string, (ref: string, text: string) => string][] = [
  ["contains", "包含", (ref, text) => `CONTAINS(LOWER(${ref}), LOWER(${text}))`],
  ["starts", "開頭為", (ref, text) => `STARTSWITH(LOWER(${ref}), LOWER(${text}))`],
  ["ends", "結尾為", (ref, text) => `ENDSWITH(LOWER(${ref}), LOWER(${text}))`],
  ["exact", "完全符合", (ref, text) => `LOWER(${ref}) = LOWER(${text})`],
];

const quote = (text: string) => `'${text.replace(/'/g, "''")}'`;

/** 萬用字元篩選的條件公式（與 Tableau 寫進 .twb 的相同）：不分大小寫；排除時 Null 也留下。 */
export function wildcardFormula(ref: string, kind: string, text: string, exclude: boolean): string {
  const make = WILDCARDS.find(([key]) => key === kind)?.[2] ?? WILDCARDS[0][2];
  const formula = make(ref, quote(text));
  return exclude ? `ISNULL(${ref}) OR (NOT(${formula}))` : formula;
}

/** 條件公式是否為萬用字元篩選（這個欄位的）：比對方式、文字與是否排除。 */
export function parseWildcard(condition: string, ref: string): { kind: string; text: string; exclude: boolean } | null {
  for (const exclude of [false, true]) {
    for (const [kind] of WILDCARDS) {
      const [head, tail] = wildcardFormula(ref, kind, "\u0000", exclude).split("'\u0000'");
      if (condition.startsWith(head) && condition.endsWith(tail) && condition.length >= head.length + tail.length + 2) {
        const literal = condition.slice(head.length, condition.length - tail.length);
        if (literal.startsWith("'") && literal.endsWith("'")) return { kind, text: literal.slice(1, -1).replace(/''/g, "'"), exclude };
      }
    }
  }
  return null;
}

/** 日期欄位的篩選方式（Tableau 把日期拖到篩選架時先問的那一步）。 */
const DATE_WAYS: [string, string, FieldUse["date"], Mode][] = [
  ["relative", "相對日期", null, "relative"],
  ["range", "日期範圍", null, "range"],
  ["year", "年", { kind: "part", unit: "year" }, "values"],
  ["quarter", "季", { kind: "part", unit: "quarter" }, "values"],
  ["month", "月", { kind: "part", unit: "month" }, "values"],
  ["weekday", "星期", { kind: "part", unit: "weekday" }, "values"],
  ["day", "日", { kind: "part", unit: "day" }, "values"],
  ["trunc_month", "年月", { kind: "trunc", unit: "month" }, "values"],
  ["trunc_day", "個別日期", { kind: "trunc", unit: "day" }, "values"],
];

const PERIODS: [string, string][] = [
  ["year", "年"],
  ["quarter", "季"],
  ["month", "月"],
  ["week", "週"],
  ["day", "日"],
];

const OPERATORS = [">", ">=", "=", "<=", "<", "<>"];

function display(value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "boolean") return value ? "真" : "假";
  return String(value);
}

function initialMode(filter: FilterIR | null, use: FieldUse, field: CatalogField | undefined, measure: boolean): Mode {
  if (filter) return filter.kind === "range" ? "range" : filter.kind === "relative_date" ? "relative" : "values";
  if (measure) return "range";
  if (isDate(field) && use.date === null) return "range";
  if (use.continuous && (isNumeric(field) || isDate(field))) return "range";
  return "values";
}

function dateWayOf(use: FieldUse, mode: Mode): string {
  if (mode === "relative") return "relative";
  if (mode === "range") return "range";
  const found = DATE_WAYS.find(([, , date]) => date && use.date && date.kind === use.date.kind && date.unit === use.date.unit);
  return found ? found[0] : "year";
}

export function FilterDialog({ target, onClose }: { target: FilterTarget; onClose: () => void }) {
  const editor = useEditor();
  const { sheet, fields, workbook } = editor;
  const existing = target.index !== null ? (sheet.filters[target.index] ?? null) : null;
  const [use, setUse] = useState<FieldUse>(existing?.field ?? target.use);
  const field = fields.get(use.field);
  const measure = isMeasure(use, fields);
  const [mode, setMode] = useState<Mode>(() => initialMode(existing, use, field, measure));
  // 萬用字元：字串欄位（不是日期的部分）
  const textual = field?.datatype === "string" && use.date === null && !measure;
  const wildcard = existing?.kind === "condition" && field && textual ? parseWildcard(String(existing.condition ?? ""), formulaRef(field)) : null;
  const [tab, setTab] = useState<Tab>(wildcard ? "wildcard" : existing?.kind === "condition" ? "condition" : existing?.kind === "top_n" ? "top" : "general");
  const [wildKind, setWildKind] = useState(wildcard?.kind ?? "contains");
  const [wildText, setWildText] = useState(wildcard?.text ?? "");
  const [wildExclude, setWildExclude] = useState(wildcard?.exclude ?? false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // 一般：可選的值與勾選的值
  const [domain, setDomain] = useState<Scalar[] | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [exclude, setExclude] = useState(existing?.kind === "categorical" && existing.mode === "exclude");
  // 開啟報表時篩選到最新的值（日期的類別篩選）
  const [latest, setLatest] = useState(existing?.kind === "categorical" && existing.latest === true);
  const [search, setSearch] = useState("");
  // 範圍
  const [low, setLow] = useState<string>(existing?.kind === "range" && existing.min !== null && existing.min !== undefined ? String(existing.min) : "");
  const [high, setHigh] = useState<string>(existing?.kind === "range" && existing.max !== null && existing.max !== undefined ? String(existing.max) : "");
  const [bounds, setBounds] = useState<{ min: Scalar; max: Scalar } | null>(null);
  const [includeNull, setIncludeNull] = useState(Boolean(existing?.include_null));
  // 相對日期
  const [period, setPeriod] = useState<string>((existing?.period as string | undefined) ?? "month");
  const [relative, setRelative] = useState<string>(() => {
    if (existing?.kind !== "relative_date") return "last_n";
    const first = Number(existing.first);
    const last = Number(existing.last);
    if (first === -1 && last === -1) return "previous";
    if (first === 0 && last === 0) return existing.include_future === false ? "to_date" : "current";
    if (first === 1 && last === 1) return "next";
    if (last === 0) return "last_n";
    return "next_n";
  });
  const [count, setCount] = useState<number>(() => {
    if (existing?.kind !== "relative_date") return 3;
    const first = Number(existing.first);
    const last = Number(existing.last);
    return last === 0 ? 1 - first : last - first + 1;
  });
  const [anchor, setAnchor] = useState<string>((existing?.anchor as string | null | undefined) ?? "");
  // 條件
  const conditionText = existing?.kind === "condition" ? String(existing.condition ?? "") : "";
  const [conditionKind, setConditionKind] = useState<"field" | "formula">(conditionText ? "formula" : "field");
  const [condition, setCondition] = useState(conditionText);
  const measures = useMemo(
    () => [...fields.values()].filter((f) => !f.hidden && (f.role === "measure" || f.level === "aggregate") && f.level !== "table_calc"),
    [fields],
  );
  const [condField, setCondField] = useState<string>(measures[0]?.id ?? "");
  const [condAgg, setCondAgg] = useState("sum");
  const [condOp, setCondOp] = useState(">");
  const [condValue, setCondValue] = useState("0");
  // 頂部
  const [topDirection, setTopDirection] = useState<"top" | "bottom">((existing?.direction as "top" | "bottom" | undefined) ?? "top");
  const [topCount, setTopCount] = useState<number>(typeof existing?.count === "number" ? existing.count : 10);
  const [topBy, setTopBy] = useState<string>(existing?.kind === "top_n" ? String(existing.by ?? "") : "");

  const fieldName = pillLabel(use, fields, sheet.table_calcs);

  useEffect(() => {
    if (mode !== "values") return;
    let cancelled = false;
    setDomain(null);
    editApi
      .domain(workbook, use, { kind: "values" })
      .then((d) => {
        if (cancelled || d.kind !== "values") return;
        setDomain(d.values);
        const keys = d.values.map((v) => JSON.stringify(v));
        if (existing?.kind === "categorical" && JSON.stringify(existing.field) === JSON.stringify(use)) {
          const chosen = new Set((existing.values ?? []).map((v) => JSON.stringify(v)));
          setChecked(existing.mode === "all" ? new Set(keys) : chosen);
        } else {
          setChecked(new Set(keys));
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
    // existing 只在開啟時讀一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workbook, JSON.stringify(use), mode]);

  useEffect(() => {
    if (mode !== "range") return;
    let cancelled = false;
    editApi
      .domain(workbook, use, { kind: "range", worksheet: sheet.id, excludeFilter: existing?.id ?? null })
      .then((d) => {
        if (cancelled || d.kind !== "range") return;
        setBounds({ min: d.min, max: d.max });
        if (!existing || existing.kind !== "range") {
          setLow(d.min === null ? "" : String(d.min));
          setHigh(d.max === null ? "" : String(d.max));
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workbook, JSON.stringify(use), mode]);

  const dateInput = isDate(field) && use.date === null && !measure;
  const parse = (text: string): Scalar => {
    if (text.trim() === "") return null;
    if (dateInput) return text.slice(0, 10);
    const n = Number(text);
    return Number.isFinite(n) ? n : text;
  };

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const id = existing?.id ?? (await editor.newId("flt", field?.caption ?? use.field));
      const context = existing?.stage === "context";
      // 套用到其他工作表的篩選（同一個群組）改了內容仍是同一個篩選
      const group = (existing?.group as string | null | undefined) ?? null;
      let filter: FilterIR;
      if (mode === "range") {
        const stage = measure ? (use.table_calc !== null || field?.level === "table_calc" ? "table_calc" : "measure") : context ? "context" : "dimension";
        filter = { kind: "range", id, field: use, stage, min: parse(low), max: parse(high), include_null: includeNull, group, source_ref: EDITOR_REF };
      } else if (mode === "relative") {
        const n = Math.max(1, Math.floor(count));
        const [first, last] =
          relative === "previous" ? [-1, -1] : relative === "next" ? [1, 1] : relative === "last_n" ? [1 - n, 0] : relative === "next_n" ? [0, n - 1] : [0, 0];
        filter = {
          kind: "relative_date",
          id,
          field: { ...use, date: null, continuous: true, agg: null, table_calc: null },
          stage: context ? "context" : "dimension",
          period,
          first,
          last,
          include_future: relative !== "to_date",
          include_null: false,
          anchor: anchor || null,
          group,
          source_ref: EDITOR_REF,
        };
      } else if (tab === "wildcard") {
        if (!field || !wildText) throw new Error("請輸入比對的文字");
        filter = {
          kind: "condition",
          id,
          field: use,
          stage: context ? "context" : "dimension",
          condition: wildcardFormula(formulaRef(field), wildKind, wildText, wildExclude),
          mode: "all",
          values: [],
          group,
          source_ref: EDITOR_REF,
        };
      } else if (tab === "condition") {
        const formula =
          conditionKind === "formula"
            ? condition.trim()
            : (() => {
                const f = fields.get(condField);
                if (!f) return "";
                const ref = f.level === "aggregate" ? formulaRef(f) : `${condAgg.toUpperCase()}(${formulaRef(f)})`;
                return `${ref} ${condOp} ${condValue.trim() || "0"}`;
              })();
        if (!formula) throw new Error("請輸入條件");
        filter = { kind: "condition", id, field: use, stage: context ? "context" : "dimension", condition: formula, mode: "all", values: [], group, source_ref: EDITOR_REF };
      } else if (tab === "top") {
        const by = topBy.trim();
        if (!by) throw new Error("請選擇排名的依據");
        filter = {
          kind: "top_n",
          id,
          field: use,
          stage: context ? "context" : "top_n",
          direction: topDirection,
          count: Math.max(1, Math.floor(topCount)),
          by,
          mode: "all",
          values: [],
          group,
          source_ref: EDITOR_REF,
        };
      } else {
        const all = domain ?? [];
        const values = all.filter((v) => checked.has(JSON.stringify(v)));
        const everything = values.length === all.length && !exclude;
        const stage = measure ? (use.table_calc !== null || field?.level === "table_calc" ? "table_calc" : "measure") : context ? "context" : "dimension";
        filter = {
          kind: "categorical",
          id,
          field: use,
          stage,
          mode: everything ? "all" : exclude ? "exclude" : "include",
          values: everything ? [] : values,
          ranges: [],
          group,
          ...(latest && isDate(field) && !measure ? { latest: true } : {}),
          source_ref: EDITOR_REF,
        };
      }
      const index = target.index;
      editor.apply((s: SheetIR) => ({
        ...s,
        filters: index === null ? [...s.filters, filter] : s.filters.map((f, i) => (i === index ? filter : f)),
      }));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const shown = (domain ?? []).filter((v) => !search || display(v).toLowerCase().includes(search.toLowerCase()));
  const topMeasures = measures.map((f) => ({ id: f.id, formula: measureFormula({ field: f.id, agg: f.level === "aggregate" ? null : (f.default_agg ?? "sum"), date: null, continuous: true, table_calc: null }, fields) }));

  return (
    <Modal title={`篩選［${fieldName}］`} onClose={onClose} wide>
      <div className="filter-dialog">
        {isDate(field) && !measure && (
          <label className="inline">
            篩選方式
            <select
              aria-label="篩選方式"
              value={dateWayOf(use, mode)}
              onChange={(e) => {
                const way = DATE_WAYS.find(([key]) => key === e.target.value);
                if (!way) return;
                const [, , date, nextMode] = way;
                setMode(nextMode);
                setUse({ ...use, date, continuous: nextMode !== "values" ? true : false, agg: null });
              }}
            >
              {DATE_WAYS.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}

        {mode === "values" && (
          <>
            <div className="tabs" role="tablist">
              {(
                [
                  ["general", "一般"],
                  ...(textual ? [["wildcard", "萬用字元"]] : []),
                  ["condition", "條件"],
                  ["top", "頂部"],
                ] as [Tab, string][]
              ).map(([key, label]) => (
                <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>
                  {label}
                </button>
              ))}
            </div>
            {tab === "wildcard" && (
              <div className="filter-condition">
                <label className="inline">
                  比對的文字
                  <input aria-label="比對的文字" value={wildText} onChange={(e) => setWildText(e.target.value)} />
                </label>
                <label className="inline">
                  比對方式
                  <select aria-label="比對方式" value={wildKind} onChange={(e) => setWildKind(e.target.value)}>
                    {WILDCARDS.map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="inline">
                  <input type="checkbox" checked={wildExclude} onChange={(e) => setWildExclude(e.target.checked)} /> 排除
                </label>
                <p className="hint">不分大小寫；排除時沒有值（Null）的資料列也留下。</p>
              </div>
            )}
            {tab === "general" && (
              <div className="filter-values">
                <div className="filter-tools">
                  <input type="search" placeholder="搜尋" aria-label="搜尋值" value={search} onChange={(e) => setSearch(e.target.value)} />
                  <button type="button" onClick={() => setChecked(new Set((domain ?? []).map((v) => JSON.stringify(v))))}>
                    全部
                  </button>
                  <button type="button" onClick={() => setChecked(new Set())}>
                    無
                  </button>
                  <label>
                    <input type="checkbox" checked={exclude} onChange={(e) => setExclude(e.target.checked)} /> 排除
                  </label>
                </div>
                {domain === null && !error && <p className="hint">載入中…</p>}
                <ul className="value-list" aria-label="值">
                  {shown.slice(0, 2000).map((v) => {
                    const key = JSON.stringify(v);
                    return (
                      <li key={key}>
                        <label>
                          <input
                            type="checkbox"
                            checked={checked.has(key)}
                            onChange={(e) => {
                              const next = new Set(checked);
                              if (e.target.checked) next.add(key);
                              else next.delete(key);
                              setChecked(next);
                            }}
                          />{" "}
                          {display(v)}
                        </label>
                      </li>
                    );
                  })}
                </ul>
                {shown.length > 2000 && <p className="hint">只列出前 2000 個值，請以搜尋縮小範圍</p>}
                {domain && (
                  <p className="hint">
                    已選取 {checked.size} / {domain.length} 個值
                  </p>
                )}
                {isDate(field) && !measure && (
                  <label>
                    <input type="checkbox" checked={latest} onChange={(e) => setLatest(e.target.checked)} /> 開啟報表時篩選到最新的值
                  </label>
                )}
              </div>
            )}
            {tab === "condition" && (
              <div className="filter-condition">
                <label>
                  <input type="radio" checked={conditionKind === "field"} onChange={() => setConditionKind("field")} /> 依欄位
                </label>
                <div className="row">
                  <select aria-label="條件的欄位" value={condField} onChange={(e) => setCondField(e.target.value)} disabled={conditionKind !== "field"}>
                    {measures.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.caption}
                      </option>
                    ))}
                  </select>
                  <select aria-label="條件的彙總" value={condAgg} onChange={(e) => setCondAgg(e.target.value)} disabled={conditionKind !== "field"}>
                    {AGGREGATIONS.filter(([a]) => a !== "attr").map(([a, label]) => (
                      <option key={a} value={a}>
                        {label}
                      </option>
                    ))}
                  </select>
                  <select aria-label="比較" value={condOp} onChange={(e) => setCondOp(e.target.value)} disabled={conditionKind !== "field"}>
                    {OPERATORS.map((op) => (
                      <option key={op}>{op}</option>
                    ))}
                  </select>
                  <input aria-label="條件的值" value={condValue} onChange={(e) => setCondValue(e.target.value)} disabled={conditionKind !== "field"} />
                </div>
                <label>
                  <input type="radio" checked={conditionKind === "formula"} onChange={() => setConditionKind("formula")} /> 依公式
                </label>
                <textarea
                  aria-label="條件公式"
                  className="formula"
                  rows={3}
                  value={condition}
                  placeholder="例如 SUM([Sales]) > 1000"
                  onChange={(e) => setCondition(e.target.value)}
                  disabled={conditionKind !== "formula"}
                />
              </div>
            )}
            {tab === "top" && (
              <div className="filter-top row">
                <select aria-label="頂部或底部" value={topDirection} onChange={(e) => setTopDirection(e.target.value as "top" | "bottom")}>
                  <option value="top">頂部</option>
                  <option value="bottom">底部</option>
                </select>
                <input type="number" min={1} aria-label="數量" value={topCount} onChange={(e) => setTopCount(Number(e.target.value))} />
                依
                <select aria-label="排名依據" value={topBy} onChange={(e) => setTopBy(e.target.value)}>
                  <option value="">（選擇量值）</option>
                  {topMeasures.map((m) =>
                    m.formula ? (
                      <option key={m.id} value={m.formula}>
                        {m.formula}
                      </option>
                    ) : null,
                  )}
                  {topBy && !topMeasures.some((m) => m.formula === topBy) && <option value={topBy}>{topBy}</option>}
                </select>
              </div>
            )}
          </>
        )}

        {mode === "range" && (
          <div className="filter-range">
            <div className="row">
              <label>
                最小值
                <input type={dateInput ? "date" : "text"} aria-label="最小值" value={dateInput ? low.slice(0, 10) : low} onChange={(e) => setLow(e.target.value)} />
              </label>
              <label>
                最大值
                <input type={dateInput ? "date" : "text"} aria-label="最大值" value={dateInput ? high.slice(0, 10) : high} onChange={(e) => setHigh(e.target.value)} />
              </label>
            </div>
            {bounds && (
              <p className="hint">
                資料的範圍：{display(bounds.min)} 到 {display(bounds.max)}（留空表示不限）
              </p>
            )}
            <label>
              <input type="checkbox" checked={includeNull} onChange={(e) => setIncludeNull(e.target.checked)} /> 包含 Null 值
            </label>
          </div>
        )}

        {mode === "relative" && (
          <div className="filter-relative">
            <div className="row">
              {PERIODS.map(([key, label]) => (
                <label key={key}>
                  <input type="radio" name="period" checked={period === key} onChange={() => setPeriod(key)} /> {label}
                </label>
              ))}
            </div>
            <div className="column">
              {(
                [
                  ["previous", "上一期"],
                  ["current", "本期"],
                  ["next", "下一期"],
                  ["to_date", "本期至今"],
                ] as [string, string][]
              ).map(([key, label]) => (
                <label key={key}>
                  <input type="radio" name="relative" checked={relative === key} onChange={() => setRelative(key)} /> {label}
                </label>
              ))}
              <label>
                <input type="radio" name="relative" checked={relative === "last_n"} onChange={() => setRelative("last_n")} /> 最近
                <input type="number" min={1} aria-label="最近的期數" value={count} onChange={(e) => setCount(Number(e.target.value))} />期
              </label>
              <label>
                <input type="radio" name="relative" checked={relative === "next_n"} onChange={() => setRelative("next_n")} /> 未來
                <input type="number" min={1} aria-label="未來的期數" value={count} onChange={(e) => setCount(Number(e.target.value))} />期
              </label>
            </div>
            <label>
              基準日
              <input type="date" aria-label="基準日" value={anchor} onChange={(e) => setAnchor(e.target.value)} />
              <span className="hint">留空為今天</span>
            </label>
          </div>
        )}

        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={saving} onClick={() => void save()}>
            確定
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
