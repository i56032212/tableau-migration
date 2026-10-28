import { useEffect, useRef, useState } from "react";
import embed, { type Result } from "vega-embed";
import { api } from "./api";
import type { QueryState, RenderResponse } from "./types";

interface Props {
  workbook: string;
  worksheet: string;
  state: QueryState;
  hideLegend?: boolean;
  showTitle?: boolean;
  onRendered?: (response: RenderResponse) => void;
}

/** 多張圖（同一架上有多個量值）無法自動填滿容器：依區塊大小平分每張圖的寬高。 */
function sizeConcat(spec: Record<string, any>, width: number, height: number) {
  const AXIS = 110; // 座標軸標籤與標題約占的寬度
  const GAP = 28; // 每張圖之間與 x 軸標籤的高度
  if (Array.isArray(spec.vconcat)) {
    const n = spec.vconcat.length;
    const h = Math.max(Math.floor((height - GAP * n - 30) / n), 60);
    spec.vconcat = spec.vconcat.map((s: Record<string, any>) => ({ ...s, width: Math.max(width - AXIS, 80), height: h }));
  }
  if (Array.isArray(spec.hconcat)) {
    const n = spec.hconcat.length;
    const w = Math.max(Math.floor((width - AXIS * n) / n), 60);
    spec.hconcat = spec.hconcat.map((s: Record<string, any>) => ({ ...s, width: w, height: Math.max(height - 60, 60) }));
  }
}

/** 一張工作表：向查詢服務取 Vega-Lite 規格並繪製；可切換顯示資料表。 */
export function WorksheetView({ workbook, worksheet, state, hideLegend, showTitle = true, onRendered }: Props) {
  const chartRef = useRef<HTMLDivElement>(null);
  const [response, setResponse] = useState<RenderResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showTable, setShowTable] = useState(false);
  const stateKey = JSON.stringify(state);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .render(workbook, worksheet, JSON.parse(stateKey) as QueryState)
      .then((result) => {
        if (cancelled) return;
        setResponse(result);
        setError(null);
        onRendered?.(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // onRendered 只用來回報結果，不觸發重新查詢
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workbook, worksheet, stateKey]);

  useEffect(() => {
    const element = chartRef.current;
    if (!element || !response?.spec || showTable) return;
    let view: Result | null = null;
    let disposed = false;
    const spec = structuredClone(response.spec) as Record<string, any>;
    if (hideLegend && spec.encoding?.color) spec.encoding.color.legend = null;
    sizeConcat(spec, element.clientWidth, element.clientHeight);
    embed(element, spec as any, { actions: false, renderer: "svg" })
      .then((result) => {
        if (disposed) result.finalize();
        else view = result;
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
    return () => {
      disposed = true;
      view?.finalize();
    };
  }, [response, hideLegend, showTable]);

  const title = response?.name ?? worksheet;
  const meta = (
    <span className="worksheet-meta">
      {response && !response.unsupported && <span>{response.rows} 列</span>}
      {response?.table && (
        <button type="button" className="link" onClick={() => setShowTable(!showTable)}>
          {showTable ? "圖表" : "資料"}
        </button>
      )}
    </span>
  );
  return (
    <div className="worksheet" data-worksheet={worksheet} data-loading={loading ? "true" : "false"}>
      {showTitle ? (
        <div className="worksheet-header">
          <span className="worksheet-title">{title}</span>
          {meta}
        </div>
      ) : (
        // Tableau 不顯示標題時，列數與資料切換浮在右上角，不占圖表空間
        <div className="worksheet-overlay">{meta}</div>
      )}
      <div className="worksheet-body">
        {error && <div className="placeholder error">查詢失敗：{error}</div>}
        {!error && response?.unsupported && (
          <div className="placeholder unsupported">未支援：{response.unsupported}</div>
        )}
        {!error && !response?.unsupported && showTable && response?.table && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  {response.table.headers.map((h) => (
                    <th key={h}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {response.table.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j} className={/^[-\d$,.%()NT]+$/.test(cell) ? "num" : undefined}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!error && !response?.unsupported && !showTable && <div className="chart" ref={chartRef} />}
        {loading && !response && <div className="placeholder">載入中…</div>}
      </div>
    </div>
  );
}
