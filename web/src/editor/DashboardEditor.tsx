// 桌面版的 dashboard 編輯器（Tableau Desktop 的儀表板）：工作表與物件的加入（並排或浮動）、版面配置、
// 區塊的格式與大小、dashboard 的大小與背景、動作。畫面上直接以檢視器繪製，選取的區塊加上外框。

import type React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { caseState, DashboardView } from "../Dashboard";
import type { Button, Dashboard, Device, DeviceLayout, IR, Zone, ZoneStyle } from "../types";
import { ActionsDialog } from "./ActionsDialog";
import { Modal } from "../Server";
import { type RichRun, RichTextEditor, type RichToken, plainText } from "./RichText";
import { editApi, type Outline } from "./api";
import {
  DEVICE_NAMES,
  findZone,
  insertFloating,
  insertTiled,
  isFloating,
  layoutDashboard,
  mergeLayout,
  moveZone,
  newDeviceLayout,
  type NewZone,
  placeZone,
  relayout,
  relayoutParent,
  removeZone,
  targetContainer,
  TILED_STYLE,
  updateZone,
  walkZones,
  zoneLabel,
} from "./dashboardModel";
import { useEditHistory, useNotice } from "./history";
import { CONTROL_OPTIONS } from "./Shelves";

type Base = Omit<NewZone, "kind">;
const EMPTY: Base = {
  worksheet: null,
  field: null,
  parameter: null,
  control: null,
  legend: null,
  text: null,
  image: null,
  show_title: true,
  hidden: false,
  fixed: null,
  style: null,
  fit: null,
  layout: null,
  content: [],
  button: null,
  url: null,
};

function makeZone(kind: Zone["kind"], props: Partial<Base>, floating: boolean): NewZone {
  return { ...EMPTY, style: floating ? null : TILED_STYLE, ...props, kind } as NewZone;
}

const FILTER_CONTROLS: Record<string, [string, string][]> = {
  categorical: [
    ["multi_list", "多選清單"],
    ["multi_dropdown", "多選下拉式清單"],
    ["single_list", "單選清單"],
    ["single_dropdown", "單選下拉式清單"],
  ],
  range: [
    ["range_slider", "範圍滑桿"],
    ["date_range", "日期範圍"],
  ],
  relative_date: [["relative_date", "相對日期"]],
  top_n: [["condition", "條件"]],
  condition: [["condition", "條件"]],
};

const PARAMETER_CONTROLS: Record<string, [string, string][]> = {
  list: [
    ["single_dropdown", "下拉式清單"],
    ["single_list", "單選清單"],
  ],
  range: [
    ["single_slider", "滑桿"],
    ["type_in", "輸入方塊"],
  ],
  all: [["type_in", "輸入方塊"]],
};

const DEFAULT_FLOAT = { w: 400, h: 300 };

interface Drag {
  id: string;
  mode: "move" | "resize";
  startX: number;
  startY: number;
  dx: number;
  dy: number;
}

function StyleEditor({ style, onChange }: { style: ZoneStyle | null | undefined; onChange: (style: ZoneStyle | null) => void }) {
  const current: ZoneStyle = style ?? { ...TILED_STYLE, margin: [0, 0, 0, 0] };
  const set = (patch: Partial<ZoneStyle>) => onChange({ ...current, ...patch });
  return (
    <fieldset className="prop-group">
      <legend>格式</legend>
      <label className="inline">
        <input type="checkbox" checked={Boolean(current.background)} onChange={(e) => set({ background: e.target.checked ? "#ffffff" : null })} /> 背景
        <input
          type="color"
          aria-label="區塊的背景"
          disabled={!current.background}
          value={(current.background ?? "#ffffff").slice(0, 7)}
          onChange={(e) => set({ background: e.target.value })}
        />
      </label>
      <label className="inline">
        框線
        <select aria-label="框線" value={current.border} onChange={(e) => set({ border: e.target.value as ZoneStyle["border"], border_width: e.target.value === "none" ? 0 : Math.max(1, current.border_width) })}>
          <option value="none">無</option>
          <option value="solid">實線</option>
          <option value="dashed">虛線</option>
          <option value="dotted">點線</option>
        </select>
        <input type="number" min={0} max={20} aria-label="框線寬度" value={current.border_width} onChange={(e) => set({ border_width: Math.max(0, Number(e.target.value)) })} />
        <input type="color" aria-label="框線顏色" value={(current.border_color ?? "#000000").slice(0, 7)} onChange={(e) => set({ border_color: e.target.value })} />
      </label>
      <label className="inline">
        外距
        <input type="number" min={0} max={100} aria-label="外距" value={current.margin[0]} onChange={(e) => { const n = Math.max(0, Number(e.target.value)); set({ margin: [n, n, n, n] }); }} />
        內距
        <input type="number" min={0} max={100} aria-label="內距" value={current.padding[0]} onChange={(e) => { const n = Math.max(0, Number(e.target.value)); set({ padding: [n, n, n, n] }); }} />
      </label>
    </fieldset>
  );
}

