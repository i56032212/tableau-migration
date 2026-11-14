// 格式文字的編輯器（Tableau 的標題、工具提示、標籤、註解與文字物件的編輯視窗）：文字、欄位值與參數值依序
// 排列，各段可以有粗體、斜體、底線、字級與顏色；整段文字一個對齊方式。
//
// 內容是 IR 的 TextRun 清單。畫面是一個 contentEditable 的區塊：文字照打，欄位與參數是不可編輯的小方塊
// （data-run 記下它是哪個欄位或參數）；格式以瀏覽器的指令（粗體、斜體、底線、顏色）與包上 <span style> 的方式
// 套在選取的文字上。每次變更把畫面上的內容讀回 TextRun：文字節點的格式取自它外層各層元素的標籤與 style。

import { useEffect, useRef, useState } from "react";
import type { FieldUse, TextStyle } from "../types";

export interface RichRun {
  text?: string | null;
  use?: FieldUse | null;
  parameter?: string | null;
  style?: TextStyle | null;
}

/** 可以插入的欄位或參數。 */
export interface RichToken {
  label: string;
  run: { use?: FieldUse; parameter?: string };
}

const SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36];
/** 零寬度的字元（欄位、參數的小方塊後面，讓游標放得到那裡）；不是內容。 */
const ZWSP = "\u200b";

const tokenKey = (run: { use?: FieldUse | null; parameter?: string | null }) => JSON.stringify(run.use ? { use: run.use } : { parameter: run.parameter });

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function styleCss(style: TextStyle | null | undefined): string {
  if (!style) return "";
  const css: string[] = [];
  if (style.bold) css.push("font-weight:bold");
  if (style.italic) css.push("font-style:italic");
  if (style.underline) css.push("text-decoration:underline");
  if (style.size) css.push(`font-size:${style.size}pt`);
  if (style.color) css.push(`color:${style.color}`);
  return css.join(";");
}

/** TextRun → 編輯區的 HTML。 */
function toHtml(runs: RichRun[], tokens: RichToken[]): string {
  const labels = new Map(tokens.map((t) => [tokenKey(t.run), t.label]));
  return runs
    .map((run) => {
      const css = styleCss(run.style);
      const open = css ? `<span style="${css}">` : "";
      const close = css ? "</span>" : "";
      if (run.text != null) return `${open}${escapeHtml(run.text)}${close}`;
      const key = tokenKey(run);
      const label = labels.get(key) ?? (run.parameter ? run.parameter : (run.use?.field ?? ""));
      // 小方塊後面放一個零寬度的字元：游標才放得到它後面（讀回內容時去掉）
      return `${open}<span class="rich-token" contenteditable="false" data-run="${escapeHtml(key)}">${escapeHtml(`<${label}>`)}</span>${close}${ZWSP}`;
    })
    .join("");
}

