import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import {
  currentFilter,
  describeRelative,
  displayScalar,
  fieldCaption,
  fieldKey,
  findFilter,
  memberLabel,
  parameterLabel,
  selectedValues,
} from "./state";
import type { FilterValue, IR, Parameter, QueryState, Scalar, SetValue, Zone } from "./types";

interface FilterProps {
  ir: IR;
  workbook: string;
  zone: Zone;
  state: Record<string, FilterValue> | undefined;
  onChange: (key: string, value: FilterValue) => void;
  /** 「只顯示相關的值」的控制項：這張工作表目前的查詢狀態（其他篩選改變時重新取得可選值） */
  query?: QueryState;
}

/**
 * 醒目提示控制項（Tableau 的「醒目提示器」）：輸入時，名稱包含輸入文字的值的標記醒目、其餘淡化；
 * 也可以從清單中選一個值。清空即取消醒目提示。
 */
export function HighlighterControl({
  ir,
  workbook,
  zone,
  values,
  onChange,
}: {
  ir: IR;
  workbook: string;
  zone: Zone;
  values: Scalar[];
  onChange: (key: string, values: Scalar[]) => void;
}) {
  const use = zone.field!;
  const key = fieldKey(use);
  const [domain, setDomain] = useState<Scalar[]>([]);
  const [text, setText] = useState(() => (values.length === 1 ? memberLabel(ir, use, values[0]) : ""));
  useEffect(() => {
    api
      .domain(workbook, key, "values")
      .then((d) => setDomain(d.kind === "values" ? d.values : []))
      .catch(() => setDomain([]));
  }, [workbook, key]);
  const title = `醒目提示 ${fieldCaption(ir, use)}`;
  const listId = `highlighter-${zone.id}`;
  const update = (next: string) => {
    setText(next);
    const wanted = next.trim().toLowerCase();
    if (!wanted) return onChange(key, []);
    const exact = domain.filter((v) => memberLabel(ir, use, v).toLowerCase() === wanted);
    onChange(key, exact.length > 0 ? exact : domain.filter((v) => memberLabel(ir, use, v).toLowerCase().includes(wanted)));
  };
  return (
    <fieldset className="control highlighter" data-control="highlighter">
      <legend>{title}</legend>
      <input type="search" aria-label={title} placeholder={title} list={listId} value={text} onChange={(e) => update(e.target.value)} />
      <datalist id={listId}>
        {domain.map((v) => (
          <option key={JSON.stringify(v)} value={memberLabel(ir, use, v)} />
        ))}
      </datalist>
    </fieldset>
  );
}

/** 依 IR 的篩選控制項：單選、多選、範圍、日期範圍、輸入。 */
export function FilterControl(props: FilterProps) {
  const { ir, zone } = props;
  const control = zone.control ?? "multi_list";
  if (control === "relative_date" || control === "condition") {
    // 相對日期與條件篩選：顯示目前的設定（檢視器尚不能修改）
    const use = zone.field!;
    const filter = findFilter(ir, zone.worksheet!, fieldKey({ ...use, date: null }));
    const text =
      control === "relative_date" && filter?.kind === "relative_date"
        ? describeRelative(filter)
        : filter?.kind === "condition"
          ? `條件：${filter.condition}`
          : "（找不到篩選）";
    return (
      <fieldset className="control" data-control={control}>
        <legend>{fieldCaption(ir, use)}</legend>
        <div className="fixed-filter" title="此篩選在 Tableau 中設定，檢視器尚不能修改">
          {text}
        </div>
      </fieldset>
    );
  }
  return <SelectionControl {...props} />;
}

