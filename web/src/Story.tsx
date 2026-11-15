import { useEffect, useMemo, useState } from "react";
import { DashboardView } from "./Dashboard";
import { fieldKey, keepMarks, setDrill, stateFor, tooltipStateFor, type ViewState } from "./state";
import type { CaseInfo, Dashboard, FilterValue, IR, Story, StoryPoint, Zone } from "./types";
import { WorksheetView } from "./WorksheetView";

/** 故事點的狀態：記下的篩選取代同一欄位的篩選（套在篩選組合之上），篩選動作與選取以記下的標記為準。 */
export function pointState(point: StoryPoint, base: ViewState): ViewState {
  const sheetFilters: Record<string, Record<string, FilterValue>> = {};
  for (const [sheet, filters] of Object.entries(base.sheetFilters)) sheetFilters[sheet] = { ...filters };
  for (const f of point.filters) {
    sheetFilters[f.worksheet] = { ...(sheetFilters[f.worksheet] ?? {}), [fieldKey(f.field)]: { mode: f.mode, values: f.values } };
  }
  return {
    ...base,
    sheetFilters,
    // 故事點記下的參數值取代目前的值
    parameters: { ...base.parameters, ...(point.parameters ?? {}) },
    actions: Object.fromEntries(point.actions.map((a) => [a.action, a.marks])),
    selections: Object.fromEntries(point.selections.map((s) => [s.worksheet, s.marks])),
  };
}

/** 故事點改過的文字物件：換掉 dashboard 上那些區塊的內容。 */
export function pointDashboard(point: StoryPoint, dashboard: Dashboard): Dashboard {
  const texts = point.texts ?? {};
  if (Object.keys(texts).length === 0) return dashboard;
  const replace = (zones: Zone[]): Zone[] =>
    zones.map((z) =>
      texts[z.id]
        ? { ...z, content: texts[z.id], text: texts[z.id].map((r) => r.text ?? "").join("") }
        : z.children.length > 0
          ? { ...z, children: replace(z.children) }
          : z,
    );
  return { ...dashboard, zones: replace(dashboard.zones) };
}

/**
 * 把目前的檢視狀態記成故事點的狀態（Tableau 故事點的「更新」）：類別篩選的選取、參數值、篩選動作的選取與
 * 醒目顯示的標記。範圍類的篩選（滑桿、日期範圍）記不下來，回傳在 skipped 中（欄位鍵）。
 */
export function capturePoint(
  ir: IR,
  view: ViewState,
): { state: Pick<StoryPoint, "filters" | "actions" | "selections" | "parameters">; skipped: string[] } {
  const filters: StoryPoint["filters"] = [];
  const skipped: string[] = [];
  for (const [sheetId, values] of Object.entries(view.sheetFilters)) {
    const sheet = ir.worksheets.find((w) => w.id === sheetId);
    for (const [key, value] of Object.entries(values)) {
      const own = sheet?.filters.find((f) => "field" in f && f.field && fieldKey(f.field) === key);
      if (!own || !("field" in own) || !own.field) continue;
      if (value.mode === "include" || value.mode === "exclude") {
        filters.push({ worksheet: sheetId, field: own.field, mode: value.mode, values: value.values ?? [] });
      } else if (value.mode === "all") {
        filters.push({ worksheet: sheetId, field: own.field, mode: "exclude", values: [] });
      } else skipped.push(key);
    }
  }
  const actions = Object.entries(view.actions)
    .filter(([, marks]) => marks.length > 0)
    .map(([action, marks]) => ({ action, marks }));
  const selections = Object.entries(view.selections)
    .filter(([, marks]) => marks.length > 0)
    .map(([worksheet, marks]) => ({ worksheet, marks }));
  return { state: { filters, actions, selections, parameters: { ...view.parameters } }, skipped };
}

function initialIndex(story: Story): number {
  return Math.max(0, story.points.findIndex((p) => p.id === story.active));
}

