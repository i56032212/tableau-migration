// 匯出（Tableau 的「匯出影像」「匯出為 PowerPoint」與 dashboard 的匯出按鈕）：把畫面上的 dashboard、工作表或
// Story 畫成 PNG（複製 DOM、內嵌計算後的樣式與圖片，放進 SVG 的 foreignObject 後畫到 canvas），PowerPoint 是
// 一張投影片放這張圖。不需要外部套件：PPTX 以不壓縮的 zip 寫出。
// 在區塊內捲動的工作表：單獨檢視的工作表匯出整張（與列印相同，看不到的列、欄也畫出來；推定）；dashboard 與 Story
// 照畫面上的樣子（Tableau 的 dashboard 匯出的是看得到的部分），各區塊捲到哪裡就畫哪一段、捲動時固定的標題照畫面上
// 的位置。捲軸不畫。

/** 內嵌樣式時略過的屬性（與版面無關或會讓輸出變大）。 */
const SKIP = new Set(["transition", "animation", "cursor", "will-change", "pointer-events"]);

function inlineStyles(source: Element, target: Element) {
  if (source instanceof HTMLElement && target instanceof HTMLElement) {
    const computed = getComputedStyle(source);
    let css = "";
    for (let i = 0; i < computed.length; i++) {
      const name = computed[i];
      if (SKIP.has(name) || name.startsWith("transition") || name.startsWith("animation")) continue;
      css += `${name}:${computed.getPropertyValue(name)};`;
    }
    target.setAttribute("style", css);
  } else if (source instanceof SVGElement && target instanceof SVGElement && source.tagName.toLowerCase() === "svg") {
    // vega 的 SVG 以屬性設定樣式：只補上字型
    const computed = getComputedStyle(source);
    target.setAttribute("style", `${target.getAttribute("style") ?? ""};font-family:${computed.fontFamily}`);
  }
  const sourceChildren = source.children;
  const targetChildren = target.children;
  for (let i = 0; i < sourceChildren.length && i < targetChildren.length; i++) inlineStyles(sourceChildren[i], targetChildren[i]);
}

async function dataUrl(url: string): Promise<string | null> {
  try {
    const response = await fetch(url);
    if (!response.ok) return null;
    const blob = await response.blob();
    return await new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(blob);
    });
  } catch {
    return null;
  }
}

/** canvas 的上限（Chromium 單邊 32767、面積約 2.68 億像素；留一些餘裕） */
const CANVAS_SIDE = 16384;
const CANVAS_AREA = 2 ** 27;

/**
 * 複本裡在區塊內捲動的圖（.chart）：whole 時展開成整張圖的大小（圖的區塊到 element 之間的每一層都加上超出的寬高；
 * 固定標題的複本之後拿掉），回傳展開了多少。不是 whole 時照畫面上捲到的位置：圖往左上移、固定標題的複本放在
 * 畫面上的位置。兩者都不畫捲軸。
 */
function settleScrolling(element: HTMLElement, clone: HTMLElement, whole: boolean): [number, number] {
  const charts = Array.from(element.querySelectorAll<HTMLElement>(".chart"));
  const copies = Array.from(clone.querySelectorAll<HTMLElement>(".chart"));
  let grown: [number, number] = [0, 0];
  charts.forEach((chart, i) => {
    const copy = copies[i];
    if (!copy) return;
    copy.style.overflow = "hidden";
    const extraX = chart.scrollWidth - chart.clientWidth;
    const extraY = chart.scrollHeight - chart.clientHeight;
    if (extraX <= 1 && extraY <= 1) return;
    if (whole && grown[0] === 0 && grown[1] === 0) {
      // 整張：從圖的區塊往上到 element，每一層照原本的大小加上超出的部分（內嵌的樣式是算好的像素）
      const dx = Math.max(0, extraX);
      const dy = Math.max(0, extraY);
      let source: HTMLElement | null = chart;
      let target: HTMLElement | null = copy;
      while (source && target) {
        if (dx > 1) Object.assign(target.style, { width: `${source.offsetWidth + dx}px`, maxWidth: "none", flex: "0 0 auto" });
        if (dy > 1) Object.assign(target.style, { height: `${source.offsetHeight + dy}px`, maxHeight: "none", flex: "0 0 auto" });
        if (source === element) break;
        source = source.parentElement;
        target = target.parentElement;
      }
      grown = [dx > 1 ? dx : 0, dy > 1 ? dy : 0];
      return;
    }
    if (chart.scrollLeft <= 0 && chart.scrollTop <= 0) return;
    // 畫面上的樣子：圖（或包著它的 .frozen-host）往左上移，固定標題的複本放到畫面上的位置（不再靠 sticky）
    const area = chart.getBoundingClientRect();
    const left = area.left + chart.clientLeft;
    const top = area.top + chart.clientTop;
    const layers = Array.from(chart.querySelectorAll<HTMLElement>(".frozen"));
    const layerCopies = Array.from(copy.querySelectorAll<HTMLElement>(".frozen"));
    copy.style.position = "relative";
    layers.forEach((layer, j) => {
      const twin = layerCopies[j];
      if (!twin) return;
      const box = layer.getBoundingClientRect();
      Object.assign(twin.style, { position: "absolute", left: `${box.left - left}px`, top: `${box.top - top}px`, margin: "0" });
      twin.classList.replace("frozen", "frozen-shown");
      copy.appendChild(twin);
    });
    const content = copy.firstElementChild as HTMLElement | null;
    if (content) Object.assign(content.style, { marginLeft: `${-chart.scrollLeft}px`, marginTop: `${-chart.scrollTop}px` });
  });
  return grown;
}

