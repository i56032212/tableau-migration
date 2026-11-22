// 自訂的圖片形狀（Tableau 的自訂形狀調色盤）：圖表的規格只記每個標記的圖片路徑與要套用的顏色
// （usermeta.shape_images 列出資料列中的欄位），這裡載入圖片、套用顏色，把結果（data URL）填進資料列，
// 圖片的圖層才畫得出來。圖片以 data URL 放進圖表：匯出圖片、PowerPoint 時不必另外載入。

import { fileUrl } from "./api";

type Spec = Record<string, any>;

/** 規格中用到圖片形狀的各標記卡在資料列中的欄位 */
export interface ShapeFields {
  image: string;
  tint: string;
  url: string;
}

/** 形狀的圖例（usermeta.shape_legend）：各成員是內建形狀的路徑（-1 到 1）或圖片 */
export interface ShapeLegend {
  title: string;
  /** color：色彩與形狀是同一個欄位時這個成員的顏色（合併的圖例） */
  entries: { label: unknown; path?: string; image?: string; color?: string }[];
}

/** 套用顏色時圖片的最大邊長（像素）：形狀通常只有幾十個像素，太大的圖片縮小後再處理 */
const MAX_SIDE = 256;

const loaded = new Map<string, Promise<string | null>>();
const tinted = new Map<string, Promise<string | null>>();

function load(url: string): Promise<string | null> {
  let found = loaded.get(url);
  if (!found) {
    found = fetch(url)
      .then((response) => (response.ok ? response.blob() : null))
      .then(
        (blob) =>
          blob &&
          new Promise<string | null>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(blob);
          }),
      )
      .catch(() => null);
    loaded.set(url, found);
    // 載入失敗的不留在快取（圖片可能稍後才加入）
    void found.then((value) => {
      if (value === null) loaded.delete(url);
    });
  }
  return found;
}

/** 圖片不透明的部分換成這個顏色（透明度不變），與 Tableau 把顏色套用到自訂形狀相同。 */
function tint(source: string, color: string): Promise<string | null> {
  const key = `${color}|${source}`;
  let found = tinted.get(key);
  if (!found) {
    found = new Promise<string | null>((resolve) => {
      const image = new Image();
      image.onload = () => {
        const width = image.naturalWidth || 64;
        const height = image.naturalHeight || 64;
        const scale = Math.min(1, MAX_SIDE / Math.max(width, height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        const context = canvas.getContext("2d");
        if (!context) {
          resolve(null);
          return;
        }
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        context.globalCompositeOperation = "source-in";
        context.fillStyle = color;
        context.fillRect(0, 0, canvas.width, canvas.height);
        try {
          resolve(canvas.toDataURL("image/png"));
        } catch {
          resolve(null);
        }
      };
      image.onerror = () => resolve(null);
      image.src = source;
    });
    tinted.set(key, found);
  }
  return found;
}

/** 背景圖片的圖層（usermeta.background_images）：圖層資料中的圖片路徑欄、網址欄與是否左右、上下翻轉 */
interface BackgroundFields {
  image: string;
  url: string;
  flip_x: boolean;
  flip_y: boolean;
}

const flipped = new Map<string, Promise<string | null>>();

/** 圖片左右（flipX）或上下（flipY）翻轉後的 data URL：背景圖片對應的值與座標軸的方向相反時（Tableau 畫出顛倒的圖片）。 */
function flip(source: string, flipX: boolean, flipY: boolean): Promise<string | null> {
  const key = `${flipX}|${flipY}|${source}`;
  let found = flipped.get(key);
  if (!found) {
    found = new Promise<string | null>((resolve) => {
      const image = new Image();
      image.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth || 1;
        canvas.height = image.naturalHeight || 1;
        const context = canvas.getContext("2d");
        if (!context) return resolve(null);
        context.translate(flipX ? canvas.width : 0, flipY ? canvas.height : 0);
        context.scale(flipX ? -1 : 1, flipY ? -1 : 1);
        context.drawImage(image, 0, 0);
        resolve(canvas.toDataURL("image/png"));
      };
      image.onerror = () => resolve(null);
      image.src = source;
    });
    flipped.set(key, found);
  }
  return found;
}

/** 載入背景圖片（地圖＞背景圖片），填進它的圖層的網址欄；載入不了的圖片不畫（與 Tableau 找不到圖片時相同）。 */
async function prepareBackgroundImages(spec: Spec, workbook: string): Promise<void> {
  const fields = spec.usermeta?.background_images as BackgroundFields[] | undefined;
  if (!fields || fields.length === 0) return;
  const jobs: Promise<void>[] = [];
  eachRecord(spec, (record) => {
    for (const field of fields) {
      const path = record[field.image];
      if (typeof path !== "string") continue;
      jobs.push(
        load(fileUrl(workbook, path)).then(async (source) => {
          const url = source && (field.flip_x || field.flip_y) ? await flip(source, field.flip_x, field.flip_y) : source;
          record[field.url] = url;
        }),
      );
    }
  });
  await Promise.all(jobs);
}

/** 報表中一張形狀圖片的 data URL（color 不是 null 時套用這個顏色）；載入不了時為 null。 */
export async function shapeImage(workbook: string, path: string, color: string | null = null): Promise<string | null> {
  const source = await load(fileUrl(workbook, path));
  if (source === null || color === null) return source;
  return (await tint(source, color)) ?? source;
}

function eachRecord(spec: Spec, visit: (record: Record<string, unknown>) => void) {
  const values = spec.data?.values;
  if (Array.isArray(values)) values.forEach((record) => record && typeof record === "object" && visit(record));
  for (const key of ["layer", "vconcat", "hconcat", "concat"]) {
    if (Array.isArray(spec[key])) spec[key].forEach((child: Spec) => eachRecord(child, visit));
  }
  if (spec.spec) eachRecord(spec.spec, visit);
}

/**
 * 載入規格中用到的形狀圖片，填進資料列的網址欄。圖片載入不了的標記把圖片的路徑清掉：照內建的形狀畫
 * （規格中圖片形狀的點是看不見的，沒有圖片時要讓它顯示）。
 */
export async function prepareShapeImages(spec: Spec, workbook: string): Promise<void> {
  await prepareBackgroundImages(spec, workbook);
  const fields = spec.usermeta?.shape_images as ShapeFields[] | undefined;
  if (!fields || fields.length === 0) return;
  const jobs: Promise<void>[] = [];
  eachRecord(spec, (record) => {
    for (const field of fields) {
      const path = record[field.image];
      if (typeof path !== "string") continue;
      const color = typeof record[field.tint] === "string" ? (record[field.tint] as string) : null;
      jobs.push(
        shapeImage(workbook, path, color).then((url) => {
          if (url === null) record[field.image] = null;
          else record[field.url] = url;
        }),
      );
    }
  });
  await Promise.all(jobs);
}