function SelectionControl({ ir, workbook, zone, state, onChange, query }: FilterProps) {
  const use = zone.field!;
  const key = fieldKey(use);
  const control = zone.control ?? "multi_list";
  const isRange = control === "range_slider" || control === "date_range";
  const [domain, setDomain] = useState<Scalar[]>([]);
  const [bounds, setBounds] = useState<{ min: Scalar; max: Scalar } | null>(null);
  const [open, setOpen] = useState(control !== "multi_dropdown");
  const value = currentFilter(ir, zone.worksheet!, key, state);

  const options = zone.options ?? {};
  // 只顯示相關的值、內容中的所有值：帶著工作表上其他篩選目前的狀態（自己的選取不算）
  const scoped = zone.relevant ? "relevant" : options.context_values ? "context" : null;
  const relevant = useMemo(() => {
    if (!scoped || isRange || !query || !zone.worksheet) return null;
    const { [key]: _own, ...filters } = query.filters ?? {};
    return JSON.stringify({ worksheet: zone.worksheet, state: { ...query, filters }, scope: scoped });
  }, [scoped, zone.worksheet, isRange, query, key]);
  // 顯示套用按鈕：勾選先記在草稿，按「套用」才送出；搜尋：只列出名稱包含輸入文字的成員
  const [draft, setDraft] = useState<FilterValue | null>(null);
  const [search, setSearch] = useState("");
  useEffect(() => {
    let cancelled = false;
    api
      .domain(workbook, key, isRange ? "range" : "values", relevant ? JSON.parse(relevant) : undefined)
      .then((d) => {
        if (cancelled) return;
        if (d.kind === "values") setDomain(d.values);
        else setBounds({ min: d.min, max: d.max });
      })
      .catch(() => {
        if (!cancelled) setDomain([]);
      });
    return () => {
      cancelled = true;
    };
  }, [workbook, key, isRange, relevant]);

  const title = fieldCaption(ir, use);
  if (isRange) {
    const kind = control === "date_range" ? "date" : "number";
    const update = (side: "min" | "max", text: string) => {
      const parsed: Scalar = text === "" ? null : kind === "number" ? Number(text) : text;
      onChange(key, { min: side === "min" ? parsed : value.min ?? null, max: side === "max" ? parsed : value.max ?? null });
    };
    return (
      <fieldset className="control" data-control={control}>
        <legend>{title}</legend>
        <div className="range">
          <input
            type={kind}
            value={value.min === null || value.min === undefined ? "" : String(value.min).slice(0, 10)}
            min={bounds?.min === null || bounds?.min === undefined ? undefined : String(bounds.min).slice(0, 10)}
            onChange={(e) => update("min", e.target.value)}
            aria-label={`${title} 最小值`}
          />
          <span>～</span>
          <input
            type={kind}
            value={value.max === null || value.max === undefined ? "" : String(value.max).slice(0, 10)}
            max={bounds?.max === null || bounds?.max === undefined ? undefined : String(bounds.max).slice(0, 10)}
            onChange={(e) => update("max", e.target.value)}
            aria-label={`${title} 最大值`}
          />
        </div>
      </fieldset>
    );
  }

  const label = (v: Scalar) => memberLabel(ir, use, v);
  const single = control === "single_dropdown" || control === "single_list" || control === "single_slider";
  const pending = options.show_apply === true && !single && control !== "type_in";
  const shownValue = pending && draft ? draft : value;
  // 排除（顯示包含／排除）：勾選的是要排除的值
  const excluding = options.show_exclude === true && shownValue.mode === "exclude";
  const selected = excluding
    ? domain.filter((d) => (shownValue.values ?? []).some((v) => JSON.stringify(v) === JSON.stringify(d)))
    : selectedValues(shownValue, domain);
  const selectedKeys = new Set(selected.map((v) => JSON.stringify(v)));
  const emit = (next: FilterValue) => (pending ? setDraft(next) : onChange(key, next));
  const setSelection = (values: Scalar[]) => {
    if (excluding) emit({ mode: "exclude", values });
    else if (values.length === domain.length) emit({ mode: "all", values: [] });
    else emit({ mode: "include", values });
  };
  const showAll = options.show_all !== false;

  if (single) {
    const current = value.mode === "include" && selected.length === 1 ? JSON.stringify(selected[0]) : "__all__";
    return (
      <fieldset className="control" data-control={control}>
        <legend>{title}</legend>
        <select
          value={current}
          onChange={(e) =>
            e.target.value === "__all__"
              ? onChange(key, { mode: "all", values: [] })
              : onChange(key, { mode: "include", values: [JSON.parse(e.target.value) as Scalar] })
          }
        >
          {showAll ? <option value="__all__">（全部）</option> : current === "__all__" && <option value="__all__" disabled>（請選擇）</option>}
          {domain.map((v) => (
            <option key={JSON.stringify(v)} value={JSON.stringify(v)}>
              {label(v)}
            </option>
          ))}
        </select>
      </fieldset>
    );
  }

  if (control === "type_in") {
    return (
      <fieldset className="control" data-control={control}>
        <legend>{title}</legend>
        <input
          type="text"
          placeholder="以逗號分隔多個值"
          defaultValue={value.mode === "include" ? (value.values ?? []).map(displayScalar).join(",") : ""}
          onBlur={(e) => {
            const values = e.target.value.split(",").map((v) => v.trim()).filter(Boolean);
            onChange(key, values.length ? { mode: "include", values } : { mode: "all", values: [] });
          }}
        />
      </fieldset>
    );
  }

  const summary =
    (excluding ? "排除：" : "") +
    (selected.length === domain.length ? "（全部）" : selected.length === 0 ? "（無）" : selected.map(label).join("、"));
  const searchable = options.show_search !== false && domain.length > SEARCH_FROM;
  const term = search.trim().toLowerCase();
  const listed = searchable && term ? domain.filter((v) => label(v).toLowerCase().includes(term)) : domain;
  return (
    <fieldset className="control" data-control={control}>
      <legend>{title}</legend>
      {control === "multi_dropdown" && (
        <button type="button" className="dropdown" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className="summary">{summary}</span>
          <span aria-hidden>▾</span>
        </button>
      )}
      {open && (
        <div className={control === "multi_dropdown" ? "options floating" : "options"}>
          {searchable && <input type="search" className="control-search" aria-label={`搜尋 ${title}`} placeholder="搜尋" value={search} onChange={(e) => setSearch(e.target.value)} />}
          {options.show_exclude === true && (
            <label className="control-exclude" title="勾選的值改為排除">
              <input
                type="checkbox"
                checked={excluding}
                onChange={(e) =>
                  // 切換時勾選的值不變：包含 ↔ 排除
                  emit(e.target.checked ? { mode: "exclude", values: shownValue.mode === "include" ? selected : [] } : selected.length === 0 ? { mode: "all", values: [] } : { mode: "include", values: selected })
                }
              />
              排除
            </label>
          )}
          {showAll && (
            <label>
              <input
                type="checkbox"
                checked={selected.length === domain.length && domain.length > 0}
                onChange={(e) => setSelection(e.target.checked ? domain : [])}
              />
              （全部）
            </label>
          )}
          {listed.map((v) => {
            const id = JSON.stringify(v);
            return (
              <label key={id}>
                <input
                  type="checkbox"
                  checked={selectedKeys.has(id)}
                  onChange={(e) => {
                    const next = e.target.checked
                      ? domain.filter((d) => selectedKeys.has(JSON.stringify(d)) || JSON.stringify(d) === id)
                      : selected.filter((d) => JSON.stringify(d) !== id);
                    setSelection(next);
                  }}
                />
                {label(v)}
              </label>
            );
          })}
          {pending && (
            <div className="control-apply">
              <button
                type="button"
                className="primary"
                disabled={draft === null}
                onClick={() => {
                  if (draft) onChange(key, draft);
                  setDraft(null);
                }}
              >
                套用
              </button>
              <button type="button" disabled={draft === null} onClick={() => setDraft(null)}>
                取消
              </button>
            </div>
          )}
        </div>
      )}
    </fieldset>
  );
}

