// 匯入另一份報表的工作表（Tableau 的「檔案＞匯入活頁簿」、書籤，以及在活頁簿之間複製貼上工作表）：
// 從工作區中開啟的其他報表選取工作表，或整份匯入（含 dashboard、Story 與動作）。資料源與資料一併帶過來。

import { useEffect, useState } from "react";
import { api } from "../api";
import { Modal } from "../Server";
import type { WorkbookSummary } from "../types";
import { editApi } from "./api";

export function ImportSheetsDialog({
  workbook,
  onClose,
  onImported,
}: {
  workbook: string;
  onClose: () => void;
  /** 匯入完成：第一張匯入的工作表與匯入的張數 */
  onImported: (worksheet: string | null, count: number) => void;
}) {
  const [books, setBooks] = useState<WorkbookSummary[] | null>(null);
  const [source, setSource] = useState("");
  const [whole, setWhole] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .workbooks()
      .then((list) => {
        const others = list.filter((b) => b.id !== workbook);
        setBooks(others);
        if (others.length > 0) setSource(others[0].id);
      })
      .catch((e: Error) => setError(e.message));
  }, [workbook]);
  const current = books?.find((b) => b.id === source);
  const sheets = current?.worksheets ?? [];
  const submit = () => {
    if (!current || (!whole && chosen.length === 0)) return;
    setBusy(true);
    setError(null);
    editApi
      .importSheets(workbook, current.id, whole ? null : chosen)
      .then((result) => onImported(result.worksheets[0] ?? null, result.worksheets.length))
      .catch((e: Error) => {
        setError(e.message);
        setBusy(false);
      });
  };
  return (
    <Modal title="匯入工作表" onClose={onClose}>
      <div className="import-dialog">
        {books === null && !error && <p className="hint">載入中…</p>}
        {books !== null && books.length === 0 && <p className="hint">工作區中沒有其他報表：先開啟要匯入的報表（.twbx、.twb 或報表套件），再回到這裡。</p>}
        {books !== null && books.length > 0 && (
          <>
            <label className="inline">
              來源報表
              <select
                aria-label="來源報表"
                value={source}
                onChange={(e) => {
                  setSource(e.target.value);
                  setChosen([]);
                }}
              >
                {books.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="row" role="radiogroup" aria-label="匯入的範圍">
              <label>
                <input type="radio" checked={!whole} onChange={() => setWhole(false)} /> 選取的工作表
              </label>
              <label title="所有的工作表、dashboard、Story 與動作（Tableau 的「匯入活頁簿」）">
                <input type="radio" checked={whole} onChange={() => setWhole(true)} /> 整份報表（含 dashboard、Story 與動作）
              </label>
            </div>
            {!whole && (
              <ul className="value-list import-sheets" aria-label="要匯入的工作表">
                {sheets.map((w) => (
                  <li key={w.id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={chosen.includes(w.id)}
                        onChange={(e) => setChosen(e.target.checked ? [...chosen, w.id] : chosen.filter((id) => id !== w.id))}
                      />{" "}
                      {w.name}
                      {w.hidden ? "（隱藏）" : ""}
                    </label>
                  </li>
                ))}
              </ul>
            )}
            <p className="hint">用到的資料源、資料與參數一併匯入；名稱相同的加上「 (2)」。</p>
          </>
        )}
        {error && <p className="error">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="primary" disabled={busy || !current || (!whole && chosen.length === 0)} onClick={submit}>
            {busy ? "匯入中…" : "匯入"}
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </Modal>
  );
}
