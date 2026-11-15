// 桌面版的 Story 編輯器：故事點的新增、排序、說明文字與顯示的 dashboard 或工作表，Story 的標題、大小與導覽。

import type React from "react";
import { useCallback, useRef, useState } from "react";
import { Modal } from "../Server";
import type { ViewState } from "../state";
import type { IR, Story, StoryFormat, StoryNote, StoryPoint } from "../types";
import { capturePoint, StoryView } from "../Story";
import { editApi } from "./api";
import { useEditHistory, useNotice } from "./history";
import { type RichRun, RichTextEditor } from "./RichText";

const EMPTY_VIEW: ViewState = { sheetFilters: {}, parameters: {}, actions: {}, selections: {} };

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
  const [noteEditing, setNoteEditing] = useState<number | null>(null);
  const [formatOpen, setFormatOpen] = useState(false);
  // 預覽中目前的檢視狀態（在故事點上操作之後的）：「更新」把它記到故事點
  const live = useRef<{ point: string; view: ViewState } | null>(null);
  const onView = useCallback((point: StoryPoint, view: ViewState) => {
    live.current = { point: point.id, view };
  }, []);
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
  /** 預覽中目前故事點的狀態；範圍類的篩選記不下來時提醒。 */
  const capture = () => {
    if (!current || live.current?.point !== current.id) return null;
    const { state, skipped } = capturePoint(ir, live.current.view);
    if (skipped.length > 0) notify(`篩選（${skipped.join("、")}）無法記到故事點，其餘的已記下`, "error");
    return state;
  };
  // 故事點上的文字物件（Tableau 的「拖曳以新增文字」）：新增、編輯文字、拖曳移動與刪除
  const setNotes = (pointId: string, change: (notes: StoryNote[]) => StoryNote[]) =>
    apply((s) => ({ ...s, points: s.points.map((p) => (p.id === pointId ? { ...p, notes: change(p.notes ?? []) } : p)) }));
  const onNoteDown = (index: number, event: React.MouseEvent<HTMLDivElement>) => {
    if (!current || event.button !== 0) return;
    event.preventDefault();
    const pointId = current.id;
    const node = event.currentTarget;
    const startX = event.clientX;
    const startY = event.clientY;
    const drag = (move: MouseEvent) => {
      node.style.transform = `translate(${move.clientX - startX}px, ${move.clientY - startY}px)`;
    };
    const drop = (up: MouseEvent) => {
      document.removeEventListener("mousemove", drag);
      document.removeEventListener("mouseup", drop);
      node.style.transform = "";
      const dx = Math.round(up.clientX - startX);
      const dy = Math.round(up.clientY - startY);
      if (Math.abs(dx) + Math.abs(dy) <= 2) {
        setNoteEditing(index); // 沒有移動：編輯文字
        return;
      }
      setNotes(pointId, (notes) => notes.map((n, i) => (i === index ? { ...n, x: Math.max(0, n.x + dx), y: Math.max(0, n.y + dy) } : n)));
    };
    document.addEventListener("mousemove", drag);
    document.addEventListener("mouseup", drop);
  };
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
        <button type="button" title="Story 的底色、標題、導覽與文字物件的樣式（Tableau 的「Story＞格式」）" onClick={() => setFormatOpen(true)}>
          格式…
        </button>
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
          title="把預覽中目前的篩選、參數與選取記到這個故事點（Tableau 故事點的「更新」）"
          onClick={() => {
            const captured = capture();
            if (current && captured) setPoint(current.id, captured);
          }}
        >
          更新故事點
        </button>
        <button
          type="button"
          title="以預覽中目前的狀態新增一個故事點（Tableau 的「另存為新的故事點」）"
          onClick={() => {
            const captured = capture();
            if (!current || !captured) return;
            apply((s) => {
              const point: StoryPoint = { ...current, ...captured, id: nextPointId(s), caption: current.caption ? `${current.caption} (2)` : "" };
              setSelected(point.id);
              const index = s.points.findIndex((p) => p.id === current.id);
              const points = [...s.points];
              points.splice(index + 1, 0, point);
              return { ...s, points };
            });
          }}
        >
          另存為新的故事點
        </button>
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
        <button
          type="button"
          title="在這個故事點上加一個文字物件（說明文字）；在預覽上拖曳可以移動，按一下編輯"
          onClick={() => {
            if (!current) return;
            const count = (current.notes ?? []).length;
            setNotes(current.id, (notes) => [...notes, { text: [{ text: "說明文字" }], x: 20 + count * 16, y: 20 + count * 16, w: 220, h: 60 }]);
            setNoteEditing(count);
          }}
        >
          ＋ 文字物件
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
            base={EMPTY_VIEW}
            cases={[]}
            onNavigate={() => undefined}
            onView={onView}
            onNoteDown={onNoteDown}
          />
        )}
      </div>
      {current && noteEditing !== null && (current.notes ?? [])[noteEditing] && (
        <NoteDialog
          key={`${current.id}:${noteEditing}`}
          note={(current.notes ?? [])[noteEditing]}
          onClose={() => setNoteEditing(null)}
          onSave={(note) => {
            setNotes(current.id, (notes) => notes.map((n, i) => (i === noteEditing ? note : n)));
            setNoteEditing(null);
          }}
          onRemove={() => {
            setNotes(current.id, (notes) => notes.filter((_, i) => i !== noteEditing));
            setNoteEditing(null);
          }}
        />
      )}
      {formatOpen && (
        <StoryFormatDialog
          format={story.format ?? null}
          onClose={() => setFormatOpen(false)}
          onSave={(format) => {
            apply((s) => ({ ...s, format }));
            setFormatOpen(false);
          }}
        />
      )}
      {notice && (
        <div className={`toast${notice.kind === "error" ? " error" : ""}`} role="status">
          {notice.text}
        </div>
      )}
    </div>
  );
}