/** 成員超過這個數目的多選清單顯示搜尋的輸入框。 */
const SEARCH_FROM = 10;

/**
 * 集合控制項（Tableau 2020.2 的「顯示集合」）：列出集合所在維度的值，勾選的是集合的成員；改變後使用這個集合的
 * 工作表（In／Out、依集合的計算）跟著改變。只支援固定成員、單一維度的集合。
 */
export function SetControl({
  ir,
  workbook,
  field,
  value,
  onChange,
}: {
  ir: IR;
  workbook: string;
  /** 集合欄位的 id */
  field: string;
  /** 目前的成員（集合動作或這個控制項改過的）；沒有時為集合原本的成員 */
  value: SetValue | undefined;
  onChange: (value: SetValue) => void;
}) {
  const data = ir.datasources.flatMap((d) => d.fields).find((f) => f.id === field);
  const level = data?.set?.levels[0];
  const key = level ? fieldKey(level) : "";
  const [domain, setDomain] = useState<Scalar[]>([]);
  useEffect(() => {
    if (!key) return;
    let cancelled = false;
    api
      .domain(workbook, key, "values")
      .then((d) => !cancelled && setDomain(d.kind === "values" ? d.values : []))
      .catch(() => !cancelled && setDomain([]));
    return () => {
      cancelled = true;
    };
  }, [workbook, key]);
  if (!data?.set || !level) return <div className="placeholder unsupported">找不到集合 {field}</div>;
  const rule = data.set.rule;
  const levels = data.set.levels;
  if (levels.length > 1) {
    // 多個維度的集合：列出的成員組合可以勾選或取消（集合原本的成員就是可選的範圍）
    const tuples = rule.members ?? [];
    const now: SetValue = value ?? { mode: "include", members: tuples };
    const chosen = new Set(now.members.map((m) => JSON.stringify(m)));
    const pick = (tuple: Scalar[], on: boolean) =>
      onChange({ mode: "include", members: tuples.filter((t) => (JSON.stringify(t) === JSON.stringify(tuple) ? on : chosen.has(JSON.stringify(t)))) });
    return (
      <fieldset className="control" data-control="set" data-set={field}>
        <legend>{data.caption}</legend>
        <div className="options">
          {tuples.map((tuple) => (
            <label key={JSON.stringify(tuple)}>
              <input type="checkbox" checked={chosen.has(JSON.stringify(tuple))} onChange={(e) => pick(tuple, e.target.checked)} />
              {tuple.map((v, i) => memberLabel(ir, levels[i] ?? level, v)).join("、")}
            </label>
          ))}
        </div>
      </fieldset>
    );
  }
  // Top N 與條件集合：控制項改的是「一般」分頁的選取（排名或條件之前先選取的成員），全部時等同沒有排除任何成員
  const saved: SetValue =
    rule.kind === "members"
      ? { mode: rule.mode === "exclude" ? "exclude" : "include", members: rule.members ?? [] }
      : rule.mode === "include" || rule.mode === "exclude"
        ? { mode: rule.mode, members: (rule.values ?? []).map((v) => [v]) }
        : { mode: "exclude", members: [] };
  const current: SetValue = value ?? saved;
  const listed = new Set(current.members.map((m) => JSON.stringify(m[0] ?? null)));
  const inSet = (v: Scalar) => listed.has(JSON.stringify(v)) !== (current.mode === "exclude");
  const toggle = (v: Scalar, on: boolean) => {
    const members = domain.filter((d) => (JSON.stringify(d) === JSON.stringify(v) ? on : inSet(d))).map((d) => [d]);
    onChange({ mode: "include", members });
  };
  return (
    <fieldset className="control" data-control="set" data-set={field} data-rule={rule.kind}>
      <legend>{data.caption}</legend>
      {rule.kind !== "members" && <div className="hint">{rule.kind === "top_n" ? "勾選的成員中再取前（後）N 名" : "勾選的成員中符合條件的"}</div>}
      <div className="options">
        {domain.map((v) => (
          <label key={JSON.stringify(v)}>
            <input type="checkbox" checked={inSet(v)} onChange={(e) => toggle(v, e.target.checked)} />
            {memberLabel(ir, level, v)}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

interface ParameterProps {
  parameter: Parameter;
  control: string | null;
  value: Scalar;
  onChange: (value: Scalar) => void;
}

export function ParameterControl({ parameter, control, value, onChange }: ParameterProps) {
  const { allowed, datatype } = parameter;
  const numeric = datatype === "integer" || datatype === "real";
  const parse = (text: string): Scalar =>
    numeric ? (datatype === "integer" ? Number.parseInt(text, 10) : Number(text)) : text;
  const options = useMemo(() => allowed.values ?? [], [allowed.values]);

  if (allowed.kind === "list") {
    return (
      <fieldset className="control" data-control={control ?? "single_dropdown"}>
        <legend>{parameter.caption}</legend>
        <select value={JSON.stringify(value)} onChange={(e) => onChange(JSON.parse(e.target.value) as Scalar)}>
          {options.map((v) => (
            <option key={JSON.stringify(v)} value={JSON.stringify(v)}>
              {parameterLabel(parameter, v)}
            </option>
          ))}
        </select>
      </fieldset>
    );
  }
  if (allowed.kind === "range" && numeric && control === "single_slider") {
    return (
      <fieldset className="control" data-control="single_slider">
        <legend>
          {parameter.caption}：<strong>{parameterLabel(parameter, value)}</strong>
        </legend>
        <input
          type="range"
          min={Number(allowed.min ?? 0)}
          max={Number(allowed.max ?? 100)}
          step={allowed.step ?? 1}
          value={Number(value)}
          onChange={(e) => onChange(parse(e.target.value))}
        />
      </fieldset>
    );
  }
  return (
    <fieldset className="control" data-control={control ?? "type_in"}>
      <legend>{parameter.caption}</legend>
      <input
        type={numeric ? "number" : datatype === "date" ? "date" : "text"}
        value={value === null ? "" : String(value)}
        min={allowed.kind === "range" && allowed.min !== null ? String(allowed.min) : undefined}
        max={allowed.kind === "range" && allowed.max !== null ? String(allowed.max) : undefined}
        onChange={(e) => onChange(e.target.value === "" ? null : parse(e.target.value))}
      />
    </fieldset>
  );
}
