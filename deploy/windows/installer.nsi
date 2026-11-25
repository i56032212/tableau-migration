; 報表平台伺服器（TmigServer 服務）的 Windows 離線安裝程式。由 deploy/windows/build.py 呼叫：
;
;   makensis /INPUTCHARSET UTF8 /DVERSION=<版本> /DSTAGING=<組好的資料夾> /DOUTFILE=<安裝檔> installer.nsi
;
; 安裝：程式放在 Program Files\TmigServer（可以改），報表、設定與記錄放在 C:\ProgramData\TmigServer；
; 登錄 Windows 服務 TmigServer（延遲自動啟動，以虛擬帳號 NT SERVICE\TmigServer 執行）並啟動。
; 升級：先停止服務，換掉程式，不動 ProgramData 中的設定與報表。
; 解除安裝：停止並刪除服務、刪除程式；ProgramData 中的報表與設定保留。
; 無人值守：安裝檔加 /S（/D=<安裝目錄> 放在最後），解除安裝 uninstall.exe /S。

Unicode true
ManifestDPIAware true
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!ifndef VERSION
  !error "需要 /DVERSION=<版本>"
!endif
!ifndef STAGING
  !error "需要 /DSTAGING=<組好的資料夾>"
!endif
!ifndef OUTFILE
  !error "需要 /DOUTFILE=<安裝檔>"
!endif

!define SERVICE "TmigServer"
!define PRODUCT "報表平台伺服器"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE}"

Name "${PRODUCT} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${SERVICE}"
InstallDirRegKey HKLM "${UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show
BrandingText "${PRODUCT} ${VERSION}"

!include MUI2.nsh
!include LogicLib.nsh
!include x64.nsh
!include FileFunc.nsh

Var DataDir

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "安裝${PRODUCT} ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "這個安裝程式內含執行時需要的一切（Python、套件、資料庫驅動、檢視器與產生訂閱用的 Chromium），不必連網路。$\r$\n$\r$\n安裝後會登錄並啟動 Windows 服務「${SERVICE}」。報表、設定與記錄放在 C:\ProgramData\${SERVICE}；升級與解除安裝都不會刪除它們。$\r$\n$\r$\n已經安裝舊版時，安裝程式會先停止服務再更新。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "安裝完成"
!define MUI_FINISHPAGE_TEXT "服務「${SERVICE}」已經啟動，預設只接受這台電腦本身的連線：http://127.0.0.1:8000$\r$\n$\r$\n設定檔是 C:\ProgramData\${SERVICE}\server.toml（身分驗證、對外的位址與埠號、排程器）。設定好身分驗證之後才開放其他電腦連線，修改後重新啟動服務。記錄檔在同一個資料夾的 logs\。"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "TradChinese"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_TRADCHINESE} "ProductName" "${PRODUCT}"
VIAddVersionKey /LANG=${LANG_TRADCHINESE} "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_TRADCHINESE} "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_TRADCHINESE} "FileDescription" "${PRODUCT}的離線安裝程式"
VIAddVersionKey /LANG=${LANG_TRADCHINESE} "LegalCopyright" "報表平台；第三方元件的授權見安裝目錄的 NOTICE.md"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "${PRODUCT}需要 64 位元的 Windows。" /SD IDOK
    Abort
  ${EndIf}
  SetRegView 64
  SetShellVarContext all
FunctionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext all
FunctionEnd

; 停止服務（沒有安裝過時什麼都不做）；net stop 會等到服務停止
!macro StopService
  DetailPrint "停止服務 ${SERVICE}"
  nsExec::ExecToLog '"$SYSDIR\net.exe" stop ${SERVICE}'
  Pop $0
!macroend

