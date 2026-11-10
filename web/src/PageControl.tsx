// 頁面控制項（頁面架）：切換工作表目前的頁、上一頁、下一頁與自動播放。

import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { fieldKey, type ViewState } from "./state";
import type { FieldUse, IR, Scalar } from "./types";

const PLAY_INTERVAL = 1000;

/** 頁面的顯示：日期截斷依單位（年、年 季、年-月），其他照原值。 */
export function pageLabel(use: FieldUse, value: Scalar): string {
  if (value === null) return "Null";
  if (typeof value === "string" && use.date?.kind === "trunc" && /^\d{4}-\d{2}-\d{2}/.test(value)) {
    const [year, month] = [Number(value.slice(0, 4)), Number(value.slice(5, 7))];
    if (use.date.unit === "year") return String(year);
    if (use.date.unit === "quarter") return `${year} Q${Math.floor((month - 1) / 3) + 1}`;
    if (use.date.unit === "month") return `${year}-${String(month).padStart(2, "0")}`;
    return value.replace("T00:00:00", "");
  }
  return String(value);
}

/** 報表中這張工作表的頁面架：欄位、存檔時的頁與欄位名稱。 */
export function pagesOf(ir: IR, worksheet: string): { use: FieldUse | null; saved: Scalar; caption: string } {
  const sheet = ir.worksheets.find((w) => w.id === worksheet);
  const use = sheet?.pages ?? null;
  const caption = use ? (ir.datasources.flatMap((d) => d.fields).find((f) => f.id === use.field)?.caption ?? use.field) : "";
  return { use, saved: sheet?.page ?? null, caption };
}

export function PageControl({
  workbook,
  worksheet,
  use,
  saved,
  caption,
  view,
  onView,
}: {
  workbook: string;
  worksheet: string;
  /** 頁面架上的欄位 */
  use: FieldUse | null;
  /** 存檔時的頁（null 為第一頁） */
  saved: Scalar;
  caption: string;
  view: ViewState;
  onView: (view: ViewState) => void;
}) {
  const key = use ? fieldKey(use) : null;
  const [values, setValues] = useState<Scalar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!key) return;
    let cancelled = false;
    api
      .domain(workbook, key, "values")
      .then((domain) => !cancelled && setValues(domain.kind === "values" ? domain.values : []))
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [workbook, key]);
  const chosen = view.pages !== undefined && worksheet in view.pages;
  const current = chosen ? view.pages![worksheet] : saved;
  const index = useMemo(
    () => (values ? values.findIndex((v) => JSON.stringify(v) === JSON.stringify(current)) : -1),
    [values, current],
  );
  const go = (i: number) => {
    if (!values || values.length === 0) return;
    const next = values[((i % values.length) + values.length) % values.length];
    onView({ ...view, pages: { ...(view.pages ?? {}), [worksheet]: next } });
  };
  // 存檔時沒有記錄目前的頁：與 Tableau 相同顯示第一頁
  useEffect(() => {
    if (values && values.length > 0 && !chosen && saved === null) go(0);
  }, [values]);
  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => go(index + 1), PLAY_INTERVAL);
    return () => window.clearTimeout(timer);
  }, [playing, index]);
  if (!use) return null;
  if (error) return <div className="page-control error">頁面：{error}</div>;
  return (
    <div className="page-control" data-page-control={worksheet} aria-label={`頁面：${caption}`}>
      <span className="page-title">{caption}</span>
      <button type="button" title="上一頁" aria-label="上一頁" onClick={() => go(index - 1)} disabled={!values?.length}>
        ◀
      </button>
      <select aria-label="目前的頁" value={index} onChange={(e) => go(Number(e.target.value))} disabled={!values?.length}>
        {index < 0 && <option value={-1}>{current === null ? "…" : pageLabel(use, current)}</option>}
        {(values ?? []).map((v, i) => (
          <option key={i} value={i}>
            {pageLabel(use, v)}
          </option>
        ))}
      </select>
      <button type="button" title="下一頁" aria-label="下一頁" onClick={() => go(index + 1)} disabled={!values?.length}>
        ▶
      </button>
      <button
        type="button"
        title={playing ? "停止" : "自動播放"}
        aria-label={playing ? "停止" : "自動播放"}
        aria-pressed={playing}
        onClick={() => setPlaying((p) => !p)}
        disabled={!values?.length}
      >
        {playing ? "■" : "⏵"}
      </button>
    </div>
  );
}
