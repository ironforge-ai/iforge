!macro customUnInstall
  System::Call 'Kernel32::SetEnvironmentVariable(t "IFORGE_NO_AUTO_UPDATE", t "1")i.r0'
  System::Call 'Kernel32::SetEnvironmentVariable(t "IFORGE_NO_UPDATE_CHECK", t "1")i.r0'
  ; Keep the unit as Sync intent during upgrade, remove it on real uninstall.
  ${if} ${isUpdated}
    nsExec::ExecToStack '"$INSTDIR\resources\native\iforge.exe" daemon stop'
  ${else}
    nsExec::ExecToStack '"$INSTDIR\resources\native\iforge.exe" daemon uninstall'
  ${endif}
  Pop $0
  Pop $1
  ${if} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Could not stop iforge Sync. Disable Sync and retry." /SD IDOK
    SetErrorLevel 1
    Abort
  ${endif}
  ${ifNot} ${isUpdated}
    nsExec::ExecToStack 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\resources\support\windows-cli-path.ps1" -Action Remove -BinPath "$LOCALAPPDATA\iforge\bin" -LauncherPath "$LOCALAPPDATA\iforge\bin\iforge.cmd"'
    Pop $0
    Pop $1
    ${if} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "Could not remove the iforge command from your PATH. Retry uninstalling iforge." /SD IDOK
      SetErrorLevel 1
      Abort
    ${endif}
  ${endif}
!macroend
