// 描述趨勢模型、描述預測、描述叢集（Tableau「分析」選單中各模型的說明），以及把叢集存成群組。
// 數字取自這張工作表目前存檔的定義的查詢結果：趨勢線的模型在 spec.usermeta.trends，叢集與預測在回應的 describe。

import { useEffect, useState } from "react";
import { api } from "../api";
import { Modal } from "../Server";
import type { RenderResponse } from "../types";
import { editApi } from "./api";
import { useEditor } from "./context";

export type DescribeKind = "trend" | "forecast" | "cluster";

interface TrendModel {
  group: string[];
  fit: string;
  equation: string;
  observations: number;
  filtered: number;
  model_df: number;
  residual_df: number;
  r2: number | null;
  p: number | null;
  standard_error: number | null;
  terms: number[];
  /** 各係數（X 的次方由高到低，截距在最後）：估計值、標準誤、t 值與 P 值 */
  coefficients?: { power: number; value: number; standard_error: number | null; t: number | null; p: number | null }[];
}

interface ForecastModel {
  series: unknown[];
  measure: string;
  model: string;
  trend: string;
  season: string;
  alpha: number;
  beta: number;
  gamma: number;
  aic: number | null;
  rmse: number | null;
  mae?: number | null;
  mase?: number | null;
  mape?: number | null;
  quality?: "good" | "ok" | "poor" | null;
  observations: number;
  period: number;
  horizon: number;
  first: unknown;
  last: unknown;
  until: unknown;
  /** 摘要：第一個估計的期、它的估計值與精度（預測區間與估計值的距離） */
  initial_time?: unknown;
  initial?: number | null;
  initial_precision?: number | null;
  /** 最後一個估計值減第一個估計值 */
  change?: number | null;
  /** 最後一個完整季節的季節成分的高值、低值（有季節的模型） */
  seasonal_high?: number | null;
  seasonal_low?: number | null;
  /** 趨勢與季節的貢獻（百分比，加起來 100%） */
  contribution_trend?: number | null;
  contribution_season?: number | null;
}

interface ForecastDescription {
  unit: string;
  ignore_last: number;
  confidence: number;
  seasonal: boolean;
  models: ForecastModel[];
}

interface ClusterDescription {
  count: number;
  points: number;
  between?: number;
  within?: number;
  total?: number;
  unclustered: number;
  automatic: boolean;
  column: string;
  measures: string[];
  clusters: { cluster: number; items: number; centers: number[] }[];
  variables: { f: number | null; p: number | null; between: number; within: number }[];
}

const TITLES: Record<DescribeKind, string> = { trend: "描述趨勢模型", forecast: "描述預測", cluster: "描述叢集" };
const FITS: Record<string, string> = { linear: "線性", log: "對數", exp: "指數", power: "乘冪", polynomial: "多項式" };
const COMPONENTS: Record<string, string> = { none: "無", additive: "加法", multiplicative: "乘法" };
const UNITS: Record<string, string> = { year: "年", year_number: "年", quarter: "季", month: "月", week: "週", day: "日", hour: "小時" };
/** 預測的品質（與「下一期等於這一期」的單純預測相比）：誤差不到一半、較小、較大 */
const QUALITY: Record<string, string> = { good: "良好", ok: "尚可", poor: "不佳" };

/** 係數的名稱：截距，或自變數（對數與乘冪模型取自然對數）的次方。 */
function termName(fit: string, power: number): string {
  if (power === 0) return fit === "exp" || fit === "power" ? "截距（ln b0）" : "截距";
  const x = fit === "log" || fit === "power" ? "ln(X)" : "X";
  return power === 1 ? x : `${x}^${power}`;
}

function number(value: number | null | undefined, digits = 6): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", { maximumSignificantDigits: digits });
}

