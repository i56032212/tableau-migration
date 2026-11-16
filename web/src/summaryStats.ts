// 摘要卡的統計量（Tableau 的「工作表＞顯示摘要」）。
//
// 預設顯示的：總和、平均、最小值、最大值、中位數；選用的：標準差、第一與第三四分位數、偏度、超額峰度。
// Tableau 的文件只說明標準差是樣本標準差（含修正），其餘沒有列出公式，以下是推定的：
// - 四分位數：第 25、75 百分位數，在排序後的值之間線性內插（與中位數取中間兩個值的平均一致）
// - 偏度、超額峰度：樣本的公式（與 Excel 的 SKEW、KURT 相同），與標準差用樣本的公式一致

export interface SummaryStats {
  count: number;
  sum: number | null;
  average: number | null;
  minimum: number | null;
  maximum: number | null;
  median: number | null;
  /** 樣本標準差（n − 1）；少於 2 個值時沒有 */
  deviation: number | null;
  firstQuartile: number | null;
  thirdQuartile: number | null;
  /** 樣本偏度；少於 3 個值或沒有變異時沒有 */
  skewness: number | null;
  /** 樣本超額峰度；少於 4 個值或沒有變異時沒有 */
  kurtosis: number | null;
}

/** 排序後的值的百分位數（p 為 0 到 1）：位置 (n − 1)·p，在前後兩個值之間線性內插。 */
export function percentile(sorted: number[], p: number): number | null {
  const n = sorted.length;
  if (n === 0) return null;
  const at = (n - 1) * p;
  const low = Math.floor(at);
  const high = Math.ceil(at);
  return sorted[low] + (sorted[high] - sorted[low]) * (at - low);
}

export function summarize(values: number[]): SummaryStats {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  const n = sorted.length;
  const empty: SummaryStats = {
    count: n,
    sum: null,
    average: null,
    minimum: null,
    maximum: null,
    median: null,
    deviation: null,
    firstQuartile: null,
    thirdQuartile: null,
    skewness: null,
    kurtosis: null,
  };
  if (n === 0) return empty;
  const sum = sorted.reduce((a, b) => a + b, 0);
  const mean = sum / n;
  const squares = sorted.reduce((a, b) => a + (b - mean) ** 2, 0);
  const deviation = n > 1 ? Math.sqrt(squares / (n - 1)) : null;
  let skewness: number | null = null;
  let kurtosis: number | null = null;
  if (deviation !== null && deviation > 0) {
    const cubes = sorted.reduce((a, b) => a + ((b - mean) / deviation) ** 3, 0);
    const fourths = sorted.reduce((a, b) => a + ((b - mean) / deviation) ** 4, 0);
    if (n > 2) skewness = (n / ((n - 1) * (n - 2))) * cubes;
    if (n > 3) kurtosis = ((n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))) * fourths - (3 * (n - 1) ** 2) / ((n - 2) * (n - 3));
  }
  return {
    count: n,
    sum,
    average: mean,
    minimum: sorted[0],
    maximum: sorted[n - 1],
    median: percentile(sorted, 0.5),
    deviation,
    firstQuartile: percentile(sorted, 0.25),
    thirdQuartile: percentile(sorted, 0.75),
    skewness,
    kurtosis,
  };
}