Section "Install"
  StrCpy $DataDir "$APPDATA\${SERVICE}"
  !insertmacro StopService

  ; 換掉舊版的程式（資料夾中的設定與報表不動）
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\ms-playwright"
  RMDir /r "$INSTDIR\web"
  RMDir /r "$INSTDIR\config"
  SetOutPath "$INSTDIR"
  File /r "${STAGING}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 報表、設定與記錄的資料夾；server.toml 只在第一次安裝時放預設內容
  CreateDirectory "$DataDir\workbooks"
  CreateDirectory "$DataDir\store"
  CreateDirectory "$DataDir\usage"
  CreateDirectory "$DataDir\logs"
  CreateDirectory "$DataDir\temp"
  ${IfNot} ${FileExists} "$DataDir\server.toml"
    CopyFiles /SILENT "$INSTDIR\config\server.toml" "$DataDir\server.toml"
  ${EndIf}

  ; 服務：沒有時建立，已經有時只更新執行的指令（保留系統管理員改過的帳號與啟動方式）
  StrCpy $1 '\"$INSTDIR\tmig-server-host.exe\" service --config \"$DataDir\server.toml\"'
  nsExec::ExecToLog '"$SYSDIR\sc.exe" query ${SERVICE}'
  Pop $0
  ${If} $0 == 0
    DetailPrint "更新服務 ${SERVICE}"
    nsExec::ExecToLog '"$SYSDIR\sc.exe" config ${SERVICE} binPath= "$1"'
    Pop $0
  ${Else}
    DetailPrint "建立服務 ${SERVICE}"
    nsExec::ExecToLog '"$SYSDIR\sc.exe" create ${SERVICE} binPath= "$1" start= delayed-auto obj= "NT SERVICE\${SERVICE}" DisplayName= "${PRODUCT}"'
    Pop $0
    nsExec::ExecToLog '"$SYSDIR\sc.exe" sidtype ${SERVICE} unrestricted'
    Pop $2
  ${EndIf}
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "無法登錄服務 ${SERVICE}（sc.exe 結束碼 $0）。" /SD IDOK
    Abort
  ${EndIf}
  nsExec::ExecToLog '"$SYSDIR\sc.exe" description ${SERVICE} "報表平台的查詢服務、檢視器與排程器。設定檔：$DataDir\server.toml"'
  Pop $0
  ; 服務主程式本身異常結束時，一分鐘後重新啟動（程序的重新啟動由服務主程式負責）
  nsExec::ExecToLog '"$SYSDIR\sc.exe" failure ${SERVICE} reset= 86400 actions= restart/60000/restart/60000/restart/60000'
  Pop $0

  ; 資料夾只給 SYSTEM、系統管理員與服務帳號（設定檔中可能有密碼）；其他使用者讀不到
  DetailPrint "設定 $DataDir 的權限"
  nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$DataDir" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "NT SERVICE\${SERVICE}:(OI)(CI)M" /Q'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "無法設定 $DataDir 的權限（icacls 結束碼 $0），請以系統管理員身分檢查。" /SD IDOK
  ${EndIf}

  ; 新增或移除程式
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "報表平台"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${UNINSTALL_KEY}" "EstimatedSize" "$0"

  DetailPrint "啟動服務 ${SERVICE}"
  nsExec::ExecToLog '"$SYSDIR\net.exe" start ${SERVICE}'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "服務 ${SERVICE} 沒有啟動（結束碼 $0）。原因見 $DataDir\logs\host.log；修改設定後以「Start-Service ${SERVICE}」啟動。" /SD IDOK
  ${EndIf}
SectionEnd

Section "Uninstall"
  !insertmacro StopService
  DetailPrint "刪除服務 ${SERVICE}"
  nsExec::ExecToLog '"$SYSDIR\sc.exe" delete ${SERVICE}'
  Pop $0
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\ms-playwright"
  RMDir /r "$INSTDIR\web"
  RMDir /r "$INSTDIR\config"
  Delete "$INSTDIR\tmig-server-host.exe"
  Delete "$INSTDIR\tmig.cmd"
  Delete "$INSTDIR\NOTICE.md"
  Delete "$INSTDIR\build-info.json"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "${UNINSTALL_KEY}"
  MessageBox MB_ICONINFORMATION "報表、設定與記錄保留在 $APPDATA\${SERVICE}，不需要時請自行刪除。" /SD IDOK
SectionEnd
