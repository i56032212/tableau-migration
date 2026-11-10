// 桌面版的 Story 編輯器：故事點的新增、排序、說明文字與顯示的 dashboard 或工作表，Story 的標題、大小與導覽。

import { useState } from "react";
import type { IR, Story, StoryPoint } from "../types";
import { StoryView } from "../Story";
import { editApi } from "./api";
import { useEditHistory, useNotice } from "./history";

function nextPointId(story: Story): string {
  let max = 0;
  for (const p of story.points) {
    const m = /(\d+)$/.exec(p.id);
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `sp${max + 1}`;
}

export function StoryEditor({
  workbook,
  story: id,
  ir,
  onChanged,
  onOpen,
  onClose,
}: {
  workbook: string;
  story: string;
  ir: IR;
  onChanged: () => void;
  onOpen: (story: string) => void;
  onClose: (story: string | null) => void;
}) {
  const [notice, notify] = useNotice();
  const history = useEditHistory<Story>(
    `${workbook}/${id}`,
    () => editApi.story(workbook, id),
    (s) => editApi.saveStory(workbook, s),
    notify,
    (saved, previous) => {
      if (saved.name !== previous.name) onChanged();
    },
  );
  const [selected, setSelected] = useState<string | null>(null);
  const story = history.current;
  if (history.error) return <main className="error">無法編輯：{history.error}</main>;
  if (!story) return <main>載入中…</main>;
  const apply = history.apply;
  const current = story.points.find((p) => p.id === selected) ?? story.points[0];
  const targetOf = (p: StoryPoint) => (p.dashboard ? `d:${p.dashboard}` : `s:${p.worksheet}`);
  const nameOf = (p: StoryPoint) =>
    p.dashboard ? (ir.dashboards.find((d) => d.id === p.dashboard)?.name ?? p.dashboard) : (ir.worksheets.find((w) => w.id === p.worksheet)?.name ?? p.worksheet);
  const setPoint = (pointId: string, patch: Partial<StoryPoint>) =>
    apply((s) => ({ ...s, points: s.points.map((p) => (p.id === pointId ? { ...p, ...patch } : p)) }));
  const move = (pointId: string, delta: number) =>
    apply((s) => {
      const points = [...s.points];
      const index = points.findIndex((p) => p.id === pointId);
      const target = Math.max(0, Math.min(points.length - 1, index + delta));
      const [point] = points.splice(index, 1);
      points.splice(target, 0, point);
      return { ...s, points };
    });
  const targets = (
    <>
      {ir.dashboards.map((d) => (
        <option key={d.id} value={`d:${d.id}`}>
          dashboard：{d.name}
        </option>
      ))}
      {ir.worksheets.map((w) => (
        <option key={w.id} value={`s:${w.id}`}>
          工作表：{w.name}
        </option>
      ))}
    </>
  );
  const parseTarget = (value: string): Pick<StoryPoint, "dashboard" | "worksheet"> => {
    const [kind, target] = value.split(":");
    return { dashboard: kind === "d" ? target : null, worksheet: kind === "s" ? target : null };
  };

  return (
    <div className="story-editor" data-story-editor={story.id} data-saving={history.pending > 0 ? "true" : "false"}>
      <div className="editor-toolbar" role="toolbar" aria-label="Story 編輯">
        <button type="button" onClick={history.undo} disabled={!history.canUndo}>
          ↶ 復原
        </button>
        <button type="button" onClick={history.redo} disabled={!history.canRedo}>
          ↷ 重做
        </button>
        <span className="sep" />
        <label className="inline">
          Story
          <input
            key={`${story.id}:${story.name}`}
            aria-label="Story 名稱"
            defaultValue={story.name}
            onBlur={(e) => {
              const name = e.target.value.trim();
              if (name && name !== story.name) apply((s) => ({ ...s, name }));
              else e.target.value = story.name;
            }}
          />
        </label>
        <label className="inline">
          <input type="checkbox" checked={story.title !== null} onChange={(e) => apply((s) => ({ ...s, title: e.target.checked ? s.name : null }))} /> 標題
          <input
            key={`title:${story.title}`}
            aria-label="Story 標題"
            disabled={story.title === null}
            defaultValue={story.title ?? ""}
            onBlur={(e) => {
              if (story.title !== null && e.target.value !== story.title) {
                const title = e.target.value;
                apply((s) => ({ ...s, title }));
              }
            }}
          />
        </label>
        <label className="inline">
          導覽
          <select aria-label="導覽的樣式" value={story.navigation} onChange={(e) => apply((s) => ({ ...s, navigation: e.target.value as Story["navigation"] }))}>
            <option value="caption">說明文字</option>
            <option value="number">序號</option>
            <option value="dots">圓點</option>
          </select>
        </label>
        <label className="inline">
          <input type="checkbox" checked={story.arrows} onChange={(e) => apply((s) => ({ ...s, arrows: e.target.checked }))} /> 箭頭
        </label>
        <label className="inline">
          大小
          <input
            type="number"
            min={200}
            aria-label="Story 的寬度"
            key={`w${story.size.width}`}
            defaultValue={story.size.width}
            onBlur={(e) => {
              const width = Math.max(200, Number(e.target.value));
              if (width !== story.size.width) apply((s) => ({ ...s, size: { ...s.size, width } }));
            }}
          />
          ×
          <input
            type="number"
            min={200}
            aria-label="Story 的高度"
            key={`h${story.size.height}`}
            defaultValue={story.size.height}
            onBlur={(e) => {
              const height = Math.max(200, Number(e.target.value));
              if (height !== story.size.height) apply((s) => ({ ...s, size: { ...s.size, height } }));
            }}
          />
        </label>
        <span className="sep" />
        <button
          type="button"
          onClick={() =>
            void editApi
              .addStory(workbook)
              .then((created) => {
                onChanged();
                onOpen(created.id);
              })
              .catch((e: Error) => notify(e.message, "error"))
          }
        >
          新增 Story
        </button>
        <button
          type="button"
          className="danger"
          onClick={() => {
            if (!window.confirm(`刪除 Story「${story.name}」？`)) return;
            void editApi
              .removeStory(workbook, story.id)
              .then(() => {
                onChanged();
                onClose(null);
              })
              .catch((e: Error) => notify(e.message, "error"));
          }}
        >
          刪除
        </button>
        <span className="spacer" />
        {history.pending > 0 && <span className="hint">儲存中…</span>}
        <button type="button" className="primary" onClick={() => onClose(story.id)}>
          完成
        </button>
      </div>
      <aside className="story-point-list" aria-label="故事點">
        <div className="pane-section">故事點</div>
        <ol>
          {story.points.map((p, i) => (
            <li key={p.id} className={current?.id === p.id ? "selected" : ""} data-point={p.id} onClick={() => setSelected(p.id)}>
              <div className="row">
                <span className="point-number">{i + 1}</span>
                <input
                  aria-label={`故事點 ${i + 1} 的說明`}
                  key={`${p.id}:${p.caption}`}
                  defaultValue={p.caption}
                  placeholder="說明文字"
                  onBlur={(e) => e.target.value !== p.caption && setPoint(p.id, { caption: e.target.value })}
                />
              </div>
              <select aria-label={`故事點 ${i + 1} 顯示的內容`} value={targetOf(p)} onChange={(e) => setPoint(p.id, { ...parseTarget(e.target.value), actions: [], filters: [], selections: [], parameters: {}, texts: {} })}>
                {targets}
              </select>
              <div className="row point-actions">
                {(p.actions.length > 0 ||
                  p.filters.length > 0 ||
                  p.selections.length > 0 ||
                  Object.keys(p.parameters ?? {}).length > 0 ||
                  Object.keys(p.texts ?? {}).length > 0) && (
                  <button
                    type="button"
                    className="link"
                    title="清除記下的篩選、選取、參數與文字"
                    onClick={() => setPoint(p.id, { actions: [], filters: [], selections: [], parameters: {}, texts: {} })}
                  >
                    清除狀態
                  </button>
                )}
                <button type="button" className="link" aria-label="上移" onClick={() => move(p.id, -1)}>
                  ▲
                </button>
                <button type="button" className="link" aria-label="下移" onClick={() => move(p.id, 1)}>
                  ▼
                </button>
                <button
                  type="button"
                  className="link"
                  aria-label="刪除故事點"
                  disabled={story.points.length <= 1}
                  onClick={() => apply((s) => ({ ...s, points: s.points.filter((x) => x.id !== p.id), active: s.active === p.id ? null : s.active }))}
                >
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ol>
        <label className="inline">
          新增
          <select
            aria-label="新增故事點"
            value=""
            onChange={(e) => {
              if (!e.target.value) return;
              const target = parseTarget(e.target.value);
              apply((s) => {
                const point: StoryPoint = { id: nextPointId(s), caption: "", ...target, actions: [], filters: [], selections: [] };
                setSelected(point.id);
                return { ...s, points: [...s.points, point] };
              });
            }}
          >
            <option value="">（選擇 dashboard 或工作表）</option>
            {targets}
          </select>
        </label>
        <button
          type="button"
          onClick={() =>
            current &&
            apply((s) => {
              const point: StoryPoint = { ...current, id: nextPointId(s), caption: current.caption ? `${current.caption} (2)` : "" };
              setSelected(point.id);
              const index = s.points.findIndex((p) => p.id === current.id);
              const points = [...s.points];
              points.splice(index + 1, 0, point);
              return { ...s, points };
            })
          }
        >
          複製故事點
        </button>
        {current && <p className="hint">顯示：{nameOf(current)}</p>}
      </aside>
      <div className="story-preview">
        {current && (
          <StoryView
            key={`${current.id}:${history.revision}`}
            workbook={workbook}
            ir={ir}
            story={{ ...story, active: current.id }}
            base={{ sheetFilters: {}, parameters: {}, actions: {}, selections: {} }}
            cases={[]}
            onNavigate={() => undefined}
          />
        )}
      </div>
      {notice && (
        <div className={`toast${notice.kind === "error" ? " error" : ""}`} role="status">
          {notice.text}
        </div>
      )}
    </div>
  );
}