/**
 * 畫面上的一個區塊 → PNG。scale 為解析度的倍數（太大時縮小到 canvas 放得下）。whole：區塊裡在捲動的圖整張畫出來
 * （單獨檢視的工作表）；否則照畫面上的樣子（dashboard、Story）。
 */
export async function elementToPng(
  element: HTMLElement,
  scale = 2,
  whole = element.matches(".single-sheet"),
): Promise<{ blob: Blob; width: number; height: number }> {
  const clone = element.cloneNode(true) as HTMLElement;
  inlineStyles(element, clone);
  const [grownX, grownY] = settleScrolling(element, clone, whole);
  // 含框線的大小（scrollWidth 不含框線）；內容超出時取內容的大小
  const width = Math.ceil(Math.max(element.scrollWidth, element.offsetWidth) + grownX);
  const height = Math.ceil(Math.max(element.scrollHeight, element.offsetHeight) + grownY);
  scale = Math.min(scale, CANVAS_SIDE / width, CANVAS_SIDE / height, Math.sqrt(CANVAS_AREA / (width * height)));
  // 捲動時固定標題的複本（照畫面的樣子畫的除外）、浮動的選單與提示不畫
  // 檢視器的介面（列數、資料切換、選取的提示）也不畫
  clone.querySelectorAll(".frozen, .worksheet-overlay, .worksheet-meta, .action-menu, .popup-menu, .view-updating").forEach((e) => e.remove());
  // 圖片（.twbx 內的圖片、圖片按鈕）換成 data URL：SVG 中的圖片不會另外載入
  const images = Array.from(clone.querySelectorAll("img"));
  await Promise.all(
    images.map(async (img) => {
      const src = img.getAttribute("src");
      if (!src || src.startsWith("data:")) return;
      const inline = await dataUrl(new URL(src, window.location.href).toString());
      if (inline) img.setAttribute("src", inline);
      else img.remove();
    }),
  );
  clone.style.margin = "0";
  const xhtml = new XMLSerializer().serializeToString(clone);
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">` +
    `<foreignObject x="0" y="0" width="100%" height="100%">${xhtml}</foreignObject></svg>`;
  const image = new Image();
  image.decoding = "sync";
  const loaded = new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("無法產生圖片"));
  });
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  await loaded;
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.floor(width * scale));
  canvas.height = Math.max(1, Math.floor(height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("瀏覽器不支援 canvas");
  context.fillStyle = getComputedStyle(element).backgroundColor || "#ffffff";
  if (context.fillStyle === "rgba(0, 0, 0, 0)") context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.scale(scale, scale);
  context.drawImage(image, 0, 0);
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("無法產生圖片");
  return { blob, width, height };
}

// ---------------------------------------------------------------- zip（不壓縮）

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < data.length; i++) crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** 檔案 → zip（stored，不壓縮）。 */
export function zip(files: { name: string; data: Uint8Array }[]): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const file of files) {
    const name = encoder.encode(file.name);
    const crc = crc32(file.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true); // 解壓所需的版本
    local.setUint16(6, 0x0800, true); // 檔名為 UTF-8
    local.setUint16(8, 0, true); // stored
    local.setUint16(10, 0, true);
    local.setUint16(12, 0x21, true); // 1980-01-01
    local.setUint32(14, crc, true);
    local.setUint32(18, file.data.length, true);
    local.setUint32(22, file.data.length, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true);
    chunks.push(new Uint8Array(local.buffer), name, file.data);
    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true);
    entry.setUint16(4, 20, true);
    entry.setUint16(6, 20, true);
    entry.setUint16(8, 0x0800, true);
    entry.setUint16(10, 0, true);
    entry.setUint16(12, 0, true);
    entry.setUint16(14, 0x21, true);
    entry.setUint32(16, crc, true);
    entry.setUint32(20, file.data.length, true);
    entry.setUint32(24, file.data.length, true);
    entry.setUint16(28, name.length, true);
    entry.setUint32(42, offset, true);
    central.push(new Uint8Array(entry.buffer), name);
    offset += 30 + name.length + file.data.length;
  }
  const centralSize = central.reduce((sum, c) => sum + c.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  const parts = [...chunks, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(parts.reduce((sum, p) => sum + p.length, 0));
  let position = 0;
  for (const part of parts) {
    out.set(part, position);
    position += part.length;
  }
  return out;
}

// ---------------------------------------------------------------- PowerPoint

const NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main";
const NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main";
const NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';

const escapeXml = (text: string) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const THEME = `${XML}<a:theme xmlns:a="${NS_A}" name="Office"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4E79A7"/></a:accent1><a:accent2><a:srgbClr val="F28E2B"/></a:accent2><a:accent3><a:srgbClr val="E15759"/></a:accent3><a:accent4><a:srgbClr val="76B7B2"/></a:accent4><a:accent5><a:srgbClr val="59A14F"/></a:accent5><a:accent6><a:srgbClr val="EDC948"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>`;

const GROUP = `<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>`;

/** PNG → 一張投影片的 PowerPoint（16:9，圖片依比例置中）；title 為投影片的名稱。 */
export function pngToPptx(png: Uint8Array, width: number, height: number, title: string): Uint8Array<ArrayBuffer> {
  const slideW = 12192000;
  const slideH = 6858000;
  const ratio = Math.min(slideW / width, slideH / height);
  const cx = Math.round(width * ratio);
  const cy = Math.round(height * ratio);
  const x = Math.round((slideW - cx) / 2);
  const y = Math.round((slideH - cy) / 2);
  const encoder = new TextEncoder();
  const text = (s: string) => encoder.encode(s);
  const files: { name: string; data: Uint8Array }[] = [
    {
      name: "[Content_Types].xml",
      data: text(
        `${XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
          `<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
          `<Default Extension="xml" ContentType="application/xml"/>` +
          `<Default Extension="png" ContentType="image/png"/>` +
          `<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>` +
          `<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>` +
          `<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>` +
          `<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>` +
          `<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>` +
          `<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>` +
          `</Types>`,
      ),
    },
    {
      name: "_rels/.rels",
      data: text(
        `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
          `<Relationship Id="rId1" Type="${REL}/officeDocument" Target="ppt/presentation.xml"/>` +
          `<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>` +
          `</Relationships>`,
      ),
    },
    {
      name: "docProps/core.xml",
      data: text(
        `${XML}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">` +
          `<dc:title>${escapeXml(title)}</dc:title></cp:coreProperties>`,
      ),
    },
    {
      name: "ppt/presentation.xml",
      data: text(
        `${XML}<p:presentation xmlns:a="${NS_A}" xmlns:r="${NS_R}" xmlns:p="${NS_P}">` +
          `<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>` +
          `<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>` +
          `<p:sldSz cx="${slideW}" cy="${slideH}"/><p:notesSz cx="6858000" cy="9144000"/>` +
          `</p:presentation>`,
      ),
    },
    {
      name: "ppt/_rels/presentation.xml.rels",
      data: text(
        `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
          `<Relationship Id="rId1" Type="${REL}/slideMaster" Target="slideMasters/slideMaster1.xml"/>` +
          `<Relationship Id="rId2" Type="${REL}/slide" Target="slides/slide1.xml"/>` +
          `<Relationship Id="rId3" Type="${REL}/theme" Target="theme/theme1.xml"/>` +
          `</Relationships>`,
      ),
    },
    {
      name: "ppt/slideMasters/slideMaster1.xml",
      data: text(
        `${XML}<p:sldMaster xmlns:a="${NS_A}" xmlns:r="${NS_R}" xmlns:p="${NS_P}">` +
          `<p:cSld><p:spTree>${GROUP}</p:spTree></p:cSld>` +
          `<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>` +
          `<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>` +
          `</p:sldMaster>`,
      ),
    },
    {
      name: "ppt/slideMasters/_rels/slideMaster1.xml.rels",
      data: text(
        `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
          `<Relationship Id="rId1" Type="${REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>` +
          `<Relationship Id="rId2" Type="${REL}/theme" Target="../theme/theme1.xml"/>` +
          `</Relationships>`,
      ),
    },
    {
      name: "ppt/slideLayouts/slideLayout1.xml",
      data: text(
        `${XML}<p:sldLayout xmlns:a="${NS_A}" xmlns:r="${NS_R}" xmlns:p="${NS_P}" type="blank" preserve="1">` +
          `<p:cSld name="Blank"><p:spTree>${GROUP}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>` +
          `</p:sldLayout>`,
      ),
    },
    {
      name: "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
      data: text(
        `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
          `<Relationship Id="rId1" Type="${REL}/slideMaster" Target="../slideMasters/slideMaster1.xml"/>` +
          `</Relationships>`,
      ),
    },
    {
      name: "ppt/slides/slide1.xml",
      data: text(
        `${XML}<p:sld xmlns:a="${NS_A}" xmlns:r="${NS_R}" xmlns:p="${NS_P}">` +
          `<p:cSld name="${escapeXml(title)}"><p:spTree>${GROUP}` +
          `<p:pic><p:nvPicPr><p:cNvPr id="2" name="${escapeXml(title)}" descr="${escapeXml(title)}"/>` +
          `<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>` +
          `<p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>` +
          `<p:spPr><a:xfrm><a:off x="${x}" y="${y}"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>` +
          `</p:pic></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>`,
      ),
    },
    {
      name: "ppt/slides/_rels/slide1.xml.rels",
      data: text(
        `${XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
          `<Relationship Id="rId1" Type="${REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>` +
          `<Relationship Id="rId2" Type="${REL}/image" Target="../media/image1.png"/>` +
          `</Relationships>`,
      ),
    },
    { name: "ppt/theme/theme1.xml", data: text(THEME) },
    { name: "ppt/media/image1.png", data: png },
  ];
  return zip(files);
}

// ---------------------------------------------------------------- 存檔

/** 存檔：桌面版問位置後由本機引擎寫入；瀏覽器以下載儲存。 */
export async function saveFile(name: string, data: Blob, desktop: boolean): Promise<string | null> {
  if (desktop) {
    const { pickSavePath } = await import("./desktop");
    const extension = name.slice(name.lastIndexOf(".") + 1);
    const labels: Record<string, string> = {
      png: "PNG 圖片",
      pptx: "PowerPoint 簡報",
      pdf: "PDF",
      csv: "CSV",
      xlsx: "Excel 活頁簿",
    };
    const label = labels[extension] ?? extension.toUpperCase();
    let target = await pickSavePath(name, { title: "匯出", extension, label });
    if (!target) return null;
    if (!target.toLowerCase().endsWith(`.${extension}`)) target += `.${extension}`;
    const buffer = new Uint8Array(await data.arrayBuffer());
    let binary = "";
    for (let i = 0; i < buffer.length; i += 0x8000) binary += String.fromCharCode(...buffer.subarray(i, i + 0x8000));
    const { request } = await import("./api");
    const result = await request<{ path: string }>("/api/desktop/export", {
      method: "POST",
      body: JSON.stringify({ path: target, content: btoa(binary) }),
    });
    return result.path;
  }
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return name;
}

/** 匯出畫面上的 dashboard、工作表或 Story：PNG 或 PowerPoint。 */
export async function exportView(element: HTMLElement, kind: "image" | "powerpoint", title: string, desktop: boolean): Promise<string | null> {
  const { blob, width, height } = await elementToPng(element);
  const safe = title.replace(/[\\/:*?"<>|]+/g, "_") || "export";
  if (kind === "image") return saveFile(`${safe}.png`, blob, desktop);
  const pptx = pngToPptx(new Uint8Array(await blob.arrayBuffer()), width, height, title);
  return saveFile(`${safe}.pptx`, new Blob([pptx], { type: "application/vnd.openxmlformats-officedocument.presentationml.presentation" }), desktop);
}
