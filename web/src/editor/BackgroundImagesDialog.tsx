// 背景圖片（Tableau 的「地圖＞背景圖片」）：工作表的資料源的圖片清單，勾選的圖片在這張工作表上顯示；
// 新增、編輯（圖片檔、X／Y 欄位與左右、上下的值、淡化、選項與顯示的條件）與移除。圖片存在資料源上
// （同一個資料源的工作表共用），各工作表可以停用（Worksheet.background_images_off）。

import { useState } from "react";
import { fileUrl } from "../api";
import { Modal } from "../Server";
import type { FieldUse, Scalar } from "../types";
import { editApi } from "./api";
import { useEditor } from "./context";
import type { BackgroundImageIR, CatalogField } from "./model";

const IMAGE_MAX_BYTES = 4 * 1024 * 1024;
const LONGITUDE = "longitude_generated";
const LATITUDE = "latitude_generated";

function plainUse(field: string): FieldUse {
  return { field, agg: null, date: null, continuous: false, table_calc: null };
}

/** 條件的值：依欄位的型別轉換輸入的文字 */
function typedValue(text: string, field: CatalogField | undefined): Scalar {
  if (!field) return text;
  if (field.datatype === "integer" || field.datatype === "real") {
    const number = Number(text);
    return text.trim() !== "" && Number.isFinite(number) ? number : text;
  }
  if (field.datatype === "boolean") return text.trim().toLowerCase() === "true";
  return text;
}

function emptyImage(x: string, y: string): BackgroundImageIR {
  return {
    name: "",
    image: "",
    x,
    left: 0,
    right: 100,
    y,
    top: 100,
    bottom: 0,
    washout: 0,
    lock_aspect: true,
    show_entire: false,
    conditions: [],
  };
}