/** 故事點上的文字物件：文字（可以有格式）、位置與大小。 */
function NoteDialog({ note, onClose, onSave, onRemove }: { note: StoryNote; onClose: () => void; onSave: (note: StoryNote) => void; onRemove: () => void }) {
  const [runs, setRuns] = useState<RichRun[]>(() => note.text.map((r) => ({ text: r.text ?? "", style: r.style ?? null })) as RichRun[]);
  const [box, setBox] = useState({ x: note.x, y: note.y, w: note.w, h: note.h });
  const field = (key: "x" | "y" | "w" | "h", label: string, min: number) => (
    <label className="inline">
      {label}
      <input
        type="number"
        min={min}
        aria-label={`文字物件的${label}`}
        className="narrow-number"
        value={box[key]}
        onChange={(e) => setBox({ ...box, [key]: Math.max(min, Math.round(Number(e.target.value)) || min) })}
      />
    </label>
  );
  return (
    <Modal title="文字物件" onClose={onClose}>
      <div className="story-note-dialog">
        <RichTextEditor label="文字物件的文字" value={runs} onChange={setRuns} rows={4} />
        <div className="row">
          {field("x", "左", 0)}
          {field("y", "上", 0)}
          {field("w", "寬", 20)}
          {field("h", "高", 20)}
        </div>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={() => onSave({ ...box, text: runs.filter((r) => r.text !== undefined && r.text !== "") as StoryNote["text"] })}>
            確定
          </button>
          <button type="button" className="danger" onClick={onRemove}>
            移除
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}

const FORMAT_COLORS: [keyof StoryFormat, string, string][] = [
  ["background", "Story 的底色", "#ffffff"],
  ["title_color", "標題的顏色", "#333333"],
  ["navigator_background", "導覽的底色", "#f4f4f4"],
  ["navigator_color", "導覽文字的顏色", "#333333"],
  ["note_background", "文字物件的底色", "#fffbe6"],
  ["note_color", "文字物件的文字顏色", "#333333"],
];

/** Story 的格式：底色、標題（顏色、字級、對齊）、導覽與文字物件的顏色；不勾選的項目用預設。 */
function StoryFormatDialog({ format, onClose, onSave }: { format: StoryFormat | null; onClose: () => void; onSave: (format: StoryFormat | null) => void }) {
  const [value, setValue] = useState<StoryFormat>(format ?? {});
  const save = () => {
    const cleaned = Object.fromEntries(Object.entries(value).filter(([, v]) => v !== null && v !== undefined && v !== ""));
    onSave(Object.keys(cleaned).length > 0 ? (cleaned as StoryFormat) : null);
  };
  return (
    <Modal title="Story 的格式" onClose={onClose}>
      <div className="story-format-dialog">
        {FORMAT_COLORS.map(([key, label, fallback]) => (
          <div className="row" key={key}>
            <label className="inline">
              <input
                type="checkbox"
                aria-label={`設定${label}`}
                checked={Boolean(value[key])}
                onChange={(e) => setValue({ ...value, [key]: e.target.checked ? fallback : null })}
              />{" "}
              {label}
            </label>
            {Boolean(value[key]) && <input type="color" aria-label={label} value={String(value[key])} onChange={(e) => setValue({ ...value, [key]: e.target.value })} />}
          </div>
        ))}
        <div className="row">
          <label className="inline">
            標題的字級
            <input
              type="number"
              min={6}
              max={72}
              aria-label="標題的字級"
              className="narrow-number"
              placeholder="預設"
              value={value.title_size ?? ""}
              onChange={(e) => setValue({ ...value, title_size: e.target.value ? Math.max(6, Math.min(72, Math.round(Number(e.target.value)))) : null })}
            />
          </label>
          <label className="inline">
            標題的對齊
            <select aria-label="標題的對齊" value={value.title_align ?? ""} onChange={(e) => setValue({ ...value, title_align: (e.target.value || null) as StoryFormat["title_align"] })}>
              <option value="">預設</option>
              <option value="left">靠左</option>
              <option value="center">置中</option>
              <option value="right">靠右</option>
            </select>
          </label>
        </div>
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={save}>
            確定
          </button>
          <button type="button" onClick={() => onSave(null)}>
            清除格式
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
