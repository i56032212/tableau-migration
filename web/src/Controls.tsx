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
import type { FilterValue, IR, Parameter, QueryState, Scalar, Zone } from "./types";

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

  // 只顯示相關的值：帶著工作表上其他篩選目前的狀態（自己的選取不算）
  const relevant = useMemo(() => {
    if (!zone.relevant || isRange || !query || !zone.worksheet) return null;
    const { [key]: _own, ...filters } = query.filters ?? {};
    return JSON.stringify({ worksheet: zone.worksheet, state: { ...query, filters } });
  }, [zone.relevant, zone.worksheet, isRange, query, key]);
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

  const selected = selectedValues(value, domain);
  const selectedKeys = new Set(selected.map((v) => JSON.stringify(v)));
  const label = (v: Scalar) => memberLabel(ir, use, v);
  const single = control === "single_dropdown" || control === "single_list" || control === "single_slider";
  const setSelection = (values: Scalar[]) => {
    if (values.length === domain.length) onChange(key, { mode: "all", values: [] });
    else onChange(key, { mode: "include", values });
  };

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
          <option value="__all__">（全部）</option>
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
    selected.length === domain.length ? "（全部）" : selected.length === 0 ? "（無）" : selected.map(label).join("、");
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
          <label>
            <input
              type="checkbox"
              checked={selected.length === domain.length && domain.length > 0}
              onChange={(e) => setSelection(e.target.checked ? domain : [])}
            />
            （全部）
          </label>
          {domain.map((v) => {
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
        </div>
      )}
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
