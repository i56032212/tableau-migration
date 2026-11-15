// 資料窗格的欄位選單（Tableau Desktop 在欄位上按右鍵）：重新命名、複製、隱藏、轉換為維度或量值、預設屬性
// （彙總、數字或日期格式、註解）、別名、建立與編輯群組、集合、數值分組，以及刪除。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi, type SetRuleBody } from "./api";
import { type EditorContextValue, useEditor } from "./context";
import { datasourceApi } from "./datasourceApi";
import { type FormatKind, formatString, parseFormat } from "./Dialogs";
import { AGGREGATIONS, type CatalogField, isDate } from "./model";
import { type MenuItem, SEPARATOR } from "./Shelves";

const DISCRETE = ["string", "integer", "real", "boolean"];
const GROUPABLE = [...DISCRETE, "date", "datetime"];

function isDimension(f: CatalogField): boolean {
  return f.role === "dimension" && f.level !== "aggregate" && f.level !== "table_calc";
}

function rowLevel(f: CatalogField): boolean {
  return f.level !== "aggregate" && f.level !== "table_calc";
}

/** 成員的顯示：Null、布林與日期以 Tableau 的寫法。 */
export function memberText(value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "boolean") return value ? "True" : "False";
  return String(value);
}

/** 欄位本身（不彙總、不衍生日期）的使用。 */
function plainUse(field: string): FieldUse {
  return { field, agg: null, date: null, continuous: false, table_calc: null };
}

function memberKey(value: Scalar): string {
  return `${typeof value}:${String(value)}`;
}

/** 欄位的不重複值（篩選對話框同一個 API）。 */
function useMembers(workbook: string, field: string) {
  const [values, setValues] = useState<Scalar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    editApi
      .domain(workbook, plainUse(field), { kind: "values" })
      .then((d) => !cancelled && setValues(d.kind === "values" ? d.values : []))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook, field]);
  return { values, error };
}

const DATA_TYPES: [NonNullable<Parameters<typeof datasourceApi.updateField>[3]["datatype"]>, string][] = [
  ["integer", "數字（整數）"],
  ["real", "數字（小數）"],
  ["datetime", "日期與時間"],
  ["date", "日期"],
  ["string", "字串"],
  ["boolean", "布林"],
];

/** 總計使用（預設屬性）：自動為在較粗的詳細程度重新彙總，其餘為畫面上各標記的值再彙總。 */
const TOTAL_USING: ["sum" | "avg" | "min" | "max" | null, string][] = [
  [null, "自動"],
  ["sum", "加總"],
  ["avg", "平均"],
  ["min", "最小值"],
  ["max", "最大值"],
];

