// 動作（Tableau 的「儀表板＞動作」）：篩選、網址、參數、切換與集合動作的新增、修改、刪除。

import { useEffect, useMemo, useState } from "react";
import { Modal } from "../Server";
import type { Action, Dashboard, FieldUse } from "../types";
import { editApi, type Outline } from "./api";
import { walkZones } from "./dashboardModel";
import { type Catalog, pillLabel, useKey } from "./model";

type Kind = Action["kind"];

const KINDS: [Kind, string][] = [
  ["filter", "篩選"],
  ["url", "網址"],
  ["parameter", "變更參數"],
  ["navigate", "前往工作表"],
  ["set", "變更集合值"],
];

const ACTIVATIONS: [Action["activation"], string][] = [
  ["select", "選取"],
  ["hover", "暫留"],
  ["menu", "功能表"],
];

interface Draft {
  id: string | null;
  caption: string;
  kind: Kind;
  activation: Action["activation"];
  scope: "dashboard" | "all";
  sources: string[];
  targets: string[];
  clearing: Action["clearing"];
  fields: string[];
  allFields: boolean;
  url: string;
  escape: boolean;
  parameter: string;
  parameterField: string;
  aggregation: string;
  parameterClearing: "keep" | "assign";
  targetView: string;
  setTarget: string;
  setBehavior: "assign" | "add" | "remove";
  setClearing: "keep" | "add_all" | "remove_all";
}

function draftOf(action: Action | null, dashboard: Dashboard, onBoard: string[]): Draft {
  return {
    id: action?.id ?? null,
    caption: action?.caption ?? "動作",
    kind: action?.kind ?? "filter",
    activation: action?.activation ?? "select",
    scope: action && action.dashboard === null ? "all" : "dashboard",
    sources: action?.sources ?? onBoard.slice(0, 1),
    targets: action?.targets ?? onBoard.slice(1),
    clearing: action?.clearing ?? "show_all",
    fields: [],
    allFields: true,
    url: action?.url?.template ?? "https://",
    escape: action?.url?.escape ?? true,
    parameter: action?.parameter?.parameter ?? "",
    parameterField: action?.parameter?.field ?? "",
    aggregation: action?.parameter?.aggregation ?? "attr",
    parameterClearing: action?.parameter?.clearing ?? "keep",
    targetView: action?.target_dashboard ? `d:${action.target_dashboard}` : action?.target_worksheet ? `s:${action.target_worksheet}` : `d:${dashboard.id}`,
    setTarget: action?.set_action?.target ?? "",
    setBehavior: action?.set_action?.behavior ?? "assign",
    setClearing: action?.set_action?.clearing ?? "keep",
  };
}