function rgbToHex(color: string): string | null {
  if (/^#[0-9a-f]{6}$/i.test(color)) return color.toLowerCase();
  const match = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(color);
  if (!match) return null;
  return `#${[match[1], match[2], match[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
}

/** 一個節點的格式：由內而外找各層元素的標籤與 style，最內層的設定優先。 */
function styleOf(node: Node, root: HTMLElement): TextStyle | null {
  const style: TextStyle = {};
  for (let el = node.parentElement; el && el !== root; el = el.parentElement) {
    const tag = el.tagName;
    const css = el.style;
    if (style.bold === undefined) {
      if (css.fontWeight) style.bold = css.fontWeight === "bold" || Number(css.fontWeight) >= 600;
      else if (tag === "B" || tag === "STRONG") style.bold = true;
    }
    if (style.italic === undefined) {
      if (css.fontStyle) style.italic = css.fontStyle === "italic";
      else if (tag === "I" || tag === "EM") style.italic = true;
    }
    if (style.underline === undefined) {
      const line = css.textDecorationLine || css.textDecoration;
      if (line) style.underline = line.includes("underline");
      else if (tag === "U") style.underline = true;
    }
    if (style.size === undefined && css.fontSize.endsWith("pt")) style.size = Number.parseFloat(css.fontSize);
    if (style.color === undefined) {
      const color = rgbToHex(css.color || el.getAttribute("color") || "");
      if (color) style.color = color;
    }
  }
  const cleaned: TextStyle = {};
  if (style.bold) cleaned.bold = true;
  if (style.italic) cleaned.italic = true;
  if (style.underline) cleaned.underline = true;
  if (style.size) cleaned.size = style.size;
  if (style.color) cleaned.color = style.color;
  return Object.keys(cleaned).length ? cleaned : null;
}

/** 編輯區的內容 → TextRun（相鄰、格式相同的文字合併）。 */
function fromDom(root: HTMLElement, align: TextStyle["align"]): RichRun[] {
  const runs: RichRun[] = [];
  const withAlign = (style: TextStyle | null): TextStyle | null => (align ? { ...(style ?? {}), align } : style);
  const addText = (text: string, style: TextStyle | null) => {
    if (!text) return;
    const last = runs[runs.length - 1];
    const full = withAlign(style);
    if (last && last.text != null && JSON.stringify(last.style ?? null) === JSON.stringify(full)) last.text += text;
    else runs.push({ text, style: full });
  };
  const visit = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      addText((node.textContent ?? "").split(ZWSP).join(""), styleOf(node, root));
      return;
    }
    if (!(node instanceof HTMLElement)) return;
    if (node.dataset.run) {
      const run = JSON.parse(node.dataset.run) as { use?: FieldUse; parameter?: string };
      runs.push({ ...run, style: withAlign(styleOf(node, root)) });
      return;
    }
    if (node.tagName === "BR") {
      // 區塊最後的 <br> 是瀏覽器放的空白行記號，不是內容
      if (node.nextSibling || node.parentElement === root) addText("\n", null);
      return;
    }
    // 瀏覽器在換行時可能包成區塊（div、p）：區塊之前換行
    const block = node.tagName === "DIV" || node.tagName === "P";
    if (block && runs.length > 0 && !(runs[runs.length - 1].text ?? "x").endsWith("\n")) addText("\n", null);
    node.childNodes.forEach(visit);
  };
  root.childNodes.forEach(visit);
  // 結尾的換行不算內容（瀏覽器在最後一行之後補上的空白行；Tableau 的格式文字也不留結尾的空白）
  const last = runs[runs.length - 1];
  if (last && last.text != null) {
    last.text = last.text.replace(/\n+$/, "");
    if (!last.text) runs.pop();
  }
  return runs;
}

/** 選取範圍是否在編輯區之內。 */
function selectionIn(root: HTMLElement): Range | null {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  return root.contains(range.commonAncestorContainer) ? range : null;
}

export function RichTextEditor({
  value,
  onChange,
  tokens = [],
  label,
  rows = 4,
}: {
  value: RichRun[];
  onChange: (runs: RichRun[]) => void;
  tokens?: RichToken[];
  /** 編輯區的 aria-label */
  label: string;
  rows?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [align, setAlign] = useState<TextStyle["align"]>(() => value.find((r) => r.style?.align)?.style?.align ?? null);
  const alignRef = useRef(align);
  alignRef.current = align;
  // 編輯區中最後的選取範圍：工具列的選單、色彩選擇器會拿走焦點，套用格式之前先還原
  const saved = useRef<Range | null>(null);
  useEffect(() => {
    const track = () => {
      const root = ref.current;
      const range = root ? selectionIn(root) : null;
      if (range) saved.current = range.cloneRange();
    };
    document.addEventListener("selectionchange", track);
    return () => document.removeEventListener("selectionchange", track);
  }, []);
  const restore = () => {
    const root = ref.current;
    if (!root) return;
    // 選取範圍還在編輯區中（選單不會改變文件的選取）：直接用它；記下的範圍可能還沒更新
    if (selectionIn(root)) {
      root.focus();
      return;
    }
    const range = saved.current;
    if (!range) return;
    root.focus();
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  };
  // 內容只在掛上時寫進編輯區（之後由使用者編輯，避免游標跳動）
  useEffect(() => {
    if (ref.current) ref.current.innerHTML = toHtml(value, tokens);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const emit = () => {
    if (ref.current) onChange(fromDom(ref.current, alignRef.current));
  };
  const focus = () => {
    const root = ref.current;
    if (!root) return null;
    if (!selectionIn(root)) {
      // 沒有選取：游標放到最後
      root.focus();
      const range = document.createRange();
      range.selectNodeContents(root);
      range.collapse(false);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
    }
    return root;
  };
  const command = (name: string, argument?: string) => {
    if (!focus()) return;
    document.execCommand("styleWithCSS", false, "true");
    document.execCommand(name, false, argument);
    emit();
  };
  /** 選取的文字包上 <span style>（字級）。 */
  const wrap = (css: Partial<CSSStyleDeclaration>) => {
    const root = focus();
    const range = root ? selectionIn(root) : null;
    if (!root || !range || range.collapsed) return;
    const span = document.createElement("span");
    Object.assign(span.style, css);
    span.appendChild(range.extractContents());
    range.insertNode(span);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    const next = document.createRange();
    next.selectNodeContents(span);
    selection?.addRange(next);
    emit();
  };
  const insert = (token: RichToken) => {
    const root = focus();
    const range = root ? selectionIn(root) : null;
    if (!root || !range) return;
    range.deleteContents();
    const span = document.createElement("span");
    span.className = "rich-token";
    span.contentEditable = "false";
    span.dataset.run = tokenKey(token.run);
    span.textContent = `<${token.label}>`;
    // 小方塊後面放一個零寬度的字元，游標移到它後面（可以接著打字）
    const tail = document.createTextNode(ZWSP);
    range.insertNode(tail);
    range.insertNode(span);
    const next = document.createRange();
    next.setStart(tail, tail.length);
    next.collapse(true);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(next);
    root.focus();
    emit();
  };
  return (
    <div className="rich-text">
      <div className="rich-toolbar" role="toolbar" aria-label={`${label}的格式`}>
        <button type="button" aria-label="粗體" title="粗體" onMouseDown={(e) => e.preventDefault()} onClick={() => command("bold")}>
          <b>B</b>
        </button>
        <button type="button" aria-label="斜體" title="斜體" onMouseDown={(e) => e.preventDefault()} onClick={() => command("italic")}>
          <i>I</i>
        </button>
        <button type="button" aria-label="底線" title="底線" onMouseDown={(e) => e.preventDefault()} onClick={() => command("underline")}>
          <u>U</u>
        </button>
        <select
          aria-label="字級"
          value=""
          onChange={(e) => {
            const size = Number(e.target.value);
            restore();
            if (size) wrap({ fontSize: `${size}pt` });
          }}
        >
          <option value="">字級</option>
          {SIZES.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
        <input
          type="color"
          aria-label="文字顏色"
          title="文字顏色"
          defaultValue="#333333"
          onChange={(e) => {
            restore();
            command("foreColor", e.target.value);
          }}
        />
        <select
          aria-label="對齊"
          value={align ?? ""}
          onChange={(e) => {
            const next = (e.target.value || null) as TextStyle["align"];
            setAlign(next);
            alignRef.current = next;
            emit();
          }}
        >
          <option value="">對齊：預設</option>
          <option value="left">靠左</option>
          <option value="center">置中</option>
          <option value="right">靠右</option>
        </select>
        {tokens.length > 0 && (
          <select
            aria-label="插入"
            value=""
            onChange={(e) => {
              const token = tokens[Number(e.target.value)];
              restore();
              if (token) insert(token);
            }}
          >
            <option value="">插入…</option>
            {tokens.map((token, i) => (
              <option key={tokenKey(token.run)} value={i}>
                {token.label}
              </option>
            ))}
          </select>
        )}
      </div>
      <div
        ref={ref}
        className="rich-editor"
        role="textbox"
        aria-multiline="true"
        aria-label={label}
        contentEditable
        suppressContentEditableWarning
        style={{ minHeight: `${rows * 1.5}em`, textAlign: align ?? undefined }}
        onInput={emit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            // 換行一律是文字中的換行字元（不讓瀏覽器包成區塊）
            e.preventDefault();
            document.execCommand("insertText", false, "\n");
          }
        }}
        onPaste={(e) => {
          // 貼上只取純文字
          e.preventDefault();
          document.execCommand("insertText", false, e.clipboardData.getData("text/plain"));
        }}
      />
    </div>
  );
}

/** TextRun 的純文字（欄位與參數以 <名稱> 表示），清單中顯示用。 */
export function plainText(runs: RichRun[], tokens: RichToken[] = []): string {
  const labels = new Map(tokens.map((t) => [tokenKey(t.run), t.label]));
  return runs.map((run) => (run.text != null ? run.text : `<${labels.get(tokenKey(run)) ?? run.parameter ?? run.use?.field ?? ""}>`)).join("");
}
