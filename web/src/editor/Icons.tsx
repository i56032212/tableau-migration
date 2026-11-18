// 編輯器的圖示：自己畫的單色線條圖示（16×16，顏色跟著文字），不使用任何產品的圖示。

import type React from "react";

const PATHS = {
  undo: (
    <>
      <path d="M5.5 3.5l-3 3 3 3" />
      <path d="M2.5 6.5h7a3.5 3.5 0 0 1 0 7H7" />
    </>
  ),
  redo: (
    <>
      <path d="M10.5 3.5l3 3-3 3" />
      <path d="M13.5 6.5h-7a3.5 3.5 0 0 0 0 7H9" />
    </>
  ),
  save: (
    <>
      <path d="M2.5 2.5h9l2 2v9h-11z" />
      <path d="M5 2.5v3.5h5V2.5M5 13.5V9.5h6v4" />
    </>
  ),
  datasource: (
    <>
      <ellipse cx="8" cy="4" rx="5" ry="1.8" />
      <path d="M3 4v8c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V4" />
      <path d="M3 8c0 1 2.2 1.8 5 1.8s5-.8 5-1.8" />
    </>
  ),
  "datasource-add": (
    <>
      <ellipse cx="6.5" cy="4" rx="4" ry="1.6" />
      <path d="M2.5 4v7.6c0 .8 1.6 1.5 3.6 1.6M10.5 4v3.5" />
      <path d="M12 9.5v5M9.5 12h5" />
    </>
  ),
  worksheet: (
    <>
      <rect x="2.5" y="3" width="11" height="10" />
      <path d="M2.5 6.3h11M6.3 6.3V13M2.5 9.6h11" />
    </>
  ),
  "worksheet-add": (
    <>
      <path d="M8.5 13H2.5V3h10v5" />
      <path d="M2.5 6.3h10M6.3 6.3V13" />
      <path d="M12 9.5v5M9.5 12h5" />
    </>
  ),
  dashboard: (
    <>
      <rect x="2.5" y="2.5" width="11" height="11" />
      <path d="M8 2.5v11M8 7h5.5M2.5 9.5H8" />
    </>
  ),
  "dashboard-add": (
    <>
      <path d="M8.5 13.5h-6v-11h11V8" />
      <path d="M7.5 2.5v11M2.5 8.5h5" />
      <path d="M12 9.5v5M9.5 12h5" />
    </>
  ),
  story: <path d="M8 4.6C6.5 3.4 4.5 3 2.5 3.2v9c2-.2 4 .2 5.5 1.4 1.5-1.2 3.5-1.6 5.5-1.4v-9c-2-.2-4 .2-5.5 1.4zM8 4.6v9" />,
  "story-add": (
    <>
      <path d="M7.5 13.3c-1.4-1-3.2-1.4-5-1.2v-9c1.8-.2 3.6.2 5 1.3v8.9zM7.5 4.4c1.4-1.1 3.2-1.5 5-1.3V8" />
      <path d="M12 9.5v5M9.5 12h5" />
    </>
  ),
  duplicate: (
    <>
      <rect x="5.5" y="5.5" width="8" height="8" />
      <path d="M10.5 5.5v-3h-8v8h3" />
    </>
  ),
  clear: (
    <>
      <rect x="2.5" y="2.5" width="11" height="11" />
      <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />
    </>
  ),
  swap: (
    <>
      <path d="M3 5.5h9.5M10 3l2.5 2.5L10 8" />
      <path d="M13 10.5H3.5M6 8l-2.5 2.5L6 13" />
    </>
  ),
  "sort-asc": (
    <>
      <path d="M2.5 4.5h3M2.5 8.5h5M2.5 12.5h7" />
      <path d="M12.5 13V3.5M10.5 5.5l2-2 2 2" />
    </>
  ),
  "sort-desc": (
    <>
      <path d="M2.5 4.5h7M2.5 8.5h5M2.5 12.5h3" />
      <path d="M12.5 3v9.5M10.5 10.5l2 2 2-2" />
    </>
  ),
  "show-me": (
    <>
      <path d="M3 13.5V9M6.5 13.5V6M10 13.5v-3" />
      <path d="M12 2l.8 1.9 1.9.8-1.9.8L12 7.4l-.8-1.9-1.9-.8 1.9-.8z" />
    </>
  ),
  color: <path d="M8 2.5c2.1 2.7 3.6 4.7 3.6 6.6a3.6 3.6 0 0 1-7.2 0C4.4 7.2 5.9 5.2 8 2.5z" />,
  size: <path d="M3.5 12.5l9-9M8.5 3.5h4v4M3.5 8.5v4h4" />,
  label: (
    <>
      <path d="M2.5 3h6l5 5-5 5-6-6z" />
      <circle cx="5.5" cy="6" r="0.7" />
    </>
  ),
  detail: <path d="M8 2.5L2.5 5.5 8 8.5l5.5-3zM2.5 8.3L8 11.3l5.5-3M2.5 11L8 14l5.5-3" />,
  tooltip: <path d="M2.5 3h11v7.5H8L5 13v-2.5H2.5z" />,
  shape: (
    <>
      <path d="M5 3l3 5H2z" />
      <rect x="8.5" y="8.5" width="5" height="5" />
    </>
  ),
  path: (
    <>
      <path d="M2.5 12L6 6l4 4 3.5-6" />
      <circle cx="6" cy="6" r="0.8" />
      <circle cx="10" cy="10" r="0.8" />
    </>
  ),
  angle: <path d="M8 8V2.5A5.5 5.5 0 1 1 3.2 10.7z" />,
  geometry: <path d="M3 5.5l5-3 5 3.5-1.5 6.5h-7z" />,
  columns: <path d="M4 3v10M8 3v10M12 3v10" />,
  rows: <path d="M3 4h10M3 8h10M3 12h10" />,
  caret: <path d="M4.5 6.5L8 10l3.5-3.5" />,
  grid: (
    <>
      <path d="M2.5 5.5h11M2.5 10.5h11M5.5 2.5v11M10.5 2.5v11" />
    </>
  ),
  actions: <path d="M9 2.5L4 9h3.5L7 13.5 12 7H8.5z" />,
} satisfies Record<string, React.ReactNode>;

export type IconName = keyof typeof PATHS;

/** 單色的線條圖示。 */
export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
