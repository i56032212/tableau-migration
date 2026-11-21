// 編輯的歷程（復原、重做）：每次變更依序存回引擎，失敗時退回；dashboard 與 Story 編輯器共用。

import { useCallback, useEffect, useRef, useState } from "react";

interface History<T> {
  entries: T[];
  index: number;
}

export interface EditHistory<T> {
  current: T | null;
  error: string | null;
  pending: number;
  canUndo: boolean;
  canRedo: boolean;
  /** 套用變更（同步或非同步），存回後記入歷程 */
  apply: (change: (value: T) => T | Promise<T>) => void;
  undo: () => void;
  redo: () => void;
  /** 每次存回成功後遞增 */
  revision: number;
  /** 歷程目前在第幾步、一共幾步（還沒載入時是 0）：與整份報表刪除分頁的紀錄排先後用（structure.ts） */
  index: number;
  length: number;
}

/** 復原、重做的快捷鍵（Ctrl+Z、Ctrl+Y）不在這裡：編輯器把這份歷程交給 structure.ts 的 useUndoRedo，
 * 與「復原刪除的分頁」一起處理。onEdit：使用者做了新的變更（不含復原與重做）。 */
export function useEditHistory<T>(
  key: string,
  load: () => Promise<T>,
  save: (value: T) => Promise<T>,
  notify: (message: string, kind?: "info" | "error") => void,
  onSaved?: (value: T, previous: T) => void,
  onEdit?: () => void,
): EditHistory<T> {
  const [history, setHistoryState] = useState<History<T> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);
  const [revision, setRevision] = useState(0);
  const ref = useRef<History<T> | null>(null);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const setHistory = (next: History<T> | null) => {
    ref.current = next;
    setHistoryState(next);
  };
  const callbacks = useRef({ load, save, notify, onSaved, onEdit });
  callbacks.current = { load, save, notify, onSaved, onEdit };

  useEffect(() => {
    let cancelled = false;
    setHistory(null);
    callbacks.current
      .load()
      .then((value) => {
        if (cancelled) return;
        setHistory({ entries: [value], index: 0 });
        setError(null);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [key]);

  const enqueue = useCallback((task: () => Promise<void>) => {
    setPending((n) => n + 1);
    queue.current = queue.current.then(task).finally(() => setPending((n) => n - 1));
  }, []);

  const store = async (value: T): Promise<T | null> => {
    try {
      const saved = await callbacks.current.save(value);
      setRevision((r) => r + 1);
      return saved;
    } catch (e) {
      callbacks.current.notify((e as Error).message, "error");
      return null;
    }
  };

  const apply = useCallback(
    (change: (value: T) => T | Promise<T>) =>
      enqueue(async () => {
        const current = ref.current;
        if (!current) return;
        const base = current.entries[current.index];
        let next: T;
        try {
          next = await change(base);
        } catch (e) {
          callbacks.current.notify((e as Error).message, "error");
          return;
        }
        if (JSON.stringify(next) === JSON.stringify(base)) return;
        callbacks.current.onEdit?.();
        setHistory({ entries: [...current.entries.slice(0, current.index + 1), next], index: current.index + 1 });
        const saved = await store(next);
        const latest = ref.current;
        if (!latest) return;
        if (saved === null) {
          setHistory({ entries: latest.entries.slice(0, latest.index), index: latest.index - 1 });
          return;
        }
        setHistory({ entries: latest.entries.map((e, i) => (i === latest.index ? saved : e)), index: latest.index });
        callbacks.current.onSaved?.(saved, base);
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enqueue],
  );

  const move = useCallback(
    (delta: number) =>
      enqueue(async () => {
        const current = ref.current;
        if (!current) return;
        const index = current.index + delta;
        if (index < 0 || index >= current.entries.length) return;
        const saved = await store(current.entries[index]);
        if (saved === null) return;
        setHistory({ entries: current.entries, index });
        callbacks.current.onSaved?.(saved, current.entries[current.index]);
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enqueue],
  );
  const undo = useCallback(() => move(-1), [move]);
  const redo = useCallback(() => move(1), [move]);

  return {
    current: history ? history.entries[history.index] : null,
    error,
    pending,
    canUndo: history !== null && history.index > 0,
    canRedo: history !== null && history.index < history.entries.length - 1,
    apply,
    undo,
    redo,
    revision,
    index: history?.index ?? 0,
    length: history?.entries.length ?? 0,
  };
}

/** 編輯器的短訊息（數秒後消失）。 */
export function useNotice(): [{ text: string; kind: "info" | "error" } | null, (text: string, kind?: "info" | "error") => void] {
  const [message, setMessage] = useState<{ text: string; kind: "info" | "error" } | null>(null);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(null), message.kind === "error" ? 8000 : 3000);
    return () => window.clearTimeout(timer);
  }, [message]);
  const notify = useCallback((text: string, kind: "info" | "error" = "info") => setMessage({ text, kind }), []);
  return [message, notify];
}
