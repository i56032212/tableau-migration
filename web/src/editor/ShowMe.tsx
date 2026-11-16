// 智慧顯示的面板（Tableau 工具列右邊的 Show Me）：圖表類型的格子，不能用的變淡、滑過時顯示需要的欄位，
// 建議的圖表加上外框；點選後重新排列檢視（可以復原）。

import { useState } from "react";
import type { FieldUse } from "../types";
import { editApi } from "./api";
import { useEditor } from "./context";
import { CHARTS, recommended, type ShowMeContext, viewParts } from "./showMeCharts";

function plain(field: string): FieldUse {
  return { field, agg: null, date: null, continuous: false, table_calc: null };
}

export function ShowMePanel({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const [hint, setHint] = useState<string | null>(null);
  const parts = viewParts(editor.sheet, editor.fields);
  const best = recommended(parts);
  const ctx: ShowMeContext = {
    fields: editor.fields,
    bins: async (measure) => {
      const datasource = editor.sheet.datasource;
      const ds = editor.catalog.datasources.find((d) => d.id === datasource);
      const existing = ds?.fields.find((f) => f.bin && f.bin.source === measure.field);
      if (existing) return existing.id;
      // 與 Tableau 的建議相同：分組數 = 3 + log2(n) × log(n)，大小 = (最大 − 最小) ÷ 分組數
      const [range, values] = await Promise.all([
        editApi.domain(editor.workbook, plain(measure.field), { kind: "range" }),
        editApi.domain(editor.workbook, plain(measure.field), { kind: "values" }),
      ]);
      const n = values.kind === "values" ? values.values.length : 0;
      let size = 1;
      if (range.kind === "range" && typeof range.min === "number" && typeof range.max === "number" && n > 1) {
        const raw = (range.max - range.min) / (3 + Math.log2(n) * Math.log10(n));
        size = raw >= 1 ? Math.round(raw) : Number(raw.toPrecision(2)) || 1;
      }
      const created = await editApi.saveBins(editor.workbook, datasource, { source: measure.field, caption: "", size, parameter: null, field: null });
      editor.reloadCatalog();
      return created.id;
    },
  };
  return (
    <div className="show-me" role="dialog" aria-label="智慧顯示">
      <div className="show-me-head">
        <strong>智慧顯示</strong>
        <button type="button" className="link close" aria-label="關閉智慧顯示" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="show-me-grid">
        {CHARTS.map((chart) => {
          const ok = chart.ok(parts);
          return (
            <button
              key={chart.id}
              type="button"
              className={`show-me-chart${chart.id === best ? " recommended" : ""}`}
              data-chart={chart.id}
              disabled={!ok}
              aria-label={chart.label}
              title={`${chart.label}：${chart.needs}`}
              onMouseEnter={() => setHint(`${chart.label}：${chart.needs}`)}
              onMouseLeave={() => setHint(null)}
              onClick={() => {
                editor.apply((sheet) => chart.build(sheet, viewParts(sheet, editor.fields), ctx));
                editor.notify(`已改為${chart.label}`);
              }}
            >
              <span className="show-me-icon" aria-hidden="true">
                {chart.icon}
              </span>
              <span className="show-me-label">{chart.label}</span>
            </button>
          );
        })}
      </div>
      <p className="hint show-me-hint">{hint ?? (best ? "外框醒目的是建議的圖表；滑過圖表看需要的欄位" : "把欄位放到檢視中，智慧顯示會列出可用的圖表")}</p>
    </div>
  );
}