export function DashboardEditor({
  workbook,
  dashboard: id,
  ir,
  onChanged,
  onOpen,
  onClose,
}: {
  workbook: string;
  dashboard: string;
  ir: IR;
  onChanged: () => void;
  onOpen: (dashboard: string) => void;
  onClose: (dashboard: string | null) => void;
}) {
  const [notice, notify] = useNotice();
  const [outline, setOutline] = useState<Outline | null>(null);
  const history = useEditHistory<Dashboard>(
    `${workbook}/${id}`,
    () => editApi.dashboard(workbook, id),
    (d) => editApi.saveDashboard(workbook, d),
    notify,
    (saved, previous) => {
      if (saved.name !== previous.name) onChanged();
    },
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [floating, setFloating] = useState(false);
  const [controlSheet, setControlSheet] = useState<string>("");
  const [actionsOpen, setActionsOpen] = useState(false);
  // 編輯格式文字的文字物件（區塊 id）
  const [textZone, setTextZone] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [view, setView] = useState(() => caseState(ir, undefined));
  const [drag, setDrag] = useState<Drag | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(900);

  const reloadOutline = () =>
    editApi
      .outline(workbook)
      .then(setOutline)
      .catch((e: Error) => notify(e.message, "error"));
  useEffect(() => {
    void reloadOutline();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workbook]);

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setCanvasWidth(element.clientWidth));
    observer.observe(element);
    setCanvasWidth(element.clientWidth);
    return () => observer.disconnect();
  }, [history.current === null]);

  // 裝置專屬版面：選了裝置版面時，以它的區塊與尺寸當作 dashboard 編輯，變更寫回那個版面
  const [device, setDevice] = useState<Device | "default">("default");
  const full = history.current;
  const layout = full && device !== "default" ? (full.layouts ?? []).find((l) => l.device === device) ?? null : null;
  const dashboard = full ? (layout ? layoutDashboard(full, layout) : full) : null;
  const applyLayout = (change: (d: Dashboard) => Dashboard) =>
    history.apply((d) => {
      const current = device === "default" ? null : (d.layouts ?? []).find((l) => l.device === device);
      if (!current || device === "default") return change(d);
      return mergeLayout(d, device, change(layoutDashboard(d, current)));
    });
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const w of outline?.worksheets ?? []) map.set(w.id, w.name);
    for (const p of outline?.parameters ?? []) map.set(`param:${p.id}`, p.caption);
    return map;
  }, [outline]);

  // 浮動區塊的拖曳：移動與調整大小（以畫面的縮放換算成 dashboard 的像素）
  const scaleRef = useRef(1);
  const applyRef = useRef(applyLayout);
  applyRef.current = applyLayout;
  const dragRef = useRef<Drag | null>(null);
  dragRef.current = drag;
  useEffect(() => {
    if (!drag) return;
    const onMove = (e: MouseEvent) => {
      const current = dragRef.current;
      if (!current) return;
      const next = { ...current, dx: (e.clientX - current.startX) / scaleRef.current, dy: (e.clientY - current.startY) / scaleRef.current };
      dragRef.current = next;
      setDrag(next);
    };
    const onUp = () => {
      const current = dragRef.current;
      dragRef.current = null;
      setDrag(null);
      if (!current || (Math.abs(current.dx) <= 1 && Math.abs(current.dy) <= 1)) return;
      applyRef.current((d) =>
        updateZone(d, current.id, (z) => {
          const box =
            current.mode === "move"
              ? { x: Math.max(0, Math.min(d.size.width - z.w, z.x + current.dx)), y: Math.max(0, Math.min(d.size.height - z.h, z.y + current.dy)), w: z.w, h: z.h }
              : { x: z.x, y: z.y, w: Math.max(20, Math.min(d.size.width - z.x, z.w + current.dx)), h: Math.max(20, Math.min(d.size.height - z.y, z.h + current.dy)) };
          return placeZone(z, box);
        }),
      );
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [drag !== null]);

  if (history.error) return <main className="error">無法編輯：{history.error}</main>;
  if (!dashboard || !outline) return <main>載入中…</main>;

  const scale = zoom === "fit" ? Math.min(1, Math.max(0.2, (canvasWidth - 24) / dashboard.size.width)) : zoom;
  scaleRef.current = scale;
  const apply = applyLayout;
  const selectedZone = selected ? findZone(dashboard, selected) : null;

  const add = (zone: NewZone, size = DEFAULT_FLOAT, at?: { x: number; y: number }, near?: string | null) =>
    apply((d) => {
      if (floating || at) {
        const position = at ?? { x: 24 + (d.zones.length - 1) * 16, y: 24 + (d.zones.length - 1) * 16 };
        const [next, created] = insertFloating(d, { ...zone, style: null }, { ...position, ...size });
        setSelected(created);
        return next;
      }
      const container = targetContainer(d, near ?? selected);
      if (!container) throw new Error("dashboard 上沒有可以放入的容器");
      const after = (near ?? selected) && (near ?? selected) !== container.id ? (near ?? selected) : null;
      const [next, created] = insertTiled(d, container.id, zone, after);
      setSelected(created);
      return next;
    });

  const addSheet = (worksheet: string, at?: { x: number; y: number }, near?: string | null) =>
    add(makeZone("worksheet", { worksheet }, floating || Boolean(at)), DEFAULT_FLOAT, at, near);

  const change = (zoneId: string, patch: (zone: Zone) => Zone) => apply((d) => updateZone(d, zoneId, patch));

  const overlays: { zone: Zone; depth: number; floating: boolean }[] = [];
  walkZones(dashboard.zones, (zone, _parent, depth) => {
    if (!zone.hidden) overlays.push({ zone, depth, floating: isFloating(dashboard, zone.id) });
  });

  const dropSheet = (e: React.DragEvent, zone: Zone | null) => {
    const sheet = e.dataTransfer.getData("application/x-tmig-sheet");
    if (!sheet) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = (e.currentTarget as HTMLElement).closest(".dash-stage")?.getBoundingClientRect();
    if (floating && rect) {
      addSheet(sheet, { x: (e.clientX - rect.left) / scale, y: (e.clientY - rect.top) / scale });
      return;
    }
    addSheet(sheet, undefined, zone?.id ?? null);
  };

  const sheetsOnBoard = new Set<string>();
  walkZones(dashboard.zones, (z) => {
    if (z.kind === "worksheet" && z.worksheet) sheetsOnBoard.add(z.worksheet);
  });
  const controlTarget = outline.worksheets.find((w) => w.id === (controlSheet || [...sheetsOnBoard][0]));
  /** 「當作篩選器使用」自動產生的篩選動作（這個 dashboard 上、來源只有這張工作表）。 */
  const generatedFilter = (sheet: string) =>
    (outline?.actions ?? []).find(
      (a) => a.kind === "filter" && a.dashboard === dashboard!.id && a.sources.length === 1 && a.sources[0] === sheet && a.caption.endsWith(GENERATED),
    );
  /** dashboard 上其他使用同一個資料源的工作表。 */
  const filterTargets = (sheet: string): string[] => {
    const source = outline?.worksheets.find((w) => w.id === sheet)?.datasource;
    const found: string[] = [];
    walkZones(dashboard!.zones, (z) => {
      const target = z.kind === "worksheet" ? z.worksheet : null;
      if (target && target !== sheet && !found.includes(target) && outline?.worksheets.find((w) => w.id === target)?.datasource === source) {
        found.push(target);
      }
    });
    return found;
  };
  const useAsFilter = async (sheet: string, on: boolean) => {
    try {
      const existing = generatedFilter(sheet);
      if (on && !existing) {
        const name = outline?.worksheets.find((w) => w.id === sheet)?.name ?? sheet;
        await editApi.saveAction(workbook, {
          caption: `篩選：${name}${GENERATED}`,
          kind: "filter",
          activation: "select",
          dashboard: dashboard!.id,
          sources: [sheet],
          targets: filterTargets(sheet),
          clearing: "show_all",
          fields: [],
        });
      } else if (!on && existing) {
        await editApi.removeAction(workbook, existing.id);
      }
      await reloadOutline();
      onChanged();
    } catch (e) {
      notify((e as Error).message, "error");
    }
  };

  // 文字物件中可以插入的參數
  const parameterTokens: RichToken[] = (outline?.parameters ?? []).map((p) => ({ label: `參數.${p.caption}`, run: { parameter: p.id } }));

  const zoneProps = selectedZone ? renderZoneProps(selectedZone.zone, selectedZone.parent) : null;

  /** 顯示／隱藏按鈕可以切換的區塊：這個 dashboard 上按鈕以外的區塊（最外層的容器除外）。 */
  function toggleTargets(self: string): { id: string; label: string }[] {
    const found: { id: string; label: string }[] = [];
    const root = dashboard!.zones[0]?.id;
    walkZones(dashboard!.zones, (z) => {
      if (z.id !== self && z.id !== root) found.push({ id: z.id, label: zoneLabel(z, names) });
    });
    return found;
  }

  function renderZoneProps(zone: Zone, parent: Zone | null) {
    const floatingZone = isFloating(dashboard!, zone.id);
    const root = dashboard!.zones[0]?.id === zone.id;
    const along = parent?.layout === "horizontal" ? "寬度" : parent?.layout === "vertical" ? "高度" : null;
    const set = (patch: Partial<Zone>) => change(zone.id, (z) => ({ ...z, ...patch }));
    return (
      <div className="zone-props" data-zone-props={zone.id}>
        <div className="prop-title">
          {zoneLabel(zone, names)}
          {parent && (
            <button type="button" className="link" onClick={() => setSelected(parent.id)}>
              選取上層
            </button>
          )}
        </div>
        {zone.kind === "worksheet" && (
          <>
            <label className="inline">
              工作表
              <select aria-label="區塊的工作表" value={zone.worksheet ?? ""} onChange={(e) => set({ worksheet: e.target.value })}>
                {outline!.worksheets.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline">
              顯示大小
              <select aria-label="區塊的顯示大小" value={zone.fit ?? ""} onChange={(e) => set({ fit: (e.target.value || null) as Zone["fit"] })}>
                <option value="">依工作表</option>
                <option value="standard">標準</option>
                <option value="fit_width">符合寬度</option>
                <option value="fit_height">符合高度</option>
                <option value="entire_view">整個檢視</option>
              </select>
            </label>
          </>
        )}
        {(zone.kind === "worksheet" || zone.kind === "filter" || zone.kind === "legend" || zone.kind === "parameter") && (
          <label className="inline">
            <input type="checkbox" checked={zone.show_title !== false} onChange={(e) => set({ show_title: e.target.checked })} /> 顯示標題
          </label>
        )}
        {zone.kind === "worksheet" && (
          <label className="inline" title="在工作表的下方顯示說明（Caption）">
            <input type="checkbox" checked={zone.show_caption === true} onChange={(e) => set({ show_caption: e.target.checked })} /> 顯示說明
          </label>
        )}
        {zone.kind === "worksheet" && zone.worksheet && (
          <label className="inline" title="點選這張工作表的標記時，篩選 dashboard 上其他使用同一個資料源的工作表（自動產生一個篩選動作）">
            <input
              type="checkbox"
              aria-label="當作篩選器使用"
              checked={Boolean(generatedFilter(zone.worksheet))}
              disabled={!generatedFilter(zone.worksheet) && filterTargets(zone.worksheet).length === 0}
              onChange={(e) => void useAsFilter(zone.worksheet!, e.target.checked)}
            />{" "}
            當作篩選器使用
          </label>
        )}
        {zone.kind === "text" && (
          <>
            <textarea
              aria-label="文字"
              rows={4}
              className="formula"
              key={JSON.stringify(zone.content ?? zone.text)}
              defaultValue={zone.content && zone.content.length > 0 ? plainText(zone.content, parameterTokens) : (zone.text ?? "")}
              readOnly={(zone.content ?? []).some((r) => r.parameter || r.style)}
              title={(zone.content ?? []).some((r) => r.parameter || r.style) ? "含格式或參數的值：以「編輯文字…」修改" : undefined}
              onBlur={(e) => {
                if (!e.target.readOnly && e.target.value !== zone.text) set({ text: e.target.value, content: [] });
              }}
            />
            <button type="button" title="粗體、字級、顏色、對齊與參數的值" onClick={() => setTextZone(zone.id)}>
              編輯文字…
            </button>
          </>
        )}
        {zone.kind === "image" && (
          <label className="inline">
            圖片
            <input aria-label="圖片路徑" defaultValue={zone.image ?? ""} list="dash-images" onBlur={(e) => e.target.value && set({ image: e.target.value })} />
          </label>
        )}
        {zone.kind === "web" && (
          <label className="inline" title="留空白時是空白的網頁物件，由網址動作載入（動作的「網址目標」）">
            網址
            <input
              aria-label="網址"
              key={zone.url ?? ""}
              defaultValue={zone.url ?? ""}
              placeholder="空白：由網址動作載入"
              onBlur={(e) => set({ url: e.target.value.trim() || null })}
            />
          </label>
        )}
        {zone.kind === "filter" && (
          <label className="inline">
            控制項
            <select aria-label="篩選控制項" value={zone.control ?? ""} onChange={(e) => set({ control: e.target.value })}>
              {Object.values(FILTER_CONTROLS)
                .flat()
                .filter(([value], i, all) => all.findIndex(([v]) => v === value) === i)
                .map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
        )}
        {zone.kind === "filter" && ["multi_list", "multi_dropdown", "single_list", "single_dropdown"].includes(zone.control ?? "") && (
          <>
            <label className="inline" title="可選值只列出套用工作表上其他篩選之後還有資料的值（預設列出資料庫中所有的值）">
              <input type="checkbox" checked={zone.relevant === true} onChange={(e) => set({ relevant: e.target.checked })} /> 只顯示相關的值
            </label>
            <label className="inline" title="可選值只列出套用工作表的內容篩選之後的值（沒有勾選「只顯示相關的值」時）">
              <input
                type="checkbox"
                checked={zone.options?.context_values === true}
                disabled={zone.relevant === true}
                onChange={(e) => set({ options: { ...(zone.options ?? {}), context_values: e.target.checked } })}
              />{" "}
              內容中的所有值
            </label>
            <fieldset className="control-customize">
              <legend>自訂</legend>
              {CONTROL_OPTIONS.map(([option, label, initial]) => (
                <label className="inline" key={option}>
                  <input
                    type="checkbox"
                    checked={(zone.options?.[option] ?? initial) === true}
                    onChange={(e) => set({ options: { ...(zone.options ?? {}), [option]: e.target.checked } })}
                  />{" "}
                  {label}
                </label>
              ))}
            </fieldset>
          </>
        )}
        {zone.kind === "parameter" && (
          <label className="inline">
            控制項
            <select aria-label="參數控制項" value={zone.control ?? ""} onChange={(e) => set({ control: e.target.value })}>
              {Object.values(PARAMETER_CONTROLS)
                .flat()
                .filter(([value], i, all) => all.findIndex(([v]) => v === value) === i)
                .map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
        )}
        {zone.kind === "legend" && (
          <label className="inline">
            圖例
            <select aria-label="圖例" value={zone.legend ?? "color"} onChange={(e) => set({ legend: e.target.value })}>
              <option value="color">色彩</option>
              <option value="size">大小</option>
              <option value="shape">形狀</option>
            </select>
          </label>
        )}
        {zone.kind === "button" && zone.button && (
          <>
            <label className="inline">
              動作
              <select
                aria-label="按鈕的動作"
                value={zone.button.action}
                onChange={(e) => {
                  const action = e.target.value as Button["action"];
                  const base = { ...zone.button!, target_dashboard: null, target_worksheet: null, target_story: null, zones: [] as string[], export: null as Button["export"] };
                  if (action === "navigate") {
                    const target = outline!.dashboards.find((d) => d.id !== dashboard!.id) ?? outline!.dashboards[0];
                    set({ button: { ...base, action, target_dashboard: target?.id ?? null, target_worksheet: target ? null : (outline!.worksheets[0]?.id ?? null) } });
                  } else if (action === "toggle") {
                    // 顯示／隱藏：預設切換第一個其他的區塊（下面可以改）
                    const first = toggleTargets(zone.id)[0];
                    if (first) set({ button: { ...base, action, zones: [first.id] } });
                  } else {
                    set({ button: { ...base, action, export: "pdf" } });
                  }
                }}
              >
                <option value="navigate">導覽</option>
                <option value="toggle" disabled={toggleTargets(zone.id).length === 0}>
                  顯示／隱藏區塊
                </option>
                <option value="export">匯出</option>
              </select>
            </label>
            {zone.button.action === "export" && (
              <label className="inline">
                格式
                <select
                  aria-label="匯出的格式"
                  value={zone.button.export ?? "pdf"}
                  onChange={(e) => set({ button: { ...zone.button!, export: e.target.value as Button["export"] } })}
                >
                  <option value="pdf">PDF</option>
                  <option value="image">圖片</option>
                  <option value="powerpoint">PowerPoint</option>
                </select>
              </label>
            )}
            {zone.button.action === "toggle" && (
              <div className="check-list" role="group" aria-label="顯示或隱藏的區塊">
                {toggleTargets(zone.id).map((target) => (
                  <label key={target.id}>
                    <input
                      type="checkbox"
                      checked={zone.button!.zones.includes(target.id)}
                      onChange={(e) => {
                        const zones = e.target.checked ? [...zone.button!.zones, target.id] : zone.button!.zones.filter((id) => id !== target.id);
                        // 至少要有一個區塊
                        if (zones.length > 0) set({ button: { ...zone.button!, zones } });
                      }}
                    />{" "}
                    {target.label}
                  </label>
                ))}
              </div>
            )}
            {zone.button.action === "navigate" && (
            <label className="inline">
              目標
              <select
                aria-label="按鈕的目標"
                value={
                  zone.button.target_dashboard
                    ? `d:${zone.button.target_dashboard}`
                    : zone.button.target_worksheet
                      ? `s:${zone.button.target_worksheet}`
                      : zone.button.target_story
                        ? `t:${zone.button.target_story}`
                        : ""
                }
                onChange={(e) => {
                  const [kind, target] = e.target.value.split(":");
                  set({
                    button: {
                      ...zone.button!,
                      action: "navigate",
                      target_dashboard: kind === "d" ? target : null,
                      target_worksheet: kind === "s" ? target : null,
                      target_story: kind === "t" ? target : null,
                      zones: [],
                      export: null,
                    },
                  });
                }}
              >
                {outline!.stories.map((st) => (
                  <option key={st.id} value={`t:${st.id}`}>
                    Story：{st.name}
                  </option>
                ))}
                {outline!.dashboards.map((d) => (
                  <option key={d.id} value={`d:${d.id}`}>
                    dashboard：{d.name}
                  </option>
                ))}
                {outline!.worksheets.map((w) => (
                  <option key={w.id} value={`s:${w.id}`}>
                    工作表：{w.name}
                  </option>
                ))}
              </select>
            </label>
            )}
            <label className="inline">
              文字
              <input
                aria-label="按鈕的文字"
                key={zone.button.caption ?? ""}
                defaultValue={zone.button.caption ?? ""}
                onBlur={(e) => set({ button: { ...zone.button!, caption: e.target.value || null } })}
              />
            </label>
          </>
        )}
        {zone.kind === "container" && !root && (
          <label className="inline">
            排列
            <select aria-label="容器的排列" value={zone.layout ?? "tiled"} onChange={(e) => change(zone.id, (z) => placeZone({ ...z, layout: e.target.value as Zone["layout"] }, z))}>
              <option value="horizontal">水平</option>
              <option value="vertical">垂直</option>
              <option value="tiled">並排（各自定位）</option>
            </select>
          </label>
        )}
        {zone.kind === "container" && (zone.layout === "horizontal" || zone.layout === "vertical") && (
          <label className="inline" title="沒有固定大小的子區塊沿排列方向等分（Tableau 的「平均分配內容」）">
            <input
              type="checkbox"
              checked={zone.distribute === true}
              onChange={(e) => {
                const distribute = e.target.checked;
                change(zone.id, (z) => placeZone({ ...z, distribute }, z));
              }}
            />{" "}
            平均分配內容
          </label>
        )}
        {floatingZone && (
          <div className="row position">
            {(["x", "y", "w", "h"] as const).map((k) => (
              <label key={k}>
                {k.toUpperCase()}
                <input
                  type="number"
                  min={0}
                  aria-label={`區塊的 ${k}`}
                  value={zone[k]}
                  onChange={(e) => {
                    const n = Math.max(0, Number(e.target.value));
                    change(zone.id, (z) => placeZone(z, { x: z.x, y: z.y, w: z.w, h: z.h, [k]: n }));
                  }}
                />
              </label>
            ))}
          </div>
        )}
        {along && !floatingZone && (
          <label className="inline">
            固定{along}
            <input
              type="number"
              min={0}
              aria-label={`固定${along}`}
              placeholder="自動"
              value={zone.fixed ?? ""}
              onChange={(e) => {
                const fixed = e.target.value === "" ? null : Math.max(0, Number(e.target.value));
                apply((d) => relayoutParent(updateZone(d, zone.id, (z) => ({ ...z, fixed })), zone.id));
              }}
            />
          </label>
        )}
        {!root && (
          <label className="inline">
            <input
              type="checkbox"
              checked={Boolean(zone.hidden)}
              onChange={(e) => {
                const hidden = e.target.checked;
                apply((d) => relayoutParent(updateZone(d, zone.id, (z) => ({ ...z, hidden })), zone.id));
              }}
            />{" "}
            隱藏
          </label>
        )}
        {!root && <StyleEditor style={zone.style} onChange={(style) => set({ style })} />}
        {!root && (
          <div className="dialog-actions">
            <button type="button" onClick={() => apply((d) => moveZone(d, zone.id, -1))}>
              {floatingZone ? "移到下層" : "前移"}
            </button>
            <button type="button" onClick={() => apply((d) => moveZone(d, zone.id, 1))}>
              {floatingZone ? "移到上層" : "後移"}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => {
                setSelected(parent?.id ?? null);
                apply((d) => removeZone(d, zone.id));
              }}
            >
              移除
            </button>
          </div>
        )}
      </div>
    );
  }

  const dashboardProps = (
    <div className="zone-props">
      <div className="prop-title">{layout ? `${DEVICE_NAMES[layout.device]}版面` : "dashboard"}</div>
      {layout ? (
        <label className="inline">
          大小
          <select
            aria-label="版面的大小"
            value={layout.sizing}
            onChange={(e) => {
              const sizing = e.target.value as DeviceLayout["sizing"];
              const kind = layout.device;
              history.apply((d) => ({
                ...d,
                layouts: (d.layouts ?? []).map((l) => (l.device === kind ? { ...l, sizing } : l)),
              }));
            }}
          >
            <option value="default">與預設版面相同</option>
            <option value="fixed">固定大小</option>
            <option value="automatic">符合畫面</option>
            <option value="fit_width">符合寬度（上下捲動）</option>
          </select>
        </label>
      ) : (
        <label className="inline">
          大小
          <select
            aria-label="dashboard 的大小"
            value={dashboard.sizing ?? "fixed"}
            onChange={(e) => {
              const sizing = e.target.value as Dashboard["sizing"];
              apply((d) => ({ ...d, sizing, min_size: sizing === "range" ? (d.min_size ?? { width: 800, height: 600 }) : null, max_size: sizing === "range" ? (d.max_size ?? { width: 1600, height: 1200 }) : null }));
            }}
          >
            <option value="fixed">固定大小</option>
            <option value="automatic">自動</option>
            <option value="range">範圍</option>
          </select>
        </label>
      )}
      {layout && (
        <p className="hint">
          檢視的畫面較短的一邊 500 像素以下用手機版面（沒有時用平板的）、501 到 800 用平板版面、超過 800 用桌面版面。
        </p>
      )}
      <div className="row">
        <label>
          寬
          <input
            type="number"
            min={100}
            aria-label="dashboard 的寬度"
            key={`w${dashboard.size.width}`}
            defaultValue={dashboard.size.width}
            onBlur={(e) => {
              const width = Math.max(100, Number(e.target.value));
              if (width !== dashboard.size.width) apply((d) => relayout({ ...d, size: { ...d.size, width } }));
            }}
          />
        </label>
        <label>
          高
          <input
            type="number"
            min={100}
            aria-label="dashboard 的高度"
            key={`h${dashboard.size.height}`}
            defaultValue={dashboard.size.height}
            onBlur={(e) => {
              const height = Math.max(100, Number(e.target.value));
              if (height !== dashboard.size.height) apply((d) => relayout({ ...d, size: { ...d.size, height } }));
            }}
          />
        </label>
      </div>
      <label className="inline">
        <input
          type="checkbox"
          checked={Boolean(dashboard.background)}
          onChange={(e) => apply((d) => ({ ...d, background: e.target.checked ? "#ffffff" : null }))}
        />{" "}
        背景
        <input
          type="color"
          aria-label="dashboard 的背景"
          disabled={!dashboard.background}
          value={(dashboard.background ?? "#ffffff").slice(0, 7)}
          onChange={(e) => {
            const background = e.target.value;
            apply((d) => ({ ...d, background }));
          }}
        />
      </label>
      <p className="hint">點選畫面上的區塊或版面配置中的項目以編輯它。</p>
    </div>
  );

  return (
    <div className="dash-editor" data-dashboard-editor={dashboard.id} data-saving={history.pending > 0 ? "true" : "false"}>
      <div className="editor-toolbar" role="toolbar" aria-label="dashboard 編輯">
        <button type="button" onClick={history.undo} disabled={!history.canUndo} title="復原（Ctrl+Z）">
          ↶ 復原
        </button>
        <button type="button" onClick={history.redo} disabled={!history.canRedo} title="重做（Ctrl+Y）">
          ↷ 重做
        </button>
        <span className="sep" />
        <label className="inline">
          dashboard
          <input
            key={`${dashboard.id}:${dashboard.name}`}
            aria-label="dashboard 名稱"
            defaultValue={dashboard.name}
            onBlur={(e) => {
              const name = e.target.value.trim();
              if (name && name !== dashboard.name) apply((d) => ({ ...d, name }));
              else e.target.value = dashboard.name;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
        </label>
        <button type="button" onClick={() => setActionsOpen(true)}>
          動作…
        </button>
        <label className="inline">
          縮放
          <select aria-label="縮放" value={String(zoom)} onChange={(e) => setZoom(e.target.value === "fit" ? "fit" : Number(e.target.value))}>
            <option value="fit">符合</option>
            <option value="0.5">50%</option>
            <option value="0.75">75%</option>
            <option value="1">100%</option>
          </select>
        </label>
        <label className="inline">
          版面
          <select
            aria-label="裝置版面"
            value={layout ? device : "default"}
            onChange={(e) => {
              setDevice(e.target.value as Device | "default");
              setSelected(null);
            }}
          >
            <option value="default">預設</option>
            {(full?.layouts ?? []).map((l) => (
              <option key={l.device} value={l.device}>
                {DEVICE_NAMES[l.device]}
              </option>
            ))}
          </select>
        </label>
        {layout ? (
          <button
            type="button"
            className="danger"
            onClick={() => {
              if (!window.confirm(`刪除${DEVICE_NAMES[layout.device]}版面？`)) return;
              const removed = layout.device;
              setDevice("default");
              setSelected(null);
              history.apply((d) => ({ ...d, layouts: (d.layouts ?? []).filter((l) => l.device !== removed) }));
            }}
          >
            刪除{DEVICE_NAMES[layout.device]}版面
          </button>
        ) : (
          <select
            aria-label="新增裝置版面"
            value=""
            onChange={(e) => {
              const kind = e.target.value as Device;
              if (!kind) return;
              history.apply((d) => ({ ...d, layouts: [...(d.layouts ?? []), newDeviceLayout(d, kind)] }));
              setDevice(kind);
              setSelected(null);
            }}
          >
            <option value="">新增裝置版面…</option>
            {(["desktop", "tablet", "phone"] as Device[])
              .filter((kind) => !(full?.layouts ?? []).some((l) => l.device === kind))
              .map((kind) => (
                <option key={kind} value={kind}>
                  {DEVICE_NAMES[kind]}
                </option>
              ))}
          </select>
        )}
        <span className="sep" />
        <button
          type="button"
          onClick={() =>
            void editApi
              .addDashboard(workbook, undefined, dashboard.size.width, dashboard.size.height)
              .then((created) => {
                onChanged();
                onOpen(created.id);
              })
              .catch((e: Error) => notify(e.message, "error"))
          }
        >
          新增 dashboard
        </button>
        <button
          type="button"
          onClick={() =>
            void editApi
              .duplicateDashboard(workbook, dashboard.id)
              .then((created) => {
                onChanged();
                onOpen(created.id);
              })
              .catch((e: Error) => notify(e.message, "error"))
          }
        >
          複製
        </button>
        <button
          type="button"
          className="danger"
          onClick={() => {
            if (!window.confirm(`刪除 dashboard「${dashboard.name}」？`)) return;
            void editApi
              .removeDashboard(workbook, dashboard.id)
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
        <button type="button" className="primary" onClick={() => onClose(dashboard.id)}>
          完成
        </button>
      </div>
      <aside className="dash-side" aria-label="dashboard 的物件">
        <div className="pane-section">工作表</div>
        <ul className="fields">
          {outline.worksheets.map((w) => (
            <li
              key={w.id}
              className="field sheet"
              draggable
              data-sheet={w.id}
              onDragStart={(e) => {
                e.dataTransfer.setData("application/x-tmig-sheet", w.id);
                e.dataTransfer.effectAllowed = "copy";
              }}
              onDoubleClick={() => addSheet(w.id)}
              title="拖曳到 dashboard 上，或連按兩下加入"
            >
              <span className="field-icon">▤</span>
              <span className="field-name">{w.name}</span>
              <span className="field-actions">
                <button type="button" className="link" title="加入" onClick={() => addSheet(w.id)}>
                  ＋
                </button>
              </span>
            </li>
          ))}
        </ul>
        <div className="pane-section">物件</div>
        <div className="object-buttons">
          <button type="button" onClick={() => add(makeZone("container", { layout: "horizontal" }, floating), { w: 600, h: 300 })}>
            水平容器
          </button>
          <button type="button" onClick={() => add(makeZone("container", { layout: "vertical" }, floating), { w: 300, h: 500 })}>
            垂直容器
          </button>
          <button type="button" onClick={() => add(makeZone("text", { text: "文字" }, floating), { w: 300, h: 80 })}>
            文字
          </button>
          <button type="button" onClick={() => add(makeZone("title", {}, floating), { w: 400, h: 40 })}>
            標題
          </button>
          <button type="button" onClick={() => add(makeZone("image", { image: outline.images[0] ?? "image.png" }, floating), { w: 200, h: 150 })}>
            圖片
          </button>
          <button type="button" onClick={() => add(makeZone("web", { url: "https://" }, floating), { w: 500, h: 400 })}>
            網頁
          </button>
          <button type="button" onClick={() => add(makeZone("blank", {}, floating), { w: 200, h: 100 })}>
            空白
          </button>
          <button
            type="button"
            onClick={() => {
              const target = outline.dashboards.find((d) => d.id !== dashboard.id) ?? outline.dashboards[0];
              add(
                makeZone(
                  "button",
                  {
                    button: {
                      action: "navigate",
                      target_dashboard: target?.id ?? null,
                      target_worksheet: target ? null : (outline.worksheets[0]?.id ?? null),
                      zones: [],
                      export: null,
                      caption: target ? `前往 ${target.name}` : "前往",
                      tooltip: null,
                      image: null,
                      image_hidden: null,
                      color: null,
                      background: null,
                    },
                  },
                  floating,
                ),
                { w: 160, h: 40 },
              );
            }}
          >
            導覽按鈕
          </button>
          <button
            type="button"
            title="匯出這個 dashboard（PDF、圖片或 PowerPoint；Tableau 的「下載」物件）"
            onClick={() =>
              add(
                makeZone(
                  "button",
                  {
                    button: {
                      action: "export",
                      target_dashboard: null,
                      target_worksheet: null,
                      zones: [],
                      export: "pdf",
                      caption: "匯出 PDF",
                      tooltip: null,
                      image: null,
                      image_hidden: null,
                      color: null,
                      background: null,
                    },
                  },
                  floating,
                ),
                { w: 160, h: 40 },
              )
            }
          >
            匯出按鈕
          </button>
        </div>
        <label className="inline hint">
          <input type="checkbox" checked={floating} onChange={(e) => setFloating(e.target.checked)} /> 以浮動方式加入
        </label>
        <div className="pane-section">篩選、參數與圖例</div>
        <select aria-label="控制項的工作表" value={controlTarget?.id ?? ""} onChange={(e) => setControlSheet(e.target.value)}>
          {outline.worksheets.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
        <div className="object-buttons">
          {controlTarget?.filters.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() =>
                add(
                  makeZone(
                    "filter",
                    { worksheet: controlTarget.id, field: f.field, control: (FILTER_CONTROLS[f.kind] ?? FILTER_CONTROLS.categorical)[0][0] },
                    floating,
                  ),
                  { w: 220, h: 160 },
                )
              }
            >
              篩選：{f.label}
            </button>
          ))}
          {controlTarget?.pages && (
            <button type="button" onClick={() => add(makeZone("page", { worksheet: controlTarget.id }, floating), { w: 260, h: 60 })}>
              頁面：{controlTarget.pages}
            </button>
          )}
          {controlTarget?.legends.map((legend) => (
            <button key={legend} type="button" onClick={() => add(makeZone("legend", { worksheet: controlTarget.id, legend }, floating), { w: 220, h: 140 })}>
              圖例：{legend === "color" ? "色彩" : legend === "size" ? "大小" : "形狀"}
            </button>
          ))}
          {controlTarget?.highlighters?.map((h) => (
            <button
              key={`highlighter:${JSON.stringify(h.field)}`}
              type="button"
              onClick={() => add(makeZone("highlighter", { worksheet: controlTarget.id, field: h.field }, floating), { w: 220, h: 70 })}
            >
              醒目提示：{h.label}
            </button>
          ))}
          {outline.parameters.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => add(makeZone("parameter", { parameter: p.id, control: (PARAMETER_CONTROLS[p.allowed] ?? PARAMETER_CONTROLS.all)[0][0] }, floating), { w: 220, h: 70 })}
            >
              參數：{p.caption}
            </button>
          ))}
          {(outline.sets ?? []).map((s) => (
            <button
              key={s.id}
              type="button"
              title="集合控制項：勾選的值是集合的成員"
              onClick={() => add(makeZone("set", { field: { field: s.id, agg: null, date: null, continuous: false, table_calc: null } }, floating), { w: 220, h: 160 })}
            >
              集合：{s.caption}
            </button>
          ))}
        </div>
        {controlTarget && controlTarget.filters.length === 0 && <p className="hint">這張工作表沒有篩選；先在工作表上加入篩選。</p>}
        <div className="pane-section">版面配置</div>
        <ul className="layout-tree" aria-label="版面配置">
          {(() => {
            const items: React.ReactNode[] = [];
            walkZones(dashboard.zones, (zone, _parent, depth) => {
              items.push(
                <li
                  key={zone.id}
                  className={`${selected === zone.id ? "selected" : ""}${zone.hidden ? " hidden" : ""}`}
                  style={{ paddingLeft: 6 + depth * 12 }}
                  onClick={() => setSelected(zone.id)}
                  data-tree-zone={zone.id}
                >
                  {zoneLabel(zone, names)}
                  {isFloating(dashboard, zone.id) ? "（浮動）" : ""}
                </li>,
              );
            });
            return items;
          })()}
        </ul>
      </aside>
      <div className="dash-canvas" ref={canvasRef} onClick={() => setSelected(null)}>
        <div className="dash-scaler" style={{ width: dashboard.size.width * scale, height: dashboard.size.height * scale }}>
          <div className="dash-stage" style={{ width: dashboard.size.width, height: dashboard.size.height, transform: `scale(${scale})` }}>
            <DashboardView
              workbook={workbook}
              ir={ir}
              // 編輯中的版面本身（不依編輯器的視窗改用裝置版面）
              dashboard={{ ...dashboard, sizing: "fixed", layouts: [] }}
              view={view}
              onView={setView}
              onNavigate={() => undefined}
              cases={[]}
            />
            <div
              className="dash-overlay"
              onDragOver={(e) => {
                if (e.dataTransfer.types.includes("application/x-tmig-sheet")) e.preventDefault();
              }}
              onDrop={(e) => dropSheet(e, null)}
            >
              {overlays.map(({ zone, depth, floating: isFloat }) => {
                const moving = drag?.id === zone.id ? drag : null;
                const x = zone.x + (moving?.mode === "move" ? moving.dx : 0);
                const y = zone.y + (moving?.mode === "move" ? moving.dy : 0);
                const w = zone.w + (moving?.mode === "resize" ? moving.dx : 0);
                const h = zone.h + (moving?.mode === "resize" ? moving.dy : 0);
                return (
                  <div
                    key={zone.id}
                    className={`dash-zone${selected === zone.id ? " selected" : ""}${zone.kind === "container" ? " container" : ""}${isFloat ? " floating" : ""}`}
                    data-overlay={zone.id}
                    style={{ left: x, top: y, width: Math.max(4, w), height: Math.max(4, h), zIndex: depth + (isFloat ? 100 : 1) }}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelected(zone.id);
                    }}
                    onMouseDown={(e) => {
                      if (!isFloat || e.button !== 0) return;
                      e.preventDefault();
                      setSelected(zone.id);
                      setDrag({ id: zone.id, mode: "move", startX: e.clientX, startY: e.clientY, dx: 0, dy: 0 });
                    }}
                    onDragOver={(e) => {
                      if (e.dataTransfer.types.includes("application/x-tmig-sheet")) e.preventDefault();
                    }}
                    onDrop={(e) => dropSheet(e, zone)}
                  >
                    {selected === zone.id && <span className="dash-zone-label">{zoneLabel(zone, names)}</span>}
                    {isFloat && selected === zone.id && (
                      <span
                        className="dash-handle"
                        aria-label="調整大小"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setDrag({ id: zone.id, mode: "resize", startX: e.clientX, startY: e.clientY, dx: 0, dy: 0 });
                        }}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
      <aside className="dash-props" aria-label="屬性">
        {zoneProps ?? dashboardProps}
      </aside>
      <datalist id="dash-images">
        {outline.images.map((image) => (
          <option key={image} value={image} />
        ))}
      </datalist>
      {notice && (
        <div className={`toast${notice.kind === "error" ? " error" : ""}`} role="status">
          {notice.text}
        </div>
      )}
      {textZone && (
        <ZoneTextDialog
          zone={textZone}
          dashboard={dashboard}
          tokens={parameterTokens}
          onClose={() => setTextZone(null)}
          onSave={(runs) => {
            // text 是沒有格式的純文字（參數以 <名稱> 表示），content 是各段的格式與參數
            change(textZone, (z) => ({ ...z, text: plainText(runs, parameterTokens) || " ", content: runs }));
            setTextZone(null);
          }}
        />
      )}
      {actionsOpen && (
        <ActionsDialog
          workbook={workbook}
          dashboard={dashboard}
          outline={outline}
          onClose={() => setActionsOpen(false)}
          onSaved={() => {
            void reloadOutline();
            onChanged();
          }}
        />
      )}
    </div>
  );
}

/** 「當作篩選器使用」產生的篩選動作名稱的結尾（Tableau 為「Filter N (generated)」）。 */
const GENERATED = "（自動產生）";

/** 文字物件的格式文字（Tableau 的「編輯文字」）：粗體、斜體、底線、字級、顏色、對齊，可以插入參數的值。 */
function ZoneTextDialog({
  zone,
  dashboard,
  tokens,
  onClose,
  onSave,
}: {
  zone: string;
  dashboard: Dashboard;
  tokens: RichToken[];
  onClose: () => void;
  onSave: (runs: RichRun[]) => void;
}) {
  let found: Zone | null = null;
  walkZones(dashboard.zones, (z) => {
    if (z.id === zone) found = z;
  });
  const current = found as Zone | null;
  const [runs, setRuns] = useState<RichRun[]>(() =>
    current?.content && current.content.length > 0 ? (current.content as RichRun[]) : current?.text ? [{ text: current.text }] : [],
  );
  return (
    <Modal title="編輯文字" onClose={onClose}>
      <RichTextEditor label="文字物件的內容" value={runs} tokens={tokens} onChange={setRuns} rows={6} />
      <div className="dialog-actions">
        <button type="button" className="primary" onClick={() => onSave(runs)}>
          確定
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}
