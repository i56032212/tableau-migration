import { useState } from "react";
import { Modal } from "./Server";

// 站台管理與排程管理共用的對話框：輸入一個值、確認刪除、勾選清單

export function PromptDialog({
  title,
  label,
  initial = "",
  hint,
  onClose,
  onSubmit,
}: {
  title: string;
  label: string;
  initial?: string;
  hint?: string;
  onClose: () => void;
  onSubmit: (value: string) => Promise<boolean>;
}) {
  const [value, setValue] = useState(initial);
  const submit = () => {
    if (value.trim()) void onSubmit(value.trim()).then((ok) => ok && onClose());
  };
  return (
    <Modal title={title} onClose={onClose}>
      {hint && <p className="hint">{hint}</p>}
      <label className="admin-field">
        {label}
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
      </label>
      <div className="dialog-actions">
        <button type="button" className="primary" disabled={!value.trim()} onClick={submit}>
          確定
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirm = "刪除",
  onClose,
  onConfirm,
}: {
  title: string;
  message: string;
  confirm?: string;
  onClose: () => void;
  onConfirm: () => Promise<boolean>;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <p>{message}</p>
      <div className="dialog-actions">
        <button type="button" className="danger" onClick={() => void onConfirm().then((ok) => ok && onClose())}>
          {confirm}
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}

export function ChecklistDialog({
  title,
  options,
  checked,
  onClose,
  onSave,
}: {
  title: string;
  options: string[];
  checked: string[];
  onClose: () => void;
  onSave: (values: string[]) => Promise<boolean>;
}) {
  const [values, setValues] = useState(() => new Set(checked));
  const [filter, setFilter] = useState("");
  const shown = options.filter((o) => o.toLowerCase().includes(filter.trim().toLowerCase()));
  return (
    <Modal title={title} onClose={onClose}>
      {options.length > 10 && (
        <input
          type="search"
          placeholder="搜尋"
          aria-label="搜尋"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      )}
      <div className="checklist">
        {shown.map((option) => (
          <label key={option}>
            <input
              type="checkbox"
              checked={values.has(option)}
              onChange={(e) => {
                const next = new Set(values);
                if (e.target.checked) next.add(option);
                else next.delete(option);
                setValues(next);
              }}
            />{" "}
            {option}
          </label>
        ))}
        {options.length === 0 && <p className="hint">（沒有可以選的項目）</p>}
      </div>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          onClick={() => void onSave([...values]).then((ok) => ok && onClose())}
        >
          儲存
        </button>
        <button type="button" onClick={onClose}>
          取消
        </button>
      </div>
    </Modal>
  );
}