export function ActionsDialog({
  workbook,
  dashboard,
  outline,
  onClose,
  onSaved,
}: {
  workbook: string;
  dashboard: Dashboard;
  outline: Outline;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    editApi.catalog(workbook).then(setCatalog).catch((e: Error) => setError(e.message));
  }, [workbook]);
  const onBoard = useMemo(() => {
    const ids: string[] = [];
    walkZones(dashboard.zones, (z) => {
      if (z.kind === "worksheet" && z.worksheet && !ids.includes(z.worksheet)) ids.push(z.worksheet);
    });
    return ids;
  }, [dashboard]);
  const sheetName = (id: string) => outline.worksheets.find((w) => w.id === id)?.name ?? id;
  const actions = outline.actions.filter((a) => a.dashboard === dashboard.id || a.dashboard === null);
  const allFields = new Map((catalog?.datasources ?? []).flatMap((d) => d.fields.map((f) => [f.id, f] as const)));
  const sets = (catalog?.datasources ?? []).flatMap((d) => d.fields.filter((f) => f.kind === "set"));

  const set = (patch: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...patch } : d));
  // 來源工作表檢視中的維度（篩選的欄位、網址與參數的來源欄位）
  const sourceDims: FieldUse[] = [];
  for (const id of draft?.sources ?? []) {
    for (const use of outline.worksheets.find((w) => w.id === id)?.dimensions ?? []) {
      if (!sourceDims.some((u) => useKey(u) === useKey(use))) sourceDims.push(use);
    }
  }
  const label = (use: FieldUse) => pillLabel(use, allFields, []);

  const save = async () => {
    if (!draft) return;
    setError(null);
    const body: Record<string, unknown> = {
      id: draft.id,
      caption: draft.caption,
      kind: draft.kind,
      activation: draft.activation,
      dashboard: draft.scope === "dashboard" ? dashboard.id : null,
      sources: draft.sources,
      targets: draft.kind === "filter" ? draft.targets : [],
      clearing: draft.clearing,
      fields: draft.kind === "filter" && !draft.allFields ? draft.fields : [],
      url: null,
      parameter: null,
      set_action: null,
      target_dashboard: null,
      target_worksheet: null,
    };
    if (draft.kind === "url") {
      // 網址中的 <欄位> 依來源工作表的維度對應成欄位鍵；<參數名稱> 對應成參數
      const placeholders = [...draft.url.matchAll(/<([^<>]+)>/g)].map((m) => m[0]);
      const fields = placeholders
        .map((placeholder) => {
          const name = placeholder.slice(1, -1).replace(/^\[|\]$/g, "");
          const dim = sourceDims.find((u) => label(u) === name || allFields.get(u.field)?.caption === name);
          if (dim) return { placeholder, key: useKey(dim) };
          const parameter = outline.parameters.find((p) => p.caption === name.replace(/^Parameters\./, ""));
          return parameter ? { placeholder, key: `param:${parameter.id}` } : null;
        })
        .filter(Boolean);
      body.url = { template: draft.url, fields, escape: draft.escape, delimiter: "," };
    } else if (draft.kind === "parameter") {
      body.parameter = {
        parameter: draft.parameter,
        field: draft.parameterField,
        aggregation: draft.aggregation,
        clearing: draft.parameterClearing,
        clear_value: null,
      };
    } else if (draft.kind === "navigate") {
      const [kind, target] = draft.targetView.split(":");
      body.target_dashboard = kind === "d" ? target : null;
      body.target_worksheet = kind === "s" ? target : null;
    } else if (draft.kind === "set") {
      body.set_action = { target: draft.setTarget, behavior: draft.setBehavior, clearing: draft.setClearing };
    }
    try {
      await editApi.saveAction(workbook, body);
      setDraft(null);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (action: Action) => {
    if (!window.confirm(`刪除動作「${action.caption}」？`)) return;
    try {
      await editApi.removeAction(workbook, action.id);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const checkboxes = (ids: string[], chosen: string[], onChange: (next: string[]) => void, labelOf: (id: string) => string, name: string) => (
    <div className="check-list" role="group" aria-label={name}>
      {ids.map((id) => (
        <label key={id}>
          <input
            type="checkbox"
            checked={chosen.includes(id)}
            onChange={(e) => onChange(e.target.checked ? [...chosen, id] : chosen.filter((c) => c !== id))}
          />{" "}
          {labelOf(id)}
        </label>
      ))}
    </div>
  );

  const sheetChoices = draft?.scope === "all" ? outline.worksheets.map((w) => w.id) : onBoard;
  return (
    <Modal title="動作" onClose={onClose} wide>
      {!draft && (
        <>
          <table className="list">
            <thead>
              <tr>
                <th>名稱</th>
                <th>種類</th>
                <th>來源</th>
                <th>執行方式</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {actions.map((a) => (
                <tr key={a.id}>
                  <td>{a.caption}</td>
                  <td>{KINDS.find(([k]) => k === a.kind)?.[1] ?? a.kind}</td>
                  <td>{a.sources.map(sheetName).join("、")}</td>
                  <td>{ACTIVATIONS.find(([k]) => k === a.activation)?.[1]}</td>
                  <td className="actions">
                    <button type="button" onClick={() => setDraft(draftOf(a, dashboard, onBoard))}>
                      編輯
                    </button>
                    <button type="button" className="danger" onClick={() => void remove(a)}>
                      刪除
                    </button>
                  </td>
                </tr>
              ))}
              {actions.length === 0 && (
                <tr>
                  <td colSpan={5} className="hint">
                    這個 dashboard 還沒有動作
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <div className="dialog-actions">
            <select aria-label="新增動作的種類" id="new-action-kind" defaultValue="filter">
              {KINDS.map(([k, text]) => (
                <option key={k} value={k}>
                  {text}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="primary"
              onClick={() => {
                const kind = (document.getElementById("new-action-kind") as HTMLSelectElement | null)?.value as Kind;
                setDraft({ ...draftOf(null, dashboard, onBoard), kind: kind ?? "filter", caption: `${KINDS.find(([k]) => k === kind)?.[1] ?? ""}動作` });
              }}
            >
              新增動作
            </button>
            <button type="button" onClick={onClose}>
              關閉
            </button>
          </div>
        </>
      )}
      {draft && (
        <div className="action-form">
          <label className="inline">
            名稱
            <input aria-label="動作名稱" value={draft.caption} onChange={(e) => set({ caption: e.target.value })} />
          </label>
          <label className="inline">
            種類
            <select aria-label="動作的種類" value={draft.kind} onChange={(e) => set({ kind: e.target.value as Kind })}>
              {KINDS.map(([k, text]) => (
                <option key={k} value={k}>
                  {text}
                </option>
              ))}
            </select>
          </label>
          <div className="row" role="radiogroup" aria-label="執行方式">
            執行方式
            {ACTIVATIONS.map(([k, text]) => (
              <label key={k}>
                <input type="radio" name="activation" checked={draft.activation === k} onChange={() => set({ activation: k })} /> {text}
              </label>
            ))}
          </div>
          <label className="inline">
            範圍
            <select aria-label="動作的範圍" value={draft.scope} onChange={(e) => set({ scope: e.target.value as Draft["scope"] })}>
              <option value="dashboard">只在這個 dashboard</option>
              <option value="all">所有工作表</option>
            </select>
          </label>
          <div className="prop-group">
            <div>來源工作表</div>
            {checkboxes(sheetChoices, draft.sources, (sources) => set({ sources }), sheetName, "來源工作表")}
          </div>
          {draft.kind === "filter" && (
            <>
              <div className="prop-group">
                <div>目標工作表</div>
                {checkboxes(sheetChoices, draft.targets, (targets) => set({ targets }), sheetName, "目標工作表")}
              </div>
              <div className="row" role="radiogroup" aria-label="清除選取時">
                清除選取時
                {(
                  [
                    ["show_all", "顯示所有值"],
                    ["exclude_all", "排除所有值"],
                    ["keep", "保留篩選的值"],
                  ] as const
                ).map(([k, text]) => (
                  <label key={k}>
                    <input type="radio" name="clearing" checked={draft.clearing === k} onChange={() => set({ clearing: k })} /> {text}
                  </label>
                ))}
              </div>
              <div className="prop-group">
                <label>
                  <input type="checkbox" checked={draft.allFields} onChange={(e) => set({ allFields: e.target.checked })} /> 所有欄位
                </label>
                {!draft.allFields &&
                  checkboxes(
                    sourceDims.map(useKey),
                    draft.fields,
                    (fields) => set({ fields }),
                    (key) => label(sourceDims.find((u) => useKey(u) === key)!),
                    "篩選的欄位",
                  )}
              </div>
            </>
          )}
          {draft.kind === "url" && (
            <>
              <label className="inline">
                網址
                <input aria-label="網址" className="wide" value={draft.url} onChange={(e) => set({ url: e.target.value })} />
              </label>
              <div className="row">
                插入欄位
                {sourceDims.map((u) => (
                  <button key={useKey(u)} type="button" className="link" onClick={() => set({ url: `${draft.url}<${label(u)}>` })}>
                    {label(u)}
                  </button>
                ))}
              </div>
              <label className="inline">
                <input type="checkbox" checked={draft.escape} onChange={(e) => set({ escape: e.target.checked })} /> 網址編碼
              </label>
            </>
          )}
          {draft.kind === "parameter" && (
            <>
              <label className="inline">
                目標參數
                <select aria-label="目標參數" value={draft.parameter} onChange={(e) => set({ parameter: e.target.value })}>
                  <option value="">（選擇參數）</option>
                  {outline.parameters.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.caption}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                來源欄位
                <select aria-label="來源欄位" value={draft.parameterField} onChange={(e) => set({ parameterField: e.target.value })}>
                  <option value="">（選擇欄位）</option>
                  {sourceDims.map((u) => (
                    <option key={useKey(u)} value={useKey(u)}>
                      {label(u)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                彙總
                <select aria-label="參數動作的彙總" value={draft.aggregation} onChange={(e) => set({ aggregation: e.target.value })}>
                  {["attr", "sum", "avg", "min", "max", "count", "countd"].map((a) => (
                    <option key={a} value={a}>
                      {a.toUpperCase()}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                <input type="checkbox" checked={draft.parameterClearing === "keep"} onChange={(e) => set({ parameterClearing: e.target.checked ? "keep" : "assign" })} /> 清除選取時保留目前的值
              </label>
            </>
          )}
          {draft.kind === "navigate" && (
            <label className="inline">
              目標
              <select aria-label="切換的目標" value={draft.targetView} onChange={(e) => set({ targetView: e.target.value })}>
                {outline.dashboards.map((d) => (
                  <option key={d.id} value={`d:${d.id}`}>
                    dashboard：{d.name}
                  </option>
                ))}
                {outline.worksheets.map((w) => (
                  <option key={w.id} value={`s:${w.id}`}>
                    工作表：{w.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {draft.kind === "set" && (
            <>
              <label className="inline">
                目標集合
                <select aria-label="目標集合" value={draft.setTarget} onChange={(e) => set({ setTarget: e.target.value })}>
                  <option value="">（選擇集合）</option>
                  {sets.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.caption}
                    </option>
                  ))}
                </select>
              </label>
              <div className="row" role="radiogroup" aria-label="執行動作時">
                執行動作時
                {(
                  [
                    ["assign", "指派值給集合"],
                    ["add", "將值加入集合"],
                    ["remove", "從集合移除值"],
                  ] as const
                ).map(([k, text]) => (
                  <label key={k}>
                    <input type="radio" name="setBehavior" checked={draft.setBehavior === k} onChange={() => set({ setBehavior: k })} /> {text}
                  </label>
                ))}
              </div>
              <div className="row" role="radiogroup" aria-label="清除選取時">
                清除選取時
                {(
                  [
                    ["keep", "保留集合的值"],
                    ["add_all", "將所有值加入集合"],
                    ["remove_all", "從集合移除所有值"],
                  ] as const
                ).map(([k, text]) => (
                  <label key={k}>
                    <input type="radio" name="setClearing" checked={draft.setClearing === k} onChange={() => set({ setClearing: k })} /> {text}
                  </label>
                ))}
              </div>
            </>
          )}
          {error && <p className="error">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="primary" onClick={() => void save()} disabled={draft.sources.length === 0}>
              確定
            </button>
            <button type="button" onClick={() => setDraft(null)}>
              取消
            </button>
          </div>
        </div>
      )}
      {!draft && error && <p className="error">{error}</p>}
    </Modal>
  );
}
