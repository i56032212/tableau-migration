// 資料窗格欄位選單的其他對話框：預設屬性的色彩、形狀與排序，分割與自訂分割，取代參照，描述欄位。

import type React from "react";
import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi } from "./api";
import { type EditorDialog, useEditor } from "./context";
import { memberText } from "./FieldDialogs";
import type { CatalogField } from "./model";
import { type ShapeChoice, ShapePalettes, ShapeSelect, SHAPES, useShapePalettes } from "./ShapePicker";

const TYPE_NAMES: Record<string, string> = {
  string: "字串",
  integer: "數字（整數）",
  real: "數字（小數）",
  boolean: "布林",
  date: "日期",
  datetime: "日期與時間",
  spatial: "空間資料",
};

interface Props {
  datasource: string;
  field: string;
  onClose: () => void;
  onSaved: () => void;
}

function plainUse(field: string): FieldUse {
  return { field, agg: null, date: null, continuous: false, table_calc: null };
}

function useTarget(datasource: string, field: string): CatalogField | undefined {
  const editor = useEditor();
  return editor.catalog.datasources.find((d) => d.id === datasource)?.fields.find((f) => f.id === field);
}

/** 欄位的不重複值（最多 limit 個）。 */
function useMembers(field: string, limit = 500): { values: Scalar[] | null; error: string | null } {
  const editor = useEditor();
  const [values, setValues] = useState<Scalar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    editApi
      .domain(editor.workbook, plainUse(field), { kind: "values" })
      .then((d) => !cancelled && setValues(d.kind === "values" ? d.values.slice(0, limit) : []))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [editor.workbook, field, limit]);
  return { values, error };
}