/** 欄位選單的項目（資料窗格的 ▾ 與按右鍵）。 */
export function fieldMenu(editor: EditorContextValue, field: CatalogField, datasource: string, hierarchical: boolean): MenuItem[] {
  const { workbook } = editor;
  const done = (message: string) => {
    editor.reloadCatalog();
    editor.notify(message);
  };
  const fail = (e: Error) => editor.notify(e.message, "error");
  const update = (body: Parameters<typeof datasourceApi.updateField>[3], message: string) =>
    void datasourceApi
      .updateField(workbook, datasource, field.id, body)
      .then(() => done(message))
      .catch(fail);
  const numeric = field.datatype === "integer" || field.datatype === "real";
  const dimension = isDimension(field);
  const derived = field.group != null || field.bin != null || field.kind === "set";
  const items: MenuItem[] = [
    { label: "重新命名…", action: () => editor.openDialog({ kind: "rename", datasource, field: field.id }) },
    {
      label: "複製",
      action: () =>
        void editApi
          .duplicateField(workbook, datasource, field.id)
          .then((copy) => done(`已建立「${copy.caption}」`))
          .catch(fail),
    },
    field.hidden
      ? { label: "取消隱藏", action: () => update({ hidden: false }, `已取消隱藏「${field.caption}」`) }
      : { label: "隱藏", action: () => update({ hidden: true }, `已隱藏「${field.caption}」（勾選「顯示隱藏的欄位」看得到）`) },
    SEPARATOR,
  ];
  const create: MenuItem[] = [{ label: "計算欄位…", action: () => editor.openCalc(null, datasource) }];
  if (dimension && field.kind !== "set" && GROUPABLE.includes(field.datatype)) {
    create.push({ label: "群組…", action: () => editor.openDialog({ kind: "group", datasource, source: field.id, field: null }) });
    create.push({ label: "集合…", action: () => editor.openDialog({ kind: "set", datasource, levels: [field.id], field: null }) });
  }
  if (numeric && rowLevel(field) && field.kind !== "set")
    create.push({ label: "數值分組…", action: () => editor.openDialog({ kind: "bins", datasource, source: field.id, field: null }) });
  if (dimension && field.kind !== "set" && field.datatype !== "spatial")
    create.push({ label: "合併欄位…", action: () => editor.openDialog({ kind: "combined", datasource, field: field.id }) });
  create.push({ label: "參數…", action: () => editor.openDialog({ kind: "parameter", parameter: null }) });
  items.push({ label: "建立", children: create });
  if (field.group) {
    const group = field.group;
    items.push({ label: "編輯群組…", action: () => editor.openDialog({ kind: "group", datasource, source: group.source, field: field.id }) });
  } else if (field.bin) {
    const bin = field.bin;
    items.push({ label: "編輯數值分組…", action: () => editor.openDialog({ kind: "bins", datasource, source: bin.source, field: field.id }) });
  } else if (field.kind === "set" && field.set) {
    const set = field.set;
    items.push({
      label: "編輯集合…",
      action: () => editor.openDialog({ kind: "set", datasource, levels: set.levels.map((l) => l.field), field: field.id }),
    });
    // 顯示集合：單獨檢視工作表時，檢視旁邊顯示集合控制項（勾選的值是集合的成員）
    if (set.rule.kind === "members" && set.levels.length === 1) {
      const controls = (editor.sheet.controls as { kind: string; field: FieldUse | null }[] | undefined) ?? [];
      const shown = controls.some((c) => c.kind === "set" && c.field?.field === field.id);
      items.push({
        label: "顯示集合",
        checked: shown,
        action: () =>
          editor.apply((s) => {
            const current = (s.controls as { kind: string; field: FieldUse | null }[] | undefined) ?? [];
            return {
              ...s,
              controls: shown
                ? current.filter((c) => !(c.kind === "set" && c.field?.field === field.id))
                : [...current, { kind: "set", field: { field: field.id, agg: null, date: null, continuous: false, table_calc: null }, parameter: null, control: "multi_list", relevant: false }],
            };
          }),
      });
    }
    // 合併集合：與同一個維度上的另一個集合合併成新的集合
    items.push({
      label: "建立合併集合…",
      action: () => editor.openDialog({ kind: "set", datasource, levels: set.levels.map((l) => l.field), field: null, combine: field.id }),
    });
  } else if (field.kind === "calc" && !field.combined) {
    items.push({ label: "編輯計算欄位…", action: () => editor.openCalc(field, datasource) });
  }
  if (dimension && DISCRETE.includes(field.datatype) && !isDate(field))
    items.push({ label: "別名…", action: () => editor.openDialog({ kind: "aliases", datasource, field: field.id }) });
  // 轉換：字串欄位依分隔符號分割成多個計算欄位（Tableau 的「轉換＞分割／自訂分割」）
  if (field.datatype === "string" && field.kind !== "set" && rowLevel(field))
    items.push({ label: "轉換", children: [{ label: "分割…", action: () => editor.openDialog({ kind: "split", datasource, field: field.id }) }] });
  // 變更資料類型：檔案的欄位以新的型別重新匯入，萃取與資料庫的欄位在查詢時轉換
  if (field.kind === "column" || (field.kind === "calc" && /（原始型別）|\(原始型別\)/.test(field.formula ?? "")))
    items.push({
      label: "變更資料類型",
      children: DATA_TYPES.map(([datatype, label]) => ({
        label,
        checked: field.datatype === datatype,
        action: () => field.datatype !== datatype && update({ datatype }, `「${field.caption}」的資料類型改為${label}`),
      })),
    });
  if (hierarchical)
    items.push({ label: "建立階層…", action: () => editor.openDialog({ kind: "hierarchy", datasource, name: null, field: field.id }) });
  if (field.kind !== "set" && ["string", "integer", "real"].includes(field.datatype))
    items.push({ label: "地理角色…", action: () => editor.openDialog({ kind: "geo", datasource, field: field.id }) });
  // 資料夾：建立、加入、移出（與 Tableau 的「資料夾」相同）
  const folders = editor.catalog.datasources.find((d) => d.id === datasource)?.folders ?? [];
  const current = folders.find((f) => f.fields.includes(field.id));
  const move = (folder: string | null, message: string) =>
    void editApi
      .moveToFolder(workbook, datasource, [field.id], folder)
      .then(() => done(message))
      .catch(fail);
  const folderItems: MenuItem[] = [
    { label: "建立資料夾…", action: () => editor.openDialog({ kind: "folder", datasource, fields: [field.id], folder: null }) },
  ];
  const targets = [...new Set(folders.map((f) => f.name))].filter((name) => name !== current?.name);
  if (targets.length)
    folderItems.push({
      label: "加入資料夾",
      children: targets.map((name) => ({ label: name, action: () => move(name, `已把「${field.caption}」移到「${name}」`) })),
    });
  if (current) folderItems.push({ label: "從資料夾移除", action: () => move(null, `已把「${field.caption}」移出「${current.name}」`) });
  items.push({ label: "資料夾", children: folderItems });
  items.push(SEPARATOR);
  if (!derived && rowLevel(field) && field.datatype !== "spatial") {
    items.push(
      field.role === "measure"
        ? { label: "轉換為維度", action: () => update({ role: "dimension" }, `「${field.caption}」已轉換為維度`) }
        : { label: "轉換為量值", action: () => update({ role: "measure" }, `「${field.caption}」已轉換為量值`) },
    );
  }
  items.push(
    { label: "取代參照…", action: () => editor.openDialog({ kind: "replace-references", datasource, field: field.id }) },
    { label: "描述…", action: () => editor.openDialog({ kind: "describe", datasource, field: field.id }) },
  );
  const properties: MenuItem[] = [];
  if (field.datatype !== "spatial" && field.kind !== "set")
    properties.push({ label: "色彩…", action: () => editor.openDialog({ kind: "default-color", datasource, field: field.id }) });
  if (dimension && field.kind !== "set" && DISCRETE.includes(field.datatype)) {
    properties.push(
      { label: "形狀…", action: () => editor.openDialog({ kind: "default-shapes", datasource, field: field.id }) },
      { label: "排序…", action: () => editor.openDialog({ kind: "default-sort", datasource, field: field.id }) },
    );
  }
  if (field.role === "measure" && rowLevel(field) && field.datatype !== "spatial") {
    const using = field.total_using ?? null;
    properties.push({
      label: "總計使用",
      children: TOTAL_USING.map(([agg, label]) => ({
        label,
        checked: using === agg,
        action: () =>
          void editApi
            .saveFieldProperties(workbook, datasource, field.id, { total_using: agg })
            .then(() => {
              done(`「${field.caption}」的總計改為${label}`);
              editor.reloadSheet();
            })
            .catch(fail),
      })),
    });
  }
  if (field.role === "measure" && rowLevel(field) && field.datatype !== "spatial") {
    properties.push({
      label: "彙總",
      children: AGGREGATIONS.filter(([agg]) => numeric || ["count", "countd", "min", "max"].includes(agg)).map(([agg, label]) => ({
        label,
        checked: field.default_agg === agg,
        action: () => update({ default_agg: agg }, `「${field.caption}」的預設彙總改為${label}`),
      })),
    });
  }
  if (numeric || isDate(field) || field.level === "aggregate")
    properties.push({
      label: isDate(field) ? "日期格式…" : "數字格式…",
      action: () => editor.openDialog({ kind: "field-format", datasource, field: field.id }),
    });
  if (isDate(field))
    properties.push({ label: "會計年度起始…", action: () => editor.openDialog({ kind: "fiscal", datasource, field: field.id }) });
  properties.push({ label: "註解…", action: () => editor.openDialog({ kind: "comment", datasource, field: field.id }) });
  items.push({ label: "預設屬性", children: properties });
  if (field.kind !== "column") {
    items.push(SEPARATOR, {
      label: "刪除",
      action: () =>
        void editApi
          .removeField(workbook, datasource, field.id)
          .then(() => done(`已刪除「${field.caption}」`))
          .catch(fail),
    });
  }
  return items;
}

