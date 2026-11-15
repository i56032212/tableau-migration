// 形狀的選擇（Tableau 的「編輯形狀」）：預設調色盤的 10 種形狀，與自訂的圖片形狀（Tableau 的自訂形狀
// 調色盤：My Tableau Repository\Shapes\<調色盤> 資料夾中的圖片）。圖片存在報表裡（files/Shapes/<調色盤>/），
// 存檔、發布時跟著報表；從 Tableau 開啟的報表中存在活頁簿裡的圖片也列在這裡。

import { useEffect, useRef, useState } from "react";
import { fileUrl } from "../api";
import { editApi } from "./api";

export const SHAPES: [string, string][] = [
  ["circle", "● 圓形"],
  ["square", "■ 方形"],
  ["plus", "✚ 加號"],
  ["times", "✖ 叉號"],
  ["asterisk", "★ 星號"],
  ["diamond", "◆ 菱形"],
  ["triangle", "▲ 三角形"],
  ["down_triangle", "▼ 倒三角形"],
  ["left_triangle", "◀ 向左三角形"],
  ["right_triangle", "▶ 向右三角形"],
];

/** 一個形狀：內建的形狀，或自訂的圖片（報表 files/ 下的路徑；shape 是找不到圖片時的替代） */
export interface ShapeChoice {
  shape: string;
  image?: string | null;
}

export interface ShapePalette {
  name: string;
  images: string[];
}

/** 一次加入的圖片大小上限（與伺服器相同） */
const MAX_BYTES = 4 * 1024 * 1024;

const fileName = (path: string) => path.split("/").pop() ?? path;

/** 報表中的自訂形狀調色盤；reload 重新讀取。 */
export function useShapePalettes(workbook: string): { palettes: ShapePalette[]; setPalettes: (palettes: ShapePalette[]) => void } {
  const [palettes, setPalettes] = useState<ShapePalette[]>([]);
  useEffect(() => {
    let cancelled = false;
    editApi
      .shapes(workbook)
      .then((result) => {
        if (!cancelled) setPalettes(result.palettes);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [workbook]);
  return { palettes, setPalettes };
}

const choiceValue = (choice: ShapeChoice | null | undefined) => (!choice ? "" : choice.image ? `i:${choice.image}` : `s:${choice.shape}`);

/** 一個資料項目（或所有標記）的形狀：內建的形狀或調色盤中的圖片；auto 是不指定時的說明。 */
export function ShapeSelect({
  workbook,
  label,
  ariaLabel,
  value,
  auto,
  palettes,
  onChange,
}: {
  workbook: string;
  label: string;
  ariaLabel: string;
  value: ShapeChoice | null | undefined;
  auto: string;
  palettes: ShapePalette[];
  onChange: (choice: ShapeChoice | null) => void;
}) {
  const current = choiceValue(value);
  // 指定的圖片不在任何調色盤中（例如圖片已經不在報表裡）：仍列出來，才看得出目前的設定
  const known = palettes.some((p) => value?.image && p.images.includes(value.image));
  return (
    <label className="inline shape-select">
      <span className="shape-item">{label}</span>
      <select
        aria-label={ariaLabel}
        value={current}
        onChange={(e) => {
          const chosen = e.target.value;
          if (!chosen) onChange(null);
          else if (chosen.startsWith("i:")) onChange({ shape: value?.shape ?? "circle", image: chosen.slice(2) });
          else onChange({ shape: chosen.slice(2), image: null });
        }}
      >
        <option value="">{auto}</option>
        <optgroup label="預設">
          {SHAPES.map(([shape, text]) => (
            <option key={shape} value={`s:${shape}`}>
              {text}
            </option>
          ))}
        </optgroup>
        {palettes.map((palette) => (
          <optgroup key={palette.name} label={palette.name || "（未分類）"}>
            {palette.images.map((image) => (
              <option key={image} value={`i:${image}`}>
                {fileName(image)}
              </option>
            ))}
          </optgroup>
        ))}
        {value?.image && !known && <option value={`i:${value.image}`}>{fileName(value.image)}（找不到圖片）</option>}
      </select>
      {value?.image && <img className="shape-thumb" src={fileUrl(workbook, value.image)} alt="" />}
    </label>
  );
}

/**
 * 自訂的形狀調色盤：預覽調色盤中的圖片、加入圖片（存進報表）、把調色盤依序指定給各成員
 * （Tableau 的「指定調色盤」）。onAssign 省略時沒有「指定調色盤」（只有一個資料項目）。
 */
export function ShapePalettes({
  workbook,
  palettes,
  onPalettes,
  onAssign,
}: {
  workbook: string;
  palettes: ShapePalette[];
  onPalettes: (palettes: ShapePalette[]) => void;
  onAssign?: (images: string[]) => void;
}) {
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const shown = palettes.find((p) => p.name === selected) ?? palettes[0];
  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const palette = name.trim() || shown?.name || "自訂";
    setBusy(true);
    setError(null);
    try {
      const items: { name: string; content: string }[] = [];
      for (const file of Array.from(files)) {
        if (file.size > MAX_BYTES) throw new Error(`${file.name} 超過 ${MAX_BYTES / 1024 / 1024} MB`);
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        items.push({ name: file.name, content: btoa(binary) });
      }
      const result = await editApi.addShapes(workbook, palette, items);
      onPalettes(result.palettes);
      setSelected(palette);
      setName("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };
  return (
    <fieldset className="shape-palettes">
      <legend>自訂的形狀調色盤</legend>
      {palettes.length === 0 && <p className="hint">還沒有自訂的形狀。加入圖片（PNG、JPG、GIF、BMP、WebP、SVG）後，可以把圖片當作標記的形狀；建議用背景透明、約 32×32 像素的圖片。</p>}
      {palettes.length > 0 && (
        <div className="row">
          <label className="inline">
            調色盤
            <select aria-label="形狀調色盤" value={shown?.name ?? ""} onChange={(e) => setSelected(e.target.value)}>
              {palettes.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name || "（未分類）"}
                </option>
              ))}
            </select>
          </label>
          {onAssign && shown && (
            <button type="button" title="把這個調色盤的圖片依序指定給各成員（Tableau 的「指定調色盤」）" onClick={() => onAssign(shown.images)}>
              指定調色盤
            </button>
          )}
        </div>
      )}
      {shown && (
        <div className="shape-grid" aria-label={`調色盤 ${shown.name} 的圖片`}>
          {shown.images.map((image) => (
            <img key={image} src={fileUrl(workbook, image)} alt={fileName(image)} title={fileName(image)} />
          ))}
        </div>
      )}
      <div className="row">
        <label className="inline" title="圖片加入這個調色盤（Tableau 的 Shapes 資料夾下的子資料夾）；留空白時加入上面選取的調色盤">
          加入到
          <input aria-label="新調色盤的名稱" value={name} placeholder={shown?.name || "自訂"} onChange={(e) => setName(e.target.value)} />
        </label>
        <button type="button" disabled={busy} onClick={() => input.current?.click()}>
          {busy ? "加入中…" : "加入圖片…"}
        </button>
        <input
          ref={input}
          type="file"
          aria-label="形狀的圖片檔"
          accept=".png,.jpg,.jpeg,.gif,.bmp,.webp,.svg"
          multiple
          hidden
          onChange={(e) => void upload(e.target.files)}
        />
      </div>
      {error && <p className="error">{error}</p>}
    </fieldset>
  );
}