interface Props {
  workbook: string;
  ir: IR;
  story: Story;
  /** 篩選組合的狀態（網址參數）；每個故事點以它為基礎 */
  base: ViewState;
  cases: CaseInfo[];
  onNavigate: (target: { dashboard?: string | null; worksheet?: string | null; story?: string | null }) => void;
  /** Story 編輯器：目前故事點的檢視狀態（操作之後的） */
  onView?: (point: StoryPoint, view: ViewState) => void;
}

/**
 * Story：上方為標題與故事點導覽（說明文字、序號或圓點，兩側可有上一個／下一個箭頭），下方顯示目前故事點的
 * dashboard 或工作表。在故事點上操作（篩選、選取）只影響目前的畫面，切換故事點時回到記下的狀態。
 */
export function StoryView({ workbook, ir, story, base, cases, onNavigate, onView }: Props) {
  const [index, setIndex] = useState(() => initialIndex(story));
  useEffect(() => setIndex(initialIndex(story)), [story]);
  const point = story.points[Math.min(index, story.points.length - 1)];
  const captured = useMemo(() => pointState(point, base), [point, base]);
  // 在故事點上的操作：只對同一個故事點（與同一個篩選組合）有效，切換後立刻回到記下的狀態
  const [changed, setChanged] = useState<{ point: StoryPoint; base: ViewState; view: ViewState } | null>(null);
  const view = changed && changed.point === point && changed.base === base ? changed.view : captured;
  const setView = (next: ViewState) => setChanged({ point, base, view: next });
  useEffect(() => onView?.(point, view), [onView, point, view]);

  const found = point.dashboard ? ir.dashboards.find((d) => d.id === point.dashboard) : undefined;
  const dashboard = useMemo(() => (found ? pointDashboard(point, found) : undefined), [found, point]);
  const worksheet = point.worksheet ? ir.worksheets.find((w) => w.id === point.worksheet) : undefined;
  const last = story.points.length - 1;
  const go = (next: number) => setIndex(Math.max(0, Math.min(last, next)));
  const label = (p: StoryPoint, i: number) =>
    story.navigation === "caption" && p.caption ? p.caption : story.navigation === "dots" ? "" : String(i + 1);

  return (
    <div className="story" data-story={story.id} style={{ minWidth: story.size.width }}>
      {story.title && <div className="story-title">{story.title}</div>}
      <nav className="story-nav" data-navigation={story.navigation} aria-label="故事點">
        {story.arrows && (
          <button type="button" className="story-arrow" aria-label="上一個故事點" disabled={index === 0} onClick={() => go(index - 1)}>
            ‹
          </button>
        )}
        <ol className="story-points">
          {story.points.map((p, i) => (
            <li key={p.id}>
              <button
                type="button"
                className={i === index ? "active" : ""}
                aria-current={i === index ? "step" : undefined}
                aria-label={p.caption || `故事點 ${i + 1}`}
                data-point={p.id}
                onClick={() => go(i)}
              >
                {label(p, i)}
              </button>
            </li>
          ))}
        </ol>
        {story.arrows && (
          <button type="button" className="story-arrow" aria-label="下一個故事點" disabled={index === last} onClick={() => go(index + 1)}>
            ›
          </button>
        )}
      </nav>
      <div className="story-point" data-point={point.id}>
        {dashboard && (
          <DashboardView
            workbook={workbook}
            ir={ir}
            dashboard={dashboard}
            view={view}
            onView={setView}
            onNavigate={onNavigate}
            cases={cases}
          />
        )}
        {worksheet && (
          <div className="single-sheet">
            <WorksheetView
              workbook={workbook}
              worksheet={worksheet.id}
              state={stateFor(ir, worksheet.id, view)}
              selected={view.selections[worksheet.id]}
              onDrill={(key, delta) => setView(setDrill(view, worksheet.id, key, delta))}
              tooltipState={(target, mark) => tooltipStateFor(ir, worksheet.id, target, view, mark)}
              onKeep={(mode, marks) => setView(keepMarks(view, worksheet.id, mode, marks))}
            />
          </div>
        )}
        {!dashboard && !worksheet && <div className="error">找不到故事點顯示的 dashboard 或工作表</div>}
      </div>
    </div>
  );
}
