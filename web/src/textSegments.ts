// 同一行內格式不同的文字（例如 24pt 的數字接著 16pt 的單位）：後端把這一行分成各段、每一段一個文字圖層
// （spec.usermeta.text_segments，見 kernel 的 line_layers），各段水平的位置在這裡決定——畫圖之前以畫布量出
// 各段的寬度，把每一段相對於這一行定位點的位移寫進資料列（Vega 排版時沒有量文字寬度的函式）。
// 字型要先載入（見 fonts.ts），量到的才是真正的寬度。

interface Segment {
  /** 這一段文字的欄位 */
  field: string;
  /** 位移的欄位（由這裡填入） */
  dx: string;
  /** 畫布的 font 字串，例如 700 32px "Roboto", … */
  font: string;
}

interface SegmentGroup {
  /** 這一行的對齊：定位點在這一行的左邊、中間或右邊 */
  align: "left" | "center" | "right";
  segments: Segment[];
}

interface SegmentedSpec {
  usermeta?: { text_segments?: SegmentGroup[] };
  data?: { values?: Record<string, unknown>[] };
}

let context: CanvasRenderingContext2D | null | undefined;

/** 各段依序排開：回傳每一段的位移（相對於定位點；靠左為 0 起、置中為負的一半起、靠右為負的全寬起）。 */
export function segmentOffsets(widths: number[], align: SegmentGroup["align"]): number[] {
  const total = widths.reduce((a, b) => a + b, 0);
  let x = align === "center" ? -total / 2 : align === "right" ? -total : 0;
  return widths.map((width) => {
    const at = Math.round(x * 100) / 100;
    x += width;
    return at;
  });
}

/** 把各段的位移寫進規格的資料列；沒有分段的文字或沒有畫布時不做事。 */
export function layoutTextSegments(spec: SegmentedSpec): void {
  const groups = spec.usermeta?.text_segments;
  const rows = spec.data?.values;
  if (!groups?.length || !rows?.length) return;
  if (context === undefined) context = document.createElement("canvas").getContext("2d");
  const ctx = context;
  if (!ctx) return;
  for (const row of rows) {
    for (const group of groups) {
      const widths = group.segments.map((segment) => {
        const text = row[segment.field];
        if (text === null || text === undefined || text === "") return 0;
        ctx.font = segment.font;
        return ctx.measureText(String(text)).width;
      });
      segmentOffsets(widths, group.align).forEach((offset, i) => {
        row[group.segments[i].dx] = offset;
      });
    }
  }
}