function useField(datasource: string, field: string): CatalogField | undefined {
  const editor = useEditor();
  return editor.catalog.datasources.find((d) => d.id === datasource)?.fields.find((f) => f.id === field);
}

function Actions({ onSave, onClose, disabled = false }: { onSave: () => void; onClose: () => void; disabled?: boolean }) {
  return (
    <div className="dialog-actions">
      <button type="button" className="primary" disabled={disabled} onClick={onSave}>
        確定
      </button>
      <button type="button" onClick={onClose}>
        取消
      </button>
    </div>
  );
}

/** 執行儲存；成功時重新讀取資料窗格、關閉，失敗時顯示原因。 */
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

// ---------------------------------------------------------------- 重新命名、註解、格式

export function RenameDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const target = useField(datasource, field);
  const [caption, setCaption] = useState(target?.caption ?? "");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const save = () =>
    run(() => datasourceApi.updateField(editor.workbook, datasource, field, { caption: caption.trim() }), `已改名為「${caption.trim()}」`);
  return (
    <Modal title={`重新命名：${target.caption}`} onClose={onClose}>
      <label className="admin-field">
        名稱
        <input aria-label="欄位名稱" value={caption} autoFocus onChange={(e) => setCaption(e.target.value)} onKeyDown={(e) => e.key === "Enter" && caption.trim() && save()} />
      </label>
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || !caption.trim()} />
    </Modal>
  );
}

export function CommentDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const target = useField(datasource, field);
  const [comment, setComment] = useState(target?.comment ?? "");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  return (
    <Modal title={`註解：${target.caption}`} onClose={onClose}>
      <p className="hint">註解顯示在資料窗格的提示中（與 Tableau 的「預設屬性＞註解」相同）。</p>
      <textarea aria-label="註解" className="field-comment" rows={5} value={comment} onChange={(e) => setComment(e.target.value)} />
      {error && <p className="error">{error}</p>}
      <Actions
        onSave={() =>
          run(() => editApi.saveFieldProperties(editor.workbook, datasource, field, { comment: comment.trim() || null }), "已儲存註解")
        }
        onClose={onClose}
        disabled={busy}
      />
    </Modal>
  );
}

export const MONTHS = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"];
const WEEK_DAYS: [string, string][] = [
  ["sunday", "星期日"],
  ["monday", "星期一"],
  ["tuesday", "星期二"],
  ["wednesday", "星期三"],
  ["thursday", "星期四"],
  ["friday", "星期五"],
  ["saturday", "星期六"],
];

