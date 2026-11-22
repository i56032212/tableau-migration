// 動畫（Tableau 的「格式＞動畫」）：活頁簿預設（開關、持續時間、同時或依序）與選取的工作表（每一項可以沿用
// 活頁簿的預設），以及「全部重設」（所有工作表改回預設、活頁簿預設回到關）。工作表的設定與其他編輯相同可以復原；
// 活頁簿預設直接存進報表（不在工作表的歷程裡）。持續時間的預設選項是推定的（Tableau 的說明只寫可以選預設的
// 或自訂最多 10 秒）。

import { useState } from "react";
import { DEFAULT_DURATION, type AnimationSetting } from "../markAnimation";
import { Modal } from "../Server";

/** 持續時間的選項（秒） */
const PRESETS: [number, string][] = [
  [0.15, "快（0.15 秒）"],
  [DEFAULT_DURATION, `一般（${DEFAULT_DURATION} 秒）`],
  [0.5, "慢（0.5 秒）"],
  [1, "最慢（1 秒）"],
];

/** 沒有設定的項目都拿掉；全部沒有設定時為 null（沿用活頁簿的預設） */
export function tidyAnimation(setting: AnimationSetting | null | undefined): AnimationSetting | null {
  if (!setting) return null;
  const out: AnimationSetting = {};
  if (setting.on !== null && setting.on !== undefined) out.on = setting.on;
  if (setting.duration !== null && setting.duration !== undefined) out.duration = Math.max(0, Math.min(10, setting.duration));
  if (setting.style) out.style = setting.style;
  return Object.keys(out).length > 0 ? out : null;
}

/** 活頁簿預設：關（沒有設定開）時 on 不寫；全部是預設值時為 null */
export function tidyWorkbookAnimation(setting: AnimationSetting | null | undefined): AnimationSetting | null {
  return tidyAnimation(setting ? { ...setting, on: setting.on ? true : null } : null);
}

function DurationPicker({
  value,
  inherit,
  label,
  onChange,
}: {
  value: number | null | undefined;
  /** 選取的工作表：可以選「（預設）」沿用活頁簿的設定 */
  inherit: boolean;
  label: string;
  onChange: (value: number | null) => void;
}) {
  const preset = value === null || value === undefined ? "" : PRESETS.some(([seconds]) => seconds === value) ? String(value) : "custom";
  const [custom, setCustom] = useState(preset === "custom");
  return (
    <span className="animation-duration">
      <select
        aria-label={label}
        value={custom ? "custom" : inherit || value !== null ? preset : String(DEFAULT_DURATION)}
        onChange={(e) => {
          const next = e.target.value;
          setCustom(next === "custom");
          if (next === "") onChange(null);
          else if (next !== "custom") onChange(Number(next));
          else onChange(value ?? DEFAULT_DURATION);
        }}
      >
        {inherit && <option value="">（預設）</option>}
        {PRESETS.map(([seconds, text]) => (
          <option key={seconds} value={String(seconds)}>
            {text}
          </option>
        ))}
        <option value="custom">自訂…</option>
      </select>
      {custom && (
        <label className="inline">
          <input
            type="number"
            min={0.1}
            max={10}
            step={0.1}
            aria-label={`${label}（秒）`}
            className="narrow-number"
            value={value ?? DEFAULT_DURATION}
            onChange={(e) => onChange(Math.max(0.1, Math.min(10, Number(e.target.value) || DEFAULT_DURATION)))}
          />{" "}
          秒
        </label>
      )}
    </span>
  );
}

export function AnimationDialog({
  workbook,
  sheet,
  sheetName,
  onSave,
  onReset,
  onClose,
}: {
  /** 活頁簿預設 */
  workbook: AnimationSetting | null;
  /** 選取的工作表的設定 */
  sheet: AnimationSetting | null;
  sheetName: string;
  onSave: (workbook: AnimationSetting | null, sheet: AnimationSetting | null) => void;
  /** 全部重設 */
  onReset: () => void;
  onClose: () => void;
}) {
  const [book, setBook] = useState<AnimationSetting>(workbook ?? {});
  const [own, setOwn] = useState<AnimationSetting>(sheet ?? {});
  const tri = (value: boolean | null | undefined) => (value === true ? "on" : value === false ? "off" : "");
  return (
    <Modal title="動畫" onClose={onClose}>
      <div className="animation-dialog">
        <fieldset>
          <legend>活頁簿預設</legend>
          <div className="row">
            <label className="inline">
              動畫
              <select aria-label="活頁簿預設的動畫" value={book.on ? "on" : "off"} onChange={(e) => setBook({ ...book, on: e.target.value === "on" })}>
                <option value="on">開</option>
                <option value="off">關</option>
              </select>
            </label>
            <label className="inline">
              持續時間
              <DurationPicker inherit={false} label="活頁簿預設的持續時間" value={book.duration} onChange={(duration) => setBook({ ...book, duration })} />
            </label>
            <label className="inline">
              樣式
              <select
                aria-label="活頁簿預設的樣式"
                value={book.style ?? "simultaneous"}
                onChange={(e) => setBook({ ...book, style: e.target.value as AnimationSetting["style"] })}
              >
                <option value="simultaneous">同時</option>
                <option value="sequential">依序</option>
              </select>
            </label>
          </div>
        </fieldset>
        <fieldset>
          <legend>選取的工作表：{sheetName}</legend>
          <div className="row">
            <label className="inline">
              動畫
              <select
                aria-label="工作表的動畫"
                value={tri(own.on)}
                onChange={(e) => setOwn({ ...own, on: e.target.value === "" ? null : e.target.value === "on" })}
              >
                <option value="">（預設）</option>
                <option value="on">開</option>
                <option value="off">關</option>
              </select>
            </label>
            <label className="inline">
              持續時間
              <DurationPicker inherit label="工作表的持續時間" value={own.duration} onChange={(duration) => setOwn({ ...own, duration })} />
            </label>
            <label className="inline">
              樣式
              <select
                aria-label="工作表的樣式"
                value={own.style ?? ""}
                onChange={(e) => setOwn({ ...own, style: (e.target.value || null) as AnimationSetting["style"] })}
              >
                <option value="">（預設）</option>
                <option value="simultaneous">同時</option>
                <option value="sequential">依序</option>
              </select>
            </label>
          </div>
        </fieldset>
        <p className="muted">
          同時：所有變化一起播放。依序：先淡出、再移動與排序、最後淡入。地圖、圓餅、文字標記、標題、參考線與趨勢線不做動畫。
        </p>
        <div className="dialog-actions">
          <button
            type="button"
            title="所有工作表改回沿用活頁簿的預設，活頁簿預設回到關"
            onClick={() => {
              onReset();
              onClose();
            }}
          >
            全部重設
          </button>
          <span className="spacer" />
          <button
            type="button"
            className="primary"
            onClick={() => {
              onSave(tidyWorkbookAnimation(book), tidyAnimation(own));
              onClose();
            }}
          >
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
