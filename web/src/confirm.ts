/**
 * 確認（確定／取消）。一律用這個，不直接寫 `if (!window.confirm(…))`。
 *
 * 桌面版的外殼（Tauri 的對話框外掛）把 window.confirm 換成非同步的：跳出原生的訊息方塊、回傳 Promise。
 * Promise 是真值，同步的寫法在外殼中等於沒有問就做了（刪除工作表、關閉還沒儲存的報表都不會先確認）。
 * 瀏覽器中 window.confirm 是同步的、回傳布林值；兩種都等結果。
 *
 * 對話框出不來時（例如外殼沒有允許這個指令）當作取消：寧可不做，不要沒有問就刪除。
 */
export async function confirmBox(message: string): Promise<boolean> {
  try {
    return (await (window.confirm(message) as boolean | Promise<boolean>)) === true;
  } catch {
    return false;
  }
}