export function BackgroundImagesDialog({ onClose }: { onClose: () => void }) {
  const editor = useEditor();
  const { sheet, catalog, workbook } = editor;
  const source = catalog.datasources.find((d) => d.id === sheet.datasource);
  const [images, setImages] = useState<BackgroundImageIR[]>(() => (source?.background_images ?? []).map((i) => ({ ...i })));
  const [off, setOff] = useState<string[]>(() => ((sheet.background_images_off as string[] | undefined) ?? []).slice());
  const [editing, setEditing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fields = source?.fields ?? [];
  const numbers = fields.filter((f) => f.datatype === "integer" || f.datatype === "real");
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const saved = await editApi.saveBackgroundImages(workbook, sheet.datasource, images);
      const kept = new Set(saved.images.map((i) => i.id));
      const disabled = off.filter((id) => kept.has(id));
      editor.reloadCatalog();
      // 圖片存在資料源上：重新讀取工作表（刪除的圖片已從停用的清單拿掉）並重新畫圖，再存這張工作表的勾選
      editor.reloadSheet();
      const before = (sheet.background_images_off as string[] | undefined) ?? [];
      if (disabled.join("\n") !== before.filter((id) => kept.has(id)).join("\n"))
        editor.apply((s) => ({ ...s, background_images_off: disabled }));
      onClose();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  if (editing !== null) {
    return (
      <ImageForm
        workbook={workbook}
        fields={fields}
        numbers={numbers}
        image={images[editing]}
        onCancel={() => {
          // 新增後取消：拿掉空白的那一張
          if (!images[editing].image) setImages(images.filter((_, i) => i !== editing));
          setEditing(null);
        }}
        onDone={(image) => {
          setImages(images.map((item, i) => (i === editing ? image : item)));
          setEditing(null);
        }}
      />
    );
  }
  return (
    <Modal title={`背景圖片：${source?.caption ?? sheet.datasource}`} onClose={onClose}>
      <div className="background-images-dialog">
        {images.length === 0 && <p className="hint">還沒有背景圖片。圖片畫在標記之下，欄架上有 X 欄位、列架上有 Y 欄位時顯示。</p>}
        <ul className="background-image-list">
          {images.map((image, i) => {
            const shown = !image.id || !off.includes(image.id);
            return (
              <li key={image.id ?? `new-${i}`} data-image={image.name}>
                <label className="inline">
                  <input
                    type="checkbox"
                    aria-label={`在這張工作表上顯示「${image.name}」`}
                    checked={shown}
                    disabled={!image.id}
                    onChange={(e) =>
                      image.id && setOff(e.target.checked ? off.filter((id) => id !== image.id) : [...off, image.id])
                    }
                  />{" "}
                  {image.name}
                </label>
                <span className="hint">{image.image}</span>
                <button type="button" className="link" onClick={() => setEditing(i)}>
                  編輯…
                </button>
                <button type="button" className="link" onClick={() => setImages(images.filter((_, k) => k !== i))}>
                  移除
                </button>
              </li>
            );
          })}
        </ul>
        <button
          type="button"
          disabled={numbers.length === 0}
          title={numbers.length === 0 ? "資料源中沒有數值欄位" : undefined}
          onClick={() => {
            const x = numbers[0]?.id ?? LONGITUDE;
            const y = numbers[1]?.id ?? numbers[0]?.id ?? LATITUDE;
            setImages([...images, emptyImage(x, y)]);
            setEditing(images.length);
          }}
        >
          新增圖片…
        </button>
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={busy} onClick={() => void save()}>
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

function ImageForm({
  workbook,
  fields,
  numbers,
  image,
  onCancel,
  onDone,
}: {
  workbook: string;
  fields: CatalogField[];
  numbers: CatalogField[];
  image: BackgroundImageIR;
  onCancel: () => void;
  onDone: (image: BackgroundImageIR) => void;
}) {
  const [draft, setDraft] = useState<BackgroundImageIR>(image);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const change = (patch: Partial<BackgroundImageIR>) => setDraft({ ...draft, ...patch });
  const dimensions = fields.filter((f) => f.role === "dimension" && f.kind !== "set");
  const axisChoices = [
    ...numbers.map((f) => [f.id, f.caption] as const),
    [LONGITUDE, "經度（產生）"] as const,
    [LATITUDE, "緯度（產生）"] as const,
  ];
  const number = (label: string, key: "left" | "right" | "top" | "bottom") => (
    <label className="inline">
      {label}
      <input
        type="number"
        aria-label={label}
        value={draft[key]}
        onChange={(e) => change({ [key]: Number(e.target.value) } as Partial<BackgroundImageIR>)}
      />
    </label>
  );
  const done = async () => {
    setError(null);
    if (!draft.name.trim()) return setError("請輸入名稱");
    let path = draft.image;
    if (file) {
      if (file.size > IMAGE_MAX_BYTES) return setError(`${file.name} 超過 ${IMAGE_MAX_BYTES / 1024 / 1024} MB`);
      setBusy(true);
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        path = (await editApi.addImage(workbook, file.name, btoa(binary))).path;
      } catch (e) {
        setBusy(false);
        return setError(`無法加入圖片：${(e as Error).message}`);
      }
      setBusy(false);
    }
    if (!path) return setError("請選擇圖片檔");
    if (draft.left === draft.right || draft.top === draft.bottom) return setError("左右（上下）的值不能相同");
    onDone({ ...draft, name: draft.name.trim(), image: path });
  };
  return (
    <Modal title="編輯背景圖片" onClose={onCancel}>
      <div className="background-image-form">
        <label className="inline">
          名稱
          <input aria-label="背景圖片的名稱" value={draft.name} onChange={(e) => change({ name: e.target.value })} />
        </label>
        <div className="row">
          <label className="inline">
            圖片檔
            <input
              type="file"
              aria-label="背景圖片的檔案"
              accept="image/png,image/jpeg,image/gif,image/bmp,image/webp,image/svg+xml"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          {!file && draft.image && (
            <span className="hint">
              目前：{draft.image}
              <img
                className="background-image-preview"
                src={fileUrl(workbook, draft.image)}
                alt=""
                style={{ display: "block", maxWidth: 160, maxHeight: 80 }}
              />
            </span>
          )}
        </div>
        <fieldset>
          <legend>X 欄位</legend>
          <select aria-label="X 欄位" value={draft.x} onChange={(e) => change({ x: e.target.value })}>
            {axisChoices.map(([id, caption]) => (
              <option key={id} value={id}>
                {caption}
              </option>
            ))}
          </select>
          {number("左", "left")}
          {number("右", "right")}
        </fieldset>
        <fieldset>
          <legend>Y 欄位</legend>
          <select aria-label="Y 欄位" value={draft.y} onChange={(e) => change({ y: e.target.value })}>
            {axisChoices.map(([id, caption]) => (
              <option key={id} value={id}>
                {caption}
              </option>
            ))}
          </select>
          {number("上", "top")}
          {number("下", "bottom")}
        </fieldset>
        <label className="inline" title="0% 為原色，100% 為全白">
          淡化
          <input
            type="range"
            aria-label="淡化"
            min={0}
            max={100}
            value={Math.round(draft.washout * 100)}
            onChange={(e) => change({ washout: Number(e.target.value) / 100 })}
          />
          {Math.round(draft.washout * 100)}%
        </label>
        <fieldset>
          <legend>選項</legend>
          <label className="inline">
            <input type="checkbox" checked={draft.lock_aspect} onChange={(e) => change({ lock_aspect: e.target.checked })} /> 鎖定外觀比例
          </label>
          <label className="inline">
            <input type="checkbox" checked={draft.show_entire} onChange={(e) => change({ show_entire: e.target.checked })} /> 一律顯示整張圖片
          </label>
        </fieldset>
        <fieldset>
          <legend>只在下列條件成立時顯示圖片</legend>
          {draft.conditions.map((condition, i) => (
            <div className="row" key={i} data-condition={i}>
              <select
                aria-label={`條件 ${i + 1} 的欄位`}
                value={condition.field.field}
                onChange={(e) =>
                  change({
                    conditions: draft.conditions.map((c, k) => (k === i ? { field: plainUse(e.target.value), value: c.value } : c)),
                  })
                }
              >
                {dimensions.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.caption}
                  </option>
                ))}
              </select>
              ＝
              <input
                aria-label={`條件 ${i + 1} 的值`}
                value={condition.value === null ? "" : String(condition.value)}
                onChange={(e) =>
                  change({
                    conditions: draft.conditions.map((c, k) =>
                      k === i ? { ...c, value: typedValue(e.target.value, fields.find((f) => f.id === c.field.field)) } : c,
                    ),
                  })
                }
              />
              <button type="button" className="link" onClick={() => change({ conditions: draft.conditions.filter((_, k) => k !== i) })}>
                移除
              </button>
            </div>
          ))}
          <button
            type="button"
            className="link"
            disabled={dimensions.length === 0}
            onClick={() => change({ conditions: [...draft.conditions, { field: plainUse(dimensions[0].id), value: "" }] })}
          >
            新增條件
          </button>
        </fieldset>
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={busy} onClick={() => void done()}>
            確定
          </button>
          <button type="button" onClick={onCancel}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