function useSave(onClose: () => void, onSaved: () => void) {
  const editor = useEditor();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = (task: () => Promise<unknown>, message: string) => {
    setBusy(true);
    task()
      .then(() => {
        onSaved();
        editor.notify(message);
        onClose();
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return { error, busy, run };
}

function Actions({ onSave, onClose, disabled = false, extra }: { onSave: () => void; onClose: () => void; disabled?: boolean; extra?: React.ReactNode }) {
  return (
    <div className="dialog-actions">
      <button type="button" className="primary" disabled={disabled} onClick={onSave}>
        確定
      </button>
      {extra}
      <button type="button" onClick={onClose}>
        取消
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- 預設屬性：色彩

/** 預設的色彩：維度是各成員的顏色與色盤，量值是連續的色盤（反轉、分段）。工作表沒有另外設定時使用。 */
export function DefaultColorDialog({ datasource, field, onClose, onSaved }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const palette = target?.default_color as { palette?: string | null; reverse?: boolean; steps?: number | null; assignments?: { value: Scalar; color: string }[] } | null | undefined;
  const dimension = target?.role === "dimension";
  const [name, setName] = useState(palette?.palette ?? "");
  const [reverse, setReverse] = useState(Boolean(palette?.reverse));
  const [steps, setSteps] = useState(palette?.steps ? String(palette.steps) : "");
  const [assigned, setAssigned] = useState<Record<string, { value: Scalar; color: string }>>(() =>
    Object.fromEntries((palette?.assignments ?? []).map((a) => [JSON.stringify(a.value), a])),
  );
  const { values } = useMembers(field, 200);
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const choices = editor.catalog.palettes.filter((p) => (dimension ? p.kind === "regular" : p.kind !== "regular"));
  const colors = (editor.catalog.palettes.find((p) => p.name === (name || "tableau10_10_0")) ?? choices[0])?.colors ?? [];
  const save = () => {
    const assignments = dimension ? Object.values(assigned) : [];
    const chosen = editor.catalog.palettes.find((p) => p.name === name);
    const value =
      !name && assignments.length === 0
        ? null
        : {
            kind: dimension ? "discrete" : "continuous",
            palette: name || null,
            assignments,
            colors: [],
            diverging: !dimension && chosen?.kind === "diverging",
            reverse: !dimension && reverse,
            steps: !dimension && steps.trim() ? Math.max(2, Math.floor(Number(steps))) : null,
          };
    run(
      () => editApi.saveFieldProperties(editor.workbook, datasource, field, { default_color: value }),
      value === null ? `「${target.caption}」的預設色彩改為自動` : `已設定「${target.caption}」的預設色彩`,
    );
  };
  return (
    <Modal title={`預設色彩：${target.caption}`} onClose={onClose}>
      <div className="colors-dialog">
        <p className="hint">欄位放到「色彩」上、工作表沒有另外設定時使用。</p>
        <label className="inline">
          色盤
          <select aria-label="預設色盤" value={name} onChange={(e) => setName(e.target.value)}>
            <option value="">自動</option>
            {choices.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        {!dimension && (
          <>
            <label className="inline">
              <input type="checkbox" checked={reverse} onChange={(e) => setReverse(e.target.checked)} /> 反轉
            </label>
            <label className="inline">
              分段
              <input type="number" min={2} aria-label="預設分段數" value={steps} placeholder="連續" onChange={(e) => setSteps(e.target.value)} />
            </label>
          </>
        )}
        {dimension && values && values.length > 0 && (
          <fieldset className="member-colors">
            <legend>個別成員的顏色</legend>
            {values.map((value, i) => {
              const key = JSON.stringify(value);
              const chosen = assigned[key]?.color;
              const label = memberText(value);
              return (
                <label className="inline" key={key}>
                  <input
                    type="color"
                    aria-label={`${label}的預設顏色`}
                    value={chosen ?? colors[i % Math.max(1, colors.length)] ?? "#4e79a7"}
                    onChange={(e) => setAssigned((current) => ({ ...current, [key]: { value, color: e.target.value.toLowerCase() } }))}
                  />
                  {label}
                  {chosen && (
                    <button
                      type="button"
                      className="link"
                      onClick={() =>
                        setAssigned((current) => {
                          const next = { ...current };
                          delete next[key];
                          return next;
                        })
                      }
                    >
                      重設
                    </button>
                  )}
                </label>
              );
            })}
          </fieldset>
        )}
        {error && <p className="error">{error}</p>}
        <Actions onSave={save} onClose={onClose} disabled={busy} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 預設屬性：形狀

export function DefaultShapesDialog({ datasource, field, onClose, onSaved }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const [assigned, setAssigned] = useState<Record<string, { value: Scalar } & ShapeChoice>>(() =>
    Object.fromEntries((target?.default_shapes ?? []).map((a) => [JSON.stringify(a.value), a])),
  );
  const { values, error: loadError } = useMembers(field, 200);
  const { palettes, setPalettes } = useShapePalettes(editor.workbook);
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const save = () =>
    run(
      () => editApi.saveFieldProperties(editor.workbook, datasource, field, { default_shapes: Object.values(assigned) }),
      `已設定「${target.caption}」的預設形狀`,
    );
  const assignPalette = (images: string[]) => {
    if (images.length === 0) return;
    setAssigned(Object.fromEntries((values ?? []).map((value, i) => [JSON.stringify(value), { value, shape: "circle", image: images[i % images.length] }])));
  };
  return (
    <Modal title={`預設形狀：${target.caption}`} onClose={onClose}>
      <div className="action-form shapes-dialog">
        <p className="hint">欄位放到「形狀」上、工作表沒有另外設定時使用。</p>
        {values?.length === 0 && <p className="hint">沒有可以指定形狀的成員。</p>}
        {(values ?? []).map((value, i) => {
          const id = JSON.stringify(value);
          const label = memberText(value);
          return (
            <ShapeSelect
              key={id}
              workbook={editor.workbook}
              label={label}
              ariaLabel={`${label}的預設形狀`}
              value={assigned[id]}
              auto={`自動（${SHAPES[i % SHAPES.length][1]}）`}
              palettes={palettes}
              onChange={(choice) =>
                setAssigned((current) => {
                  const next = { ...current };
                  if (choice) next[id] = { value, ...choice };
                  else delete next[id];
                  return next;
                })
              }
            />
          );
        })}
        <ShapePalettes workbook={editor.workbook} palettes={palettes} onPalettes={setPalettes} onAssign={assignPalette} />
        {(error ?? loadError) && <p className="error">{error ?? loadError}</p>}
        <Actions onSave={save} onClose={onClose} disabled={busy} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 預設屬性：排序

type SortChoice = "source" | "asc" | "desc" | "manual";

export function DefaultSortDialog({ datasource, field, onClose, onSaved }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const current = target?.default_sort ?? null;
  const [choice, setChoice] = useState<SortChoice>(current === null ? "source" : current.kind === "manual" ? "manual" : current.order);
  const [order, setOrder] = useState<Scalar[] | null>(current?.kind === "manual" ? current.manual_values : null);
  const { values, error: loadError } = useMembers(field);
  const { error, busy, run } = useSave(onClose, onSaved);
  // 手動排列：先列出已經排好的成員，其餘依資料的順序接在後面
  const manual = useMemo(() => {
    const known = order ?? [];
    const keys = new Set(known.map((v) => JSON.stringify(v)));
    return [...known, ...(values ?? []).filter((v) => !keys.has(JSON.stringify(v)))];
  }, [order, values]);
  if (!target) return null;
  const move = (index: number, delta: number) => {
    const next = [...manual];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    setOrder(next);
  };
  const save = () => {
    const value =
      choice === "source"
        ? null
        : choice === "manual"
          ? { kind: "manual" as const, order: "asc" as const, manual_values: manual }
          : { kind: "alphabetic" as const, order: choice, manual_values: [] };
    run(() => editApi.saveFieldProperties(editor.workbook, datasource, field, { default_sort: value }), `已設定「${target.caption}」的預設排序`);
  };
  return (
    <Modal title={`預設排序：${target.caption}`} onClose={onClose}>
      <div className="action-form sort-dialog">
        <p className="hint">這個維度在工作表上沒有另外排序時的順序。</p>
        {(
          [
            ["source", "資料來源順序"],
            ["asc", "字母順序：遞增"],
            ["desc", "字母順序：遞減"],
            ["manual", "手動"],
          ] as [SortChoice, string][]
        ).map(([value, label]) => (
          <label className="inline" key={value}>
            <input type="radio" name="default-sort" checked={choice === value} onChange={() => setChoice(value)} /> {label}
          </label>
        ))}
        {choice === "manual" && (
          <ol className="default-manual-sort" aria-label="手動排列的順序">
            {manual.map((value, i) => (
              <li key={JSON.stringify(value)}>
                <span>{memberText(value)}</span>
                <button type="button" className="link" aria-label={`${memberText(value)}往上`} disabled={i === 0} onClick={() => move(i, -1)}>
                  ▲
                </button>
                <button type="button" className="link" aria-label={`${memberText(value)}往下`} disabled={i === manual.length - 1} onClick={() => move(i, 1)}>
                  ▼
                </button>
              </li>
            ))}
          </ol>
        )}
        {(error ?? loadError) && <p className="error">{error ?? loadError}</p>}
        <Actions onSave={save} onClose={onClose} disabled={busy || (choice === "manual" && manual.length === 0)} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 分割、自訂分割

/** 資料中最常見的分隔符號與分割後最多的段數（Tableau 的自動分割）；找不到共同的分隔符號時為 null。 */
export function detectSplit(values: Scalar[]): { separator: string; count: number } | null {
  const texts = values.filter((v): v is string => typeof v === "string" && v !== "");
  if (texts.length === 0) return null;
  const counts = new Map<string, number>();
  for (const text of texts) {
    for (const ch of new Set(text.match(/[^\p{L}\p{N}\s]|\s/gu) ?? [])) counts.set(ch, (counts.get(ch) ?? 0) + 1);
  }
  let best: string | null = null;
  for (const [ch, n] of counts) {
    if (n * 2 < texts.length) continue; // 至少一半的值有這個符號
    if (best === null || n > (counts.get(best) ?? 0) || (n === counts.get(best) && ch !== " " && best === " ")) best = ch;
  }
  if (best === null) return null;
  const separator = best;
  const count = Math.min(10, Math.max(...texts.map((t) => t.split(separator).length)));
  return count > 1 ? { separator, count } : null;
}

export function SplitDialog({ datasource, field, onClose, onSaved }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const { values, error: loadError } = useMembers(field, 1000);
  const detected = useMemo(() => (values ? detectSplit(values) : null), [values]);
  const [separator, setSeparator] = useState<string | null>(null);
  const [side, setSide] = useState<"first" | "last" | "all">("first");
  const [count, setCount] = useState("1");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const sep = separator ?? detected?.separator ?? "";
  const parts = values ? Math.min(10, Math.max(1, ...values.filter((v): v is string => typeof v === "string").map((t) => (sep ? t.split(sep).length : 1)))) : 1;
  const split = (useSeparator: string, n: number, fromEnd: boolean) =>
    run(() => editApi.splitField(editor.workbook, datasource, field, useSeparator, n, fromEnd), `已把「${target.caption}」分割成 ${n} 個欄位`);
  const save = () => split(sep, side === "all" ? parts : Math.max(1, Math.min(10, Math.floor(Number(count)) || 1)), side === "last");
  return (
    <Modal title={`分割：${target.caption}`} onClose={onClose}>
      <div className="action-form">
        <p className="hint">
          依分隔符號把字串分成多個計算欄位（「{target.caption} - Split 1」…）。
          {detected ? `資料中最常見的分隔符號是「${detected.separator === " " ? "空格" : detected.separator}」，最多 ${detected.count} 段。` : values ? "資料中沒有共同的分隔符號，請自行指定。" : "讀取資料中…"}
        </p>
        <label className="admin-field">
          分隔符號
          <input aria-label="分隔符號" value={sep} onChange={(e) => setSeparator(e.target.value)} />
        </label>
        <label className="inline">
          分割出
          <select aria-label="分割的範圍" value={side} onChange={(e) => setSide(e.target.value as "first" | "last" | "all")}>
            <option value="first">前面的</option>
            <option value="last">最後的</option>
            <option value="all">全部</option>
          </select>
          {side !== "all" && (
            <>
              <input type="number" min={1} max={10} aria-label="分割的欄位數" value={count} onChange={(e) => setCount(e.target.value)} /> 個欄位
            </>
          )}
          {side === "all" && <span>（{parts} 個欄位）</span>}
        </label>
        {(error ?? loadError) && <p className="error">{error ?? loadError}</p>}
        <Actions
          onSave={save}
          onClose={onClose}
          disabled={busy || !sep}
          extra={
            <button type="button" disabled={busy || !detected} title="依資料中最常見的分隔符號分割成所有的段（Tableau 的「分割」）" onClick={() => detected && split(detected.separator, detected.count, false)}>
              自動分割
            </button>
          }
        />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 取代參照

export function ReplaceReferencesDialog({ datasource, field, onClose, onSaved }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const candidates = (editor.catalog.datasources.find((d) => d.id === datasource)?.fields ?? []).filter(
    (f) => f.id !== field && !f.ad_hoc && !f.hidden && f.role === target?.role,
  );
  const [replacement, setReplacement] = useState(candidates[0]?.id ?? "");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const chosen = candidates.find((f) => f.id === replacement);
  const save = () =>
    run(() => editApi.replaceReferences(editor.workbook, datasource, field, replacement), `用到「${target.caption}」的地方已改用「${chosen?.caption ?? ""}」`);
  return (
    <Modal title={`取代參照：${target.caption}`} onClose={onClose}>
      <div className="action-form">
        <p className="hint">工作表、篩選、動作與計算欄位中用到「{target.caption}」的地方，都改用選取的欄位；「{target.caption}」本身保留。</p>
        <label className="admin-field">
          改用
          <select aria-label="取代成的欄位" value={replacement} onChange={(e) => setReplacement(e.target.value)}>
            {candidates.map((f) => (
              <option key={f.id} value={f.id}>
                {f.caption}
              </option>
            ))}
          </select>
        </label>
        {error && <p className="error">{error}</p>}
        <Actions onSave={save} onClose={onClose} disabled={busy || !replacement} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 描述欄位

/** 描述（Tableau 的「描述」）：角色、型別、來源、公式、註解，以及按下「載入」後的值域。 */
export function DescribeDialog({ datasource, field, onClose }: Props) {
  const editor = useEditor();
  const target = useTarget(datasource, field);
  const source = editor.catalog.datasources.find((d) => d.id === datasource);
  const [domain, setDomain] = useState<{ kind: "values"; values: Scalar[] } | { kind: "range"; min: Scalar; max: Scalar } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  if (!target) return null;
  const numeric = target.datatype === "integer" || target.datatype === "real";
  const continuous = target.role === "measure";
  const load = () => {
    setLoading(true);
    setError(null);
    const ranged = numeric && target.role === "measure";
    editApi
      .domain(editor.workbook, plainUse(field), { kind: ranged ? "range" : "values" })
      .then(setDomain)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };
  const rows: [string, React.ReactNode][] = [
    ["角色", `${continuous ? "連續" : "離散"}的${target.role === "measure" ? "量值" : "維度"}`],
    ["類型", TYPE_NAMES[target.datatype] ?? target.datatype],
    ["資料源", source?.caption ?? datasource],
  ];
  if (target.kind === "column") {
    if (target.table) rows.push(["資料表", target.table]);
    rows.push(["來源欄位", target.column ?? target.name]);
  } else if (target.kind === "set") rows.push(["種類", "集合"]);
  else if (target.group) rows.push(["種類", "群組"]);
  else if (target.bin) rows.push(["種類", "數值分組"]);
  else rows.push(["種類", "計算欄位"]);
  if (target.formula && !target.group && !target.bin) rows.push(["公式", <code key="f">{target.formula}</code>]);
  if (target.default_agg) rows.push(["預設彙總", target.default_agg.toUpperCase()]);
  if (target.format) rows.push(["預設格式", <code key="fmt">{target.format}</code>]);
  if (target.geo_role) rows.push(["地理角色", target.geo_role]);
  if (target.comment) rows.push(["註解", target.comment]);
  rows.push(["狀態", target.hidden ? "有效（已隱藏）" : "有效"]);
  return (
    <Modal title={`描述：${target.caption}`} onClose={onClose}>
      <div className="describe-dialog">
        <table className="describe-table">
          <tbody>
            {rows.map(([label, value]) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                <td>{value}</td>
              </tr>
            ))}
            <tr>
              <th scope="row">值域</th>
              <td>
                {domain === null ? (
                  <button type="button" disabled={loading} onClick={load}>
                    {loading ? "載入中…" : "載入"}
                  </button>
                ) : domain.kind === "range" ? (
                  <span>
                    {memberText(domain.min)} 到 {memberText(domain.max)}
                  </span>
                ) : (
                  <div>
                    <div>{domain.values.length} 個成員{domain.values.length > 200 ? "（顯示前 200 個）" : ""}：</div>
                    <ul className="describe-members">
                      {domain.values.slice(0, 200).map((v) => (
                        <li key={JSON.stringify(v)}>{memberText(v)}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {error && <p className="error">{error}</p>}
              </td>
            </tr>
          </tbody>
        </table>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={onClose}>
            關閉
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 依對話框的種類顯示上面的對話框（其他種類回傳 null）。 */
export function moreFieldDialog(dialog: EditorDialog, onClose: () => void, onSaved: () => void): React.ReactNode {
  switch (dialog.kind) {
    case "default-color":
      return <DefaultColorDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "default-shapes":
      return <DefaultShapesDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "default-sort":
      return <DefaultSortDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "split":
      return <SplitDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "replace-references":
      return <ReplaceReferencesDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    case "describe":
      return <DescribeDialog datasource={dialog.datasource} field={dialog.field} onClose={onClose} onSaved={onSaved} />;
    default:
      return null;
  }
}