function pValue(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 0.0001 ? "< 0.0001" : number(value, 4);
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

/** 期間（日期取年月日；年份的整數照原樣）。 */
function period(value: unknown): string {
  return text(value).slice(0, 10);
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "—" : `${number(value, 3)}%`;
}

export function DescribeModelDialog({ kind, onClose, onSaved }: { kind: DescribeKind; onClose: () => void; onSaved: () => void }) {
  const editor = useEditor();
  const [response, setResponse] = useState<RenderResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .render(editor.workbook, editor.sheet.id, { filters: {}, parameters: {} })
      .then(setResponse)
      .catch((e: Error) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor.workbook, editor.sheet.id]);
  const table = response?.table;
  const header = (key: string) => {
    const at = table?.keys.indexOf(key) ?? -1;
    return at >= 0 ? (table?.headers[at] ?? key) : key;
  };
  let body: React.ReactNode = <p className="hint">計算中…</p>;
  if (error) body = <p className="error">{error}</p>;
  else if (response) {
    const trends = ((response.spec?.usermeta as { trends?: TrendModel[] } | undefined)?.trends ?? []) as TrendModel[];
    const forecast = response.describe?.forecast as ForecastDescription | undefined;
    const cluster = response.describe?.cluster as ClusterDescription | undefined;
    if (kind === "trend") {
      body =
        trends.length === 0 ? (
          <p className="hint">這張工作表沒有畫出趨勢線（資料不足，或座標軸不是連續的）。</p>
        ) : (
          <div className="describe-trends">
            <p className="hint">模型以最小平方法配適；指數與乘冪模型的係數、R 平方、P 值與標準誤是取對數之後的值。</p>
            {trends.map((model, i) => (
              <div key={i} className="describe-trend">
              <table className="describe-table" data-trend={i}>
                <caption>
                  {FITS[model.fit] ?? model.fit}趨勢模型{model.group.filter(Boolean).length > 0 ? `：${model.group.filter(Boolean).join("、")}` : ""}
                </caption>
                <tbody>
                  <tr>
                    <th>公式</th>
                    <td>{model.equation}</td>
                  </tr>
                  <tr>
                    <th>建立模型的觀測數</th>
                    <td>{model.observations}</td>
                  </tr>
                  <tr>
                    <th>篩選掉的觀測數</th>
                    <td>{model.filtered}</td>
                  </tr>
                  <tr>
                    <th>模型自由度</th>
                    <td>{model.model_df}</td>
                  </tr>
                  <tr>
                    <th>殘差自由度（DF）</th>
                    <td>{model.residual_df}</td>
                  </tr>
                  <tr>
                    <th>R 平方</th>
                    <td>{number(model.r2)}</td>
                  </tr>
                  <tr>
                    <th>標準誤</th>
                    <td>{number(model.standard_error)}</td>
                  </tr>
                  <tr>
                    <th>P 值（顯著性）</th>
                    <td>{pValue(model.p)}</td>
                  </tr>
                  <tr>
                    <th>係數（依 X 的次方）</th>
                    <td>{model.terms.map((t) => number(t)).join("、")}</td>
                  </tr>
                </tbody>
              </table>
              {(model.coefficients ?? []).length > 0 && (
                // 各係數（Tableau 的 Individual trend lines）：一般最小平方法的標準誤、t 值與 P 值
                <table className="describe-table wide" data-trend-terms={i}>
                  <caption>各係數</caption>
                  <thead>
                    <tr>
                      <th>項</th>
                      <th>值</th>
                      <th>標準誤</th>
                      <th>t 值</th>
                      <th>P 值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(model.coefficients ?? []).map((c) => (
                      <tr key={c.power} data-power={c.power}>
                        <td>{termName(model.fit, c.power)}</td>
                        <td>{number(c.value)}</td>
                        <td>{number(c.standard_error)}</td>
                        <td>{number(c.t)}</td>
                        <td>{pValue(c.p)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              </div>
            ))}
          </div>
        );
    } else if (kind === "forecast") {
      body = !forecast ? (
        <p className="hint">這張工作表沒有預測。</p>
      ) : (
        <div className="describe-forecast">
          <table className="describe-table">
            <caption>用來建立預測的選項</caption>
            <tbody>
              <tr>
                <th>時間序列的細度</th>
                <td>{UNITS[forecast.unit] ?? forecast.unit}</td>
              </tr>
              <tr>
                <th>忽略最後</th>
                <td>{forecast.ignore_last} 期</td>
              </tr>
              <tr>
                <th>季節模式</th>
                <td>{forecast.seasonal ? "自動尋找" : "無"}</td>
              </tr>
              <tr>
                <th>預測區間</th>
                <td>{Math.round(forecast.confidence * 100)}%</td>
              </tr>
            </tbody>
          </table>
          {forecast.models.length === 0 && <p className="hint">資料不足，沒有估計出模型。</p>}
          {forecast.models.length > 0 && (
            // 摘要（Tableau 描述預測的「摘要」索引標籤）：初始、相對於初始值的變化、季節影響、貢獻與品質
            <table className="describe-table wide" data-forecast-summary="">
              <caption>摘要</caption>
              <thead>
                <tr>
                  <th rowSpan={2}>量值</th>
                  <th rowSpan={2}>序列</th>
                  <th rowSpan={2} title="第一個估計的期的估計值 ± 精度（預測區間與估計值的距離）">
                    初始
                  </th>
                  <th rowSpan={2} title="最後一個估計值減第一個估計值">
                    相對於初始值的變化
                  </th>
                  <th colSpan={2} title="最後一個完整季節的季節成分（與趨勢的差）">
                    季節影響
                  </th>
                  <th colSpan={2} title="趨勢與季節占估計值變化的比例">
                    貢獻
                  </th>
                  <th rowSpan={2}>品質</th>
                </tr>
                <tr>
                  <th>高值</th>
                  <th>低值</th>
                  <th>趨勢</th>
                  <th>季節性</th>
                </tr>
              </thead>
              <tbody>
                {forecast.models.map((model, i) => (
                  <tr key={i} data-summary={i}>
                    <td>{header(model.measure)}</td>
                    <td>{model.series.map(text).join("、")}</td>
                    <td data-initial="">
                      {number(model.initial)} ± {number(model.initial_precision)}
                      <span className="hint">（{period(model.initial_time)}）</span>
                    </td>
                    <td data-change="">
                      {number(model.change)}
                      <span className="hint">
                        （{period(model.initial_time)}–{period(model.until)}）
                      </span>
                    </td>
                    <td data-seasonal-high="">{number(model.seasonal_high)}</td>
                    <td data-seasonal-low="">{number(model.seasonal_low)}</td>
                    <td data-contribution-trend="">{percent(model.contribution_trend)}</td>
                    <td data-contribution-season="">{percent(model.contribution_season)}</td>
                    <td>{model.quality ? QUALITY[model.quality] : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {forecast.models.length > 0 && (
            <table className="describe-table wide">
              <caption>模型（指數平滑）</caption>
              <thead>
                <tr>
                  <th>量值</th>
                  <th>序列</th>
                  <th>趨勢</th>
                  <th>季節</th>
                  <th>Alpha</th>
                  <th>Beta</th>
                  <th>Gamma</th>
                  <th title="與「下一期等於這一期」的單純預測相比：誤差不到一半為良好、較小為尚可、較大為不佳">品質</th>
                  <th>RMSE</th>
                  <th title="平均絕對誤差">MAE</th>
                  <th title="平均絕對誤差 ÷ 單純預測的平均絕對誤差">MASE</th>
                  <th title="平均絕對百分比誤差；資料中有 0 時沒有定義">MAPE</th>
                  <th>AIC</th>
                  <th>觀測數</th>
                  <th>預測期數</th>
                  <th>預測到</th>
                </tr>
              </thead>
              <tbody>
                {forecast.models.map((model, i) => (
                  <tr key={i}>
                    <td>{header(model.measure)}</td>
                    <td>{model.series.map(text).join("、")}</td>
                    <td>{COMPONENTS[model.trend] ?? model.trend}</td>
                    <td>{COMPONENTS[model.season] ?? model.season}</td>
                    <td>{number(model.alpha, 3)}</td>
                    <td>{model.trend === "none" ? "—" : number(model.beta, 3)}</td>
                    <td>{model.season === "none" ? "—" : number(model.gamma, 3)}</td>
                    <td data-quality={model.quality ?? ""}>{model.quality ? QUALITY[model.quality] : "—"}</td>
                    <td>{number(model.rmse)}</td>
                    <td>{number(model.mae)}</td>
                    <td>{number(model.mase, 3)}</td>
                    <td>{model.mape === null || model.mape === undefined ? "—" : `${number(model.mape, 3)}%`}</td>
                    <td>{number(model.aic)}</td>
                    <td>{model.observations}</td>
                    <td>{model.horizon}</td>
                    <td>{text(model.until).slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      );
    } else {
      body = !cluster ? <p className="hint">這張工作表沒有叢集。</p> : <ClusterSummary cluster={cluster} response={response} header={header} onSaved={onSaved} />;
    }
  }
  return (
    <Modal title={TITLES[kind]} onClose={onClose}>
      <div className="describe-dialog" data-describe={kind}>
        {body}
        <div className="dialog-actions">
          <button type="button" className="primary" onClick={onClose}>
            關閉
          </button>
        </div>
      </div>
    </Modal>
  );
}

function ClusterSummary({
  cluster,
  response,
  header,
  onSaved,
}: {
  cluster: ClusterDescription;
  response: RenderResponse;
  header: (key: string) => string;
  onSaved: () => void;
}) {
  const editor = useEditor();
  const [saving, setSaving] = useState(false);
  const table = response.table;
  // 存成群組：檢視的詳細程度只有一個文字維度時，以它的成員分組
  const dimensions = (table?.keys ?? []).filter((key, i) => table?.roles?.[i] === "dimension" && key !== cluster.column);
  const source = dimensions.length === 1 ? editor.fields.get(dimensions[0]) : undefined;
  const groupable = source !== undefined && source.datatype === "string" && table !== undefined;
  const saveGroup = async () => {
    if (!groupable || !table || !source) return;
    const at = table.keys.indexOf(source.id);
    const label = table.keys.indexOf(cluster.column);
    const groups = new Map<string, string[]>();
    table.raw.forEach((row, i) => {
      const name = table.rows[i]?.[label] ?? row[label];
      if (!name || row[at] === "" || row[at] === null) return;
      const members = groups.get(name) ?? [];
      if (!members.includes(row[at])) members.push(row[at]);
      groups.set(name, members);
    });
    setSaving(true);
    try {
      const field = await editApi.saveGroup(editor.workbook, editor.sheet.datasource, {
        source: source.id,
        caption: `${source.caption}（叢集）`,
        groups: [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })).map(([name, members]) => ({ name, members })),
        other: null,
        field: null,
      });
      onSaved();
      editor.notify(`已建立群組「${field.caption}」`, "info");
    } catch (e) {
      editor.notify((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="describe-cluster">
      <table className="describe-table">
        <caption>叢集的輸入與摘要</caption>
        <tbody>
          <tr>
            <th>變數</th>
            <td>{cluster.measures.map(header).join("、")}</td>
          </tr>
          <tr>
            <th>縮放</th>
            <td>正規化（最小值到最大值）</td>
          </tr>
          <tr>
            <th>叢集數</th>
            <td>
              {cluster.count}
              {cluster.automatic ? "（自動）" : ""}
            </td>
          </tr>
          <tr>
            <th>點數</th>
            <td>{cluster.points}</td>
          </tr>
          <tr>
            <th>未分到叢集的標記</th>
            <td>{cluster.unclustered}</td>
          </tr>
          <tr>
            <th>組間平方和</th>
            <td>{number(cluster.between)}</td>
          </tr>
          <tr>
            <th>組內平方和</th>
            <td>{number(cluster.within)}</td>
          </tr>
          <tr>
            <th>總平方和</th>
            <td>{number(cluster.total)}</td>
          </tr>
        </tbody>
      </table>
      <table className="describe-table wide">
        <caption>各叢集的中心</caption>
        <thead>
          <tr>
            <th>叢集</th>
            <th>項目數</th>
            {cluster.measures.map((m) => (
              <th key={m}>{header(m)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cluster.clusters.map((c) => (
            <tr key={c.cluster}>
              <td>Cluster {c.cluster}</td>
              <td>{c.items}</td>
              {c.centers.map((v, i) => (
                <td key={i}>{number(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <table className="describe-table wide">
        <caption>變異數分析</caption>
        <thead>
          <tr>
            <th>變數</th>
            <th>F 統計量</th>
            <th>P 值</th>
            <th>模型平方和</th>
            <th>誤差平方和</th>
          </tr>
        </thead>
        <tbody>
          {cluster.variables.map((v, i) => (
            <tr key={i}>
              <td>{header(cluster.measures[i])}</td>
              <td>{number(v.f)}</td>
              <td>{pValue(v.p)}</td>
              <td>{number(v.between)}</td>
              <td>{number(v.within)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button
          type="button"
          disabled={!groupable || saving}
          title={groupable ? "以目前的叢集建立群組欄位（之後資料改變時群組不變）" : "檢視的詳細程度只有一個文字維度時才能存成群組"}
          onClick={() => void saveGroup()}
        >
          存成群組
        </button>
      </div>
    </div>
  );
}
