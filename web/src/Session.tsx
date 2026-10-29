import { useCallback, useEffect, useState } from "react";
import { api, UNAUTHORIZED_EVENT } from "./api";
import type { Me } from "./types";

/** oidc 模式的登入網址：登入後回到目前的頁面。 */
function loginUrl(me: Me): string {
  const next = window.location.pathname + window.location.search;
  return `${me.login ?? "/auth/login"}?next=${encodeURIComponent(next)}`;
}

/**
 * 確認登入者後才顯示頁面（P2）：oidc 模式沒有登入時轉到登入頁，開發模式先選使用者，
 * 反向代理模式沒有登入者時顯示錯誤。查詢服務回應 401 時重新確認。
 */
export function SessionGate({ children }: { children: (me: Me) => React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(() => {
    api
      .me()
      .then((next) => {
        setMe(next);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => {
    refresh();
    window.addEventListener(UNAUTHORIZED_EVENT, refresh);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, refresh);
  }, [refresh]);

  const needsLogin = me !== null && me.mode === "oidc" && me.user === null;
  useEffect(() => {
    if (needsLogin && me) window.location.assign(loginUrl(me));
  }, [needsLogin, me]);

  if (error) return <main className="home error">無法連到查詢服務：{error}</main>;
  if (!me || needsLogin) return <main className="home">載入中…</main>;
  if (me.mode === "dev" && !me.user) {
    return (
      <main className="home">
        <h1>開發模式</h1>
        <p>請選擇要以哪個使用者檢視（報表的列層級權限依使用者而不同）：</p>
        <UserSwitcher me={me} />
      </main>
    );
  }
  if (me.mode === "proxy" && !me.user) {
    return <main className="home error">沒有登入者：請經由公司的登入入口開啟這個網站</main>;
  }
  return <>{children(me)}</>;
}

/** 開發模式切換使用者：寫入工作階段後重新載入，所有圖表以新的身分查詢。 */
function UserSwitcher({ me }: { me: Me }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <label className="user-switcher">
      使用者
      <select
        aria-label="切換使用者"
        value={me.user?.username ?? ""}
        onChange={(e) => {
          api
            .devLogin(e.target.value)
            .then(() => window.location.reload())
            .catch((err: Error) => setError(err.message));
        }}
      >
        {!me.user && <option value="">（選擇使用者）</option>}
        {(me.users ?? []).map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
      {error && <span className="error">{error}</span>}
    </label>
  );
}

/** 頁首右側的登入者：開發模式可切換，oidc 與開發模式可登出。 */
export function UserBar({ me }: { me: Me }) {
  if (me.mode === "none" || !me.user) return null;
  const name = me.user.full_name || me.user.username;
  const logout = () => {
    api.logout().finally(() => window.location.reload());
  };
  return (
    <span className="userbar">
      {me.mode === "dev" ? (
        <UserSwitcher me={me} />
      ) : (
        <span className="user" title={me.user.groups.join("、")}>
          {name}
        </span>
      )}
      {(me.mode === "oidc" || me.mode === "dev") && (
        <button type="button" className="link" onClick={logout}>
          登出
        </button>
      )}
    </span>
  );
}
