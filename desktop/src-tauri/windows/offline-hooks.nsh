; 離線安裝檔的安裝掛鉤（tauri.offline.conf.json）。
; 內附的 WebView2 固定版本放在安裝資料夾的 webview2-fixed。Microsoft 的說明：Windows 10 上未封裝的程式使用
; 固定版本 120 以後的版本，要讓「ALL APPLICATION PACKAGES」與「ALL RESTRICTED APPLICATION PACKAGES」
; 可以讀取與執行這個資料夾，否則 WebView2 的沙箱程序開不起來。Windows 11 不受影響，照樣設定也無害。
; https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "設定內附 WebView2 的資料夾權限"
  nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$INSTDIR\webview2-fixed" /grant *S-1-15-2-2:(OI)(CI)(RX) /T /Q'
  Pop $0
  nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$INSTDIR\webview2-fixed" /grant *S-1-15-2-1:(OI)(CI)(RX) /T /Q'
  Pop $0
!macroend