/** 日期欄位的會計年度起始月（預設屬性）：沿用資料源或指定月份。 */
export function FiscalDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const target = useField(datasource, field);
  const inherited = editor.catalog.datasources.find((d) => d.id === datasource)?.settings?.fiscal_year_start ?? 1;
  const [month, setMonth] = useState<number | null>(target?.fiscal_year_start ?? null);
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  return (
    <Modal title={`會計年度起始：${target.caption}`} onClose={onClose}>
      <p className="hint">年、季與週數依會計年度計算，年與季加上 FY；會計年度以結束的年份命名（四月起算時 2004 年 6 月屬於 FY 2005）。</p>
      <label className="inline">
        起始月
        <select aria-label="會計年度的起始月" value={month ?? ""} onChange={(e) => setMonth(e.target.value ? Number(e.target.value) : null)}>
          <option value="">沿用資料源（{MONTHS[inherited - 1]}）</option>
          {MONTHS.map((label, i) => (
            <option key={label} value={i + 1}>
              {label}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      <Actions
        onSave={() =>
          run(
            () => editApi.saveFieldProperties(editor.workbook, datasource, field, { fiscal_year_start: month }),
            `已設定「${target.caption}」的會計年度起始月`,
          )
        }
        onClose={onClose}
        disabled={busy}
      />
    </Modal>
  );
}

/** 資料源的日期屬性（Tableau 的「日期屬性」）：週起始日與會計年度起始月。 */
export function DatePropertiesDialog({ datasource, onClose, onSaved }: { datasource: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const ds = editor.catalog.datasources.find((d) => d.id === datasource);
  const [weekStart, setWeekStart] = useState(ds?.settings?.week_start ?? "sunday");
  const [month, setMonth] = useState(ds?.settings?.fiscal_year_start ?? 1);
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!ds) return null;
  return (
    <Modal title={`日期屬性：${ds.caption}`} onClose={onClose}>
      <label className="inline">
        週的起始日
        <select aria-label="週的起始日" value={weekStart} onChange={(e) => setWeekStart(e.target.value)}>
          {WEEK_DAYS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label className="inline">
        會計年度的起始月
        <select aria-label="會計年度的起始月" value={month} onChange={(e) => setMonth(Number(e.target.value))}>
          {MONTHS.map((label, i) => (
            <option key={label} value={i + 1}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <p className="hint">日期欄位可以在「預設屬性＞會計年度起始…」另外指定。</p>
      {error && <p className="error">{error}</p>}
      <Actions
        onSave={() =>
          run(
            () => editApi.saveDateProperties(editor.workbook, datasource, { week_start: weekStart, fiscal_year_start: month }),
            "已設定日期屬性",
          )
        }
        onClose={onClose}
        disabled={busy}
      />
    </Modal>
  );
}

/** 合併欄位（Tableau 的「建立＞合併欄位」）：勾選兩個以上的維度，依勾選的順序合併。 */
export function CombinedDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const ds = editor.catalog.datasources.find((d) => d.id === datasource);
  const [picked, setPicked] = useState<string[]>([field]);
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!ds) return null;
  const candidates = ds.fields.filter((f) => isDimension(f) && f.kind !== "set" && f.datatype !== "spatial" && !f.hidden);
  const caption = (id: string) => ds.fields.find((f) => f.id === id)?.caption ?? id;
  return (
    <Modal title="建立合併欄位" onClose={onClose}>
      <p className="hint">勾選要合併的維度（依勾選的順序）；成員為各欄位的值以「, 」連接，例如「East, Chair」。</p>
      <ul className="member-list combined-list" aria-label="合併的維度">
        {candidates.map((f) => (
          <li key={f.id}>
            <label>
              <input
                type="checkbox"
                checked={picked.includes(f.id)}
                onChange={(e) => setPicked((all) => (e.target.checked ? [...all, f.id] : all.filter((x) => x !== f.id)))}
              />{" "}
              {f.caption}
            </label>
          </li>
        ))}
      </ul>
      <p className="muted">名稱：{picked.length >= 2 ? `${picked.map(caption).join(" & ")} (Combined)` : "（請勾選兩個以上的維度）"}</p>
      {error && <p className="error">{error}</p>}
      <Actions
        onSave={() =>
          run(async () => {
            const created = await editApi.createCombined(editor.workbook, datasource, picked);
            return created;
          }, "已建立合併欄位")
        }
        onClose={onClose}
        disabled={busy || picked.length < 2}
      />
    </Modal>
  );
}

/** 建立資料夾（選取的欄位移進去）或改名。 */
export function FolderDialog({
  datasource,
  fields,
  folder,
  onClose,
  onSaved,
}: {
  datasource: string;
  fields: string[];
  folder: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const [name, setName] = useState(folder ?? "");
  const { error, busy, run } = useSave(onClose, onSaved);
  const save = () =>
    run(
      () =>
        folder === null
          ? editApi.createFolder(editor.workbook, datasource, name.trim(), fields)
          : editApi.renameFolder(editor.workbook, datasource, folder, name.trim()),
      folder === null ? `已建立資料夾「${name.trim()}」` : `已改名為「${name.trim()}」`,
    );
  return (
    <Modal title={folder === null ? "建立資料夾" : `重新命名資料夾：${folder}`} onClose={onClose}>
      <label className="inline">
        名稱
        <input
          aria-label="資料夾名稱"
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && name.trim() && save()}
        />
      </label>
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || !name.trim()} />
    </Modal>
  );
}

export function FieldFormatDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const target = useField(datasource, field);
  const current = parseFormat(target?.format ?? undefined);
  const [kind, setKind] = useState<FormatKind>(current.kind);
  const [decimals, setDecimals] = useState(current.decimals);
  const [grouping, setGrouping] = useState(current.grouping);
  const [units, setUnits] = useState(current.units);
  const [symbol, setSymbol] = useState(current.symbol);
  const [dateFormat, setDateFormat] = useState(target?.format ?? "");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const date = isDate(target);
  const text = date ? dateFormat.trim() || null : formatString(kind, decimals, grouping, units, symbol);
  const save = () => run(() => editApi.saveFieldProperties(editor.workbook, datasource, field, { format: text }), `已設定「${target.caption}」的預設格式`);
  return (
    <Modal title={`預設格式：${target.caption}`} onClose={onClose}>
      <div className="format-dialog">
        {date ? (
          <label className="inline">
            日期格式
            <input aria-label="日期格式" value={dateFormat} placeholder="例如 yyyy/mm/dd（空白為自動）" onChange={(e) => setDateFormat(e.target.value)} />
          </label>
        ) : (
          <>
            <label className="inline">
              格式
              <select aria-label="數字格式" value={kind} onChange={(e) => setKind(e.target.value as FormatKind)}>
                <option value="auto">自動</option>
                <option value="number">數字（自訂）</option>
                <option value="currency">貨幣（自訂）</option>
                <option value="percent">百分比</option>
              </select>
            </label>
            {kind !== "auto" && (
              <>
                <label className="inline">
                  小數位數
                  <input type="number" min={0} max={10} aria-label="小數位數" value={decimals} onChange={(e) => setDecimals(Math.max(0, Math.min(10, Number(e.target.value))))} />
                </label>
                {kind !== "percent" && (
                  <>
                    <label className="inline">
                      顯示單位
                      <select aria-label="顯示單位" value={units} onChange={(e) => setUnits(Number(e.target.value))}>
                        <option value={0}>無</option>
                        <option value={1}>千（K）</option>
                        <option value={2}>百萬（M）</option>
                        <option value={3}>十億（B）</option>
                      </select>
                    </label>
                    <label className="inline">
                      <input type="checkbox" checked={grouping} onChange={(e) => setGrouping(e.target.checked)} /> 千分位
                    </label>
                  </>
                )}
                {kind === "currency" && (
                  <label className="inline">
                    符號
                    <input aria-label="貨幣符號" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
                  </label>
                )}
              </>
            )}
          </>
        )}
        <p className="hint">格式字串：{text ?? "（自動）"}；膠囊上另外設定的格式優先。</p>
        {error && <p className="error">{error}</p>}
        <Actions onSave={save} onClose={onClose} disabled={busy} />
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- 別名

export function AliasDialog({ datasource, field, onClose, onSaved }: { datasource: string; field: string; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const target = useField(datasource, field);
  const { values, error: loadError } = useMembers(editor.workbook, field);
  const [labels, setLabels] = useState<Record<string, string>>(() =>
    Object.fromEntries((target?.aliases ?? []).map((a) => [memberKey(a.value), a.label])),
  );
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!target) return null;
  const members = values ?? [];
  // 已有別名、但目前資料中沒有的值也列出（與 Tableau 相同保留）
  const extra = (target.aliases ?? []).map((a) => a.value).filter((v) => !members.some((m) => memberKey(m) === memberKey(v)));
  const rows = [...members, ...extra];
  const save = () =>
    run(
      () =>
        editApi.saveAliases(
          editor.workbook,
          datasource,
          field,
          rows.filter((v) => (labels[memberKey(v)] ?? "").trim()).map((v) => ({ value: v, label: labels[memberKey(v)] })),
        ),
      `已設定「${target.caption}」的別名`,
    );
  return (
    <Modal title={`編輯別名：${target.caption}`} onClose={onClose} wide>
      {loadError && <p className="error">{loadError}</p>}
      {values === null && !loadError && <p className="hint">讀取成員中…</p>}
      <div className="alias-table-wrap">
        <table className="alias-table">
          <thead>
            <tr>
              <th>成員</th>
              <th>別名</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((v) => (
              <tr key={memberKey(v)} data-member={memberText(v)}>
                <td>{memberText(v)}</td>
                <td>
                  <input
                    aria-label={`${memberText(v)} 的別名`}
                    value={labels[memberKey(v)] ?? ""}
                    placeholder={memberText(v)}
                    onChange={(e) => setLabels((l) => ({ ...l, [memberKey(v)]: e.target.value }))}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button type="button" onClick={() => setLabels({})}>
        清除別名
      </button>
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || values === null} />
    </Modal>
  );
}

// ---------------------------------------------------------------- 群組

interface GroupDraft {
  name: string;
  members: Scalar[];
}

export function GroupDialog({
  datasource,
  source,
  field,
  members: preset,
  onClose,
  onSaved,
}: {
  datasource: string;
  source: string;
  field: string | null;
  /** 從檢視上選取的標記建立群組時，這些成員先成為一組 */
  members?: Scalar[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const target = useField(datasource, source);
  const existing = useField(datasource, field ?? "");
  const { values, error: loadError } = useMembers(editor.workbook, source);
  const [caption, setCaption] = useState(existing?.caption ?? `${target?.caption ?? ""} (group)`);
  const [groups, setGroups] = useState<GroupDraft[]>(() =>
    existing?.group
      ? existing.group.groups.map((g) => ({ name: String(g.name), members: g.members }))
      : preset?.length
        ? [{ name: preset.map(memberText).join(", "), members: preset }]
        : [],
  );
  const [other, setOther] = useState<string | null>(existing?.group?.other != null ? String(existing.group.other) : null);
  const [picked, setPicked] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const { error, busy, run } = useSave(onClose, onSaved);
  const grouped = useMemo(() => new Set(groups.flatMap((g) => g.members.map(memberKey))), [groups]);
  if (!target) return null;
  const all = values ?? [];
  const term = search.trim().toLowerCase();
  const ungrouped = all.filter((v) => !grouped.has(memberKey(v)) && (!term || memberText(v).toLowerCase().includes(term)));
  const group = () => {
    const chosen = all.filter((v) => picked.includes(memberKey(v)));
    if (!chosen.length) return;
    setGroups((list) => [...list, { name: chosen.map(memberText).join(", "), members: chosen }]);
    setPicked([]);
  };
  const save = () =>
    run(
      () =>
        editApi.saveGroup(editor.workbook, datasource, {
          source,
          caption,
          groups: groups.map((g) => ({ name: g.name, members: g.members })),
          other,
          field,
        }),
      `已儲存群組「${caption.trim() || target.caption}」`,
    );
  return (
    <Modal title={`${field ? "編輯群組" : "建立群組"}：${target.caption}`} onClose={onClose} wide>
      <label className="admin-field">
        欄位名稱
        <input aria-label="群組的名稱" value={caption} onChange={(e) => setCaption(e.target.value)} />
      </label>
      <div className="group-editor">
        <section aria-label="成員">
          <h3>成員</h3>
          <input type="search" aria-label="搜尋成員" placeholder="搜尋" value={search} onChange={(e) => setSearch(e.target.value)} />
          {loadError && <p className="error">{loadError}</p>}
          {values === null && !loadError && <p className="hint">讀取成員中…</p>}
          <ul className="member-list">
            {ungrouped.map((v) => (
              <li key={memberKey(v)}>
                <label>
                  <input
                    type="checkbox"
                    aria-label={memberText(v)}
                    checked={picked.includes(memberKey(v))}
                    onChange={(e) => setPicked((p) => (e.target.checked ? [...p, memberKey(v)] : p.filter((k) => k !== memberKey(v))))}
                  />{" "}
                  {memberText(v)}
                </label>
              </li>
            ))}
          </ul>
          <button type="button" disabled={!picked.length} onClick={group}>
            群組所選的成員
          </button>
        </section>
        <section aria-label="群組">
          <h3>群組</h3>
          {groups.length === 0 && <p className="hint">勾選左邊的成員後按「群組所選的成員」。</p>}
          <ul className="group-list">
            {groups.map((g, i) => (
              <li key={i} data-group={g.name}>
                <input aria-label="群組名稱" value={g.name} onChange={(e) => setGroups((list) => list.map((x, n) => (n === i ? { ...x, name: e.target.value } : x)))} />
                <button type="button" className="link danger-link" onClick={() => setGroups((list) => list.filter((_, n) => n !== i))}>
                  取消群組
                </button>
                <div className="group-members">
                  {g.members.map((m) => (
                    <span key={memberKey(m)} className="chip">
                      {memberText(m)}
                      <button
                        type="button"
                        className="link"
                        aria-label={`從「${g.name}」移除 ${memberText(m)}`}
                        onClick={() =>
                          setGroups((list) =>
                            list
                              .map((x, n) => (n === i ? { ...x, members: x.members.filter((y) => memberKey(y) !== memberKey(m)) } : x))
                              .filter((x) => x.members.length > 0),
                          )
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
          <label>
            <input type="checkbox" checked={other !== null} onChange={(e) => setOther(e.target.checked ? "Other" : null)} /> 包含「其他」（其餘成員）
          </label>
          {other !== null && <input aria-label="其他的名稱" value={other} onChange={(e) => setOther(e.target.value)} />}
        </section>
      </div>
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || values === null} />
    </Modal>
  );
}

// ---------------------------------------------------------------- 數值分組

export function BinDialog({
  datasource,
  source,
  field,
  onClose,
  onSaved,
}: {
  datasource: string;
  source: string;
  field: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const target = useField(datasource, source);
  const existing = useField(datasource, field ?? "");
  const size0 = existing?.bin?.size;
  const [caption, setCaption] = useState(existing?.caption ?? `${target?.caption ?? ""} (bin)`);
  const [size, setSize] = useState<string>(typeof size0 === "number" ? String(size0) : "10");
  const [parameter, setParameter] = useState<string>(size0 && typeof size0 === "object" ? size0.parameter : "");
  const [stats, setStats] = useState<{ min: number; max: number; count: number } | null>(null);
  const { error, busy, run } = useSave(onClose, onSaved);
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      editApi.domain(editor.workbook, plainUse(source), { kind: "range" }),
      editApi.domain(editor.workbook, plainUse(source), { kind: "values" }),
    ])
      .then(([range, values]) => {
        if (cancelled || range.kind !== "range" || typeof range.min !== "number" || typeof range.max !== "number") return;
        setStats({ min: range.min, max: range.max, count: values.kind === "values" ? values.values.length : 0 });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [editor.workbook, source]);
  if (!target) return null;
  const numbers = editor.catalog.parameters.filter((p) => p.datatype === "integer" || p.datatype === "real");
  // Tableau 的建議：分組數 = 3 + log2(n) × log(n)，大小 = (最大 − 最小) ÷ 分組數
  const suggest = () => {
    if (!stats || stats.count < 2) return;
    const bins = 3 + Math.log2(stats.count) * Math.log10(stats.count);
    const raw = (stats.max - stats.min) / bins;
    const rounded = raw >= 1 ? Math.round(raw) : Number(raw.toPrecision(2));
    setSize(String(rounded > 0 ? rounded : 1));
    setParameter("");
  };
  const value = Number(size);
  const save = () =>
    run(
      () =>
        editApi.saveBins(editor.workbook, datasource, {
          source,
          caption,
          size: parameter ? null : value,
          parameter: parameter || null,
          field,
        }),
      `已儲存數值分組「${caption.trim() || target.caption}」`,
    );
  return (
    <Modal title={`${field ? "編輯數值分組" : "建立數值分組"}：${target.caption}`} onClose={onClose}>
      <label className="admin-field">
        新欄位的名稱
        <input aria-label="數值分組的名稱" value={caption} onChange={(e) => setCaption(e.target.value)} />
      </label>
      <label className="admin-field">
        分組的大小
        <input type="number" aria-label="分組的大小" min={0} step="any" value={size} disabled={parameter !== ""} onChange={(e) => setSize(e.target.value)} />
      </label>
      {numbers.length > 0 && (
        <label className="admin-field">
          或以參數決定
          <select aria-label="分組大小的參數" value={parameter} onChange={(e) => setParameter(e.target.value)}>
            <option value="">（不使用參數）</option>
            {numbers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.caption}
              </option>
            ))}
          </select>
        </label>
      )}
      <fieldset className="bin-stats">
        <legend>值的範圍</legend>
        {stats ? (
          <p className="hint">
            最小 {stats.min}、最大 {stats.max}、差 {stats.max - stats.min}、相異值 {stats.count.toLocaleString()} 個
          </p>
        ) : (
          <p className="hint">讀取中…</p>
        )}
        <button type="button" disabled={!stats || stats.count < 2} onClick={suggest}>
          建議分組的大小
        </button>
      </fieldset>
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || (!parameter && !(value > 0))} />
    </Modal>
  );
}

// ---------------------------------------------------------------- 集合

type SetKind = "members" | "condition" | "top_n" | "combined";
const SET_OPS: [string, string][] = [
  ["union", "兩個集合中的所有成員"],
  ["intersect", "兩個集合中共同的成員"],
  ["left_only", "只在左側集合中的成員"],
  ["right_only", "只在右側集合中的成員"],
];
const COMPARISONS = [">", ">=", "<", "<=", "=", "!="];

export function SetDialog({
  datasource,
  levels,
  field,
  members: preset,
  combine,
  onClose,
  onSaved,
}: {
  datasource: string;
  levels: string[];
  field: string | null;
  /** 從檢視上選取的標記建立集合時的成員（各層級的值組合） */
  members?: Scalar[][];
  /** 建立合併集合：左側的集合 */
  combine?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const ds = editor.catalog.datasources.find((d) => d.id === datasource);
  const levelFields = levels.map((id) => ds?.fields.find((f) => f.id === id)).filter((f): f is CatalogField => !!f);
  const existing = ds?.fields.find((f) => f.id === field);
  const rule = existing?.set?.rule;
  const single = levelFields.length === 1;
  const { values, error: loadError } = useMembers(editor.workbook, levels[0]);
  const measures = (ds?.fields ?? []).filter((f) => (f.role === "measure" || f.level === "aggregate") && !f.hidden);
  const [caption, setCaption] = useState(existing?.caption ?? `${levelFields[0]?.caption ?? ""} Set`);
  const [kind, setKind] = useState<SetKind>((rule?.kind as SetKind | undefined) ?? (combine ? "combined" : "members"));
  // 合併集合：同樣維度上的其他集合
  const sameLevels = (f: CatalogField) => JSON.stringify((f.set?.levels ?? []).map((l) => l.field)) === JSON.stringify(levels);
  const otherSets = (ds?.fields ?? []).filter((f) => f.kind === "set" && f.set && f.id !== field && sameLevels(f));
  const [left, setLeft] = useState(rule?.kind === "combined" ? rule.left : (combine ?? otherSets[0]?.id ?? ""));
  const [right, setRight] = useState(rule?.kind === "combined" ? rule.right : (otherSets.find((f) => f.id !== (combine ?? otherSets[0]?.id))?.id ?? ""));
  const [combineOp, setCombineOp] = useState<string>(rule?.kind === "combined" ? rule.op : "union");
  const [exclude, setExclude] = useState(rule?.kind === "members" && rule.mode === "exclude");
  const [picked, setPicked] = useState<string[]>(() =>
    rule?.kind === "members" ? rule.members.map((m) => memberKey(m[0])) : (preset ?? []).map((m) => memberKey(m[0])),
  );
  const [condition, setCondition] = useState(rule?.kind === "condition" ? rule.condition : "");
  const [byField, setByField] = useState(measures[0]?.id ?? "");
  const [byAgg, setByAgg] = useState("sum");
  const [op, setOp] = useState(">");
  const [threshold, setThreshold] = useState("0");
  const [direction, setDirection] = useState<"top" | "bottom">(rule?.kind === "top_n" ? rule.direction : "top");
  const [count, setCount] = useState(rule?.kind === "top_n" && typeof rule.count === "number" ? String(rule.count) : "10");
  const [by, setBy] = useState(rule?.kind === "top_n" ? rule.by : "");
  const [search, setSearch] = useState("");
  const { error, busy, run } = useSave(onClose, onSaved);
  if (!ds || levelFields.length !== levels.length) return null;
  const fieldRef = (id: string) => {
    const f = ds.fields.find((x) => x.id === id);
    return f ? `[${f.caption.replace(/]/g, "]]")}]` : "";
  };
  const aggregate = (id: string, agg: string) => {
    const f = ds.fields.find((x) => x.id === id);
    if (!f) return "";
    return f.level === "aggregate" ? fieldRef(id) : `${agg.toUpperCase()}(${fieldRef(id)})`;
  };
  const conditionText = condition.trim() || (byField ? `${aggregate(byField, byAgg)} ${op} ${Number(threshold) || 0}` : "");
  const byText = by.trim() || (byField ? aggregate(byField, byAgg) : "");
  const all = values ?? [];
  const term = search.trim().toLowerCase();
  const shown = all.filter((v) => !term || memberText(v).toLowerCase().includes(term));
  const body = (): SetRuleBody => {
    if (kind === "combined") return { kind: "combined", left, right, op: combineOp as "union" | "intersect" | "left_only" | "right_only" };
    if (!single || kind === "members") {
      const rows = single ? all.filter((v) => picked.includes(memberKey(v))).map((v) => [v]) : (preset ?? (rule?.kind === "members" ? rule.members : []));
      return { kind: "members", mode: exclude ? "exclude" : "include", members: rows };
    }
    if (kind === "condition") return { kind: "condition", condition: conditionText };
    return { kind: "top_n", direction, count: Math.max(1, Math.round(Number(count) || 1)), by: byText };
  };
  const save = () =>
    run(
      () => editApi.saveSet(editor.workbook, datasource, { levels, caption, rule: body(), field }),
      `已儲存集合「${caption.trim() || levelFields[0].caption}」`,
    );
  return (
    <Modal title={`${field ? "編輯集合" : "建立集合"}：${levelFields.map((f) => f.caption).join("、")}`} onClose={onClose} wide>
      <label className="admin-field">
        名稱
        <input aria-label="集合的名稱" value={caption} onChange={(e) => setCaption(e.target.value)} />
      </label>
      {(single || otherSets.length >= 2) && (
        <div className="tabs" role="tablist">
          {(
            [
              ["members", "一般"],
              ...(single ? [["condition", "條件"], ["top_n", "頂部"]] : []),
              ...(otherSets.length >= 2 || kind === "combined" ? [["combined", "合併"]] : []),
            ] as [SetKind, string][]
          ).map(([k, label]) => (
            <button key={k} type="button" role="tab" aria-selected={kind === k} className={kind === k ? "active" : ""} onClick={() => setKind(k)}>
              {label}
            </button>
          ))}
        </div>
      )}
      {kind === "combined" && (
        <div className="set-rule">
          <label className="admin-field">
            左側的集合
            <select aria-label="左側的集合" value={left} onChange={(e) => setLeft(e.target.value)}>
              {otherSets.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.caption}
                </option>
              ))}
            </select>
          </label>
          <label className="admin-field">
            右側的集合
            <select aria-label="右側的集合" value={right} onChange={(e) => setRight(e.target.value)}>
              {otherSets.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.caption}
                </option>
              ))}
            </select>
          </label>
          {SET_OPS.map(([value, label]) => (
            <label className="inline" key={value}>
              <input type="radio" name="set-op" checked={combineOp === value} onChange={() => setCombineOp(value)} /> {label}
            </label>
          ))}
          {otherSets.length < 2 && <p className="hint">這個維度上要有兩個集合才能合併。</p>}
        </div>
      )}
      {kind !== "combined" && (!single || kind === "members") && (
        <div className="set-members">
          {single ? (
            <>
              <input type="search" aria-label="搜尋成員" placeholder="搜尋" value={search} onChange={(e) => setSearch(e.target.value)} />
              {loadError && <p className="error">{loadError}</p>}
              {values === null && !loadError && <p className="hint">讀取成員中…</p>}
              <div className="pane-actions">
                <button type="button" onClick={() => setPicked(all.map(memberKey))}>
                  全部
                </button>
                <button type="button" onClick={() => setPicked([])}>
                  無
                </button>
              </div>
              <ul className="member-list">
                {shown.map((v) => (
                  <li key={memberKey(v)}>
                    <label>
                      <input
                        type="checkbox"
                        aria-label={memberText(v)}
                        checked={picked.includes(memberKey(v))}
                        onChange={(e) => setPicked((p) => (e.target.checked ? [...p, memberKey(v)] : p.filter((k) => k !== memberKey(v))))}
                      />{" "}
                      {memberText(v)}
                    </label>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="hint">
              成員為檢視上選取的 {(preset ?? (rule?.kind === "members" ? rule.members : [])).length} 個值組合（
              {levelFields.map((f) => f.caption).join("、")}）。
            </p>
          )}
          <label>
            <input type="checkbox" checked={exclude} onChange={(e) => setExclude(e.target.checked)} /> 排除（所選以外的成員在集合中）
          </label>
        </div>
      )}
      {single && kind === "condition" && (
        <div className="set-rule">
          <p className="hint">條件成立的成員在集合中（以彙總計算，例如 SUM([Profit]) &gt; 0）。</p>
          <div className="inline-fields">
            <select aria-label="條件的欄位" value={byField} onChange={(e) => setByField(e.target.value)}>
              {measures.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.caption}
                </option>
              ))}
            </select>
            <select aria-label="條件的彙總" value={byAgg} onChange={(e) => setByAgg(e.target.value)}>
              {AGGREGATIONS.slice(0, 7).map(([agg, label]) => (
                <option key={agg} value={agg}>
                  {label}
                </option>
              ))}
            </select>
            <select aria-label="比較" value={op} onChange={(e) => setOp(e.target.value)}>
              {COMPARISONS.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
            <input type="number" aria-label="比較的值" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
          </div>
          <label className="admin-field">
            或以公式
            <input aria-label="條件的公式" value={condition} placeholder={conditionText} onChange={(e) => setCondition(e.target.value)} />
          </label>
        </div>
      )}
      {single && kind === "top_n" && (
        <div className="set-rule">
          <div className="inline-fields">
            <select aria-label="頂部或底部" value={direction} onChange={(e) => setDirection(e.target.value as "top" | "bottom")}>
              <option value="top">頂部</option>
              <option value="bottom">底部</option>
            </select>
            <input type="number" min={1} aria-label="數量" value={count} onChange={(e) => setCount(e.target.value)} />
            <span>依</span>
            <select aria-label="排名的欄位" value={byField} onChange={(e) => setByField(e.target.value)}>
              {measures.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.caption}
                </option>
              ))}
            </select>
            <select aria-label="排名的彙總" value={byAgg} onChange={(e) => setByAgg(e.target.value)}>
              {AGGREGATIONS.slice(0, 7).map(([agg, label]) => (
                <option key={agg} value={agg}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <label className="admin-field">
            或以公式
            <input aria-label="排名的公式" value={by} placeholder={byText} onChange={(e) => setBy(e.target.value)} />
          </label>
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <Actions onSave={save} onClose={onClose} disabled={busy || (single && kind === "members" && values === null) || (kind === "combined" && (!left || !right || left === right))} />
    </Modal>
  );
}
