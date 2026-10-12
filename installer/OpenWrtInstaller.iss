; Установщик Windows (Inno Setup 6): один Setup.exe — программа с встроенными прошивками,
; ярлыки в «Пуске» и на рабочем столе, удаление через «Приложения» Windows.
; Сборка: scripts\build_windows.bat (сам найдёт Inno Setup) или вручную:
;   ISCC /DAppVersion=1.4.0 /DExeDir=..\dist installer\OpenWrtInstaller.iss

#ifndef AppVersion
  #define AppVersion "1.4.0"
#endif
#ifndef ExeDir
  #define ExeDir "..\dist"
#endif
#ifndef OutName
  #define OutName "OpenWrtInstaller-Setup-" + AppVersion
#endif
#define AppName "OpenWrt Installer SNR AX2"
#define ExeName "OpenWrtInstaller.exe"

[Setup]
AppId={{6F1C7B9A-3D2E-4C8B-9A51-0B7E2D4F8A13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OpenWrt Installer contributors
AppPublisherURL=https://github.com/igor-chelapin/panel-setup
DefaultDirName={autopf}\OpenWrt Installer
DefaultGroupName=OpenWrt Installer
DisableProgramGroupPage=yes
DisableDirPage=auto
LicenseFile=..\LICENSE
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
OutputDir=..\release
OutputBaseFilename={#OutName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; программе всё равно нужны права администратора (смена IP, брандмауэр)
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=6.1sp1
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.DesktopIcon=Ярлык на рабочем столе
en.DesktopIcon=Desktop shortcut
ru.RunApp=Запустить {#AppName}
en.RunApp=Launch {#AppName}
ru.AskDeleteData=Удалить также настройки, журналы и бэкапы роутера?%n%n%1%n%nВНИМАНИЕ: в бэкапах лежит заводская прошивка и калибровка Wi-Fi вашего роутера. Если вы их никуда не копировали — нажмите «Нет».
en.AskDeleteData=Also delete settings, logs and router backups?%n%n%1%n%nWARNING: backups contain the factory firmware and Wi-Fi calibration of your router. If you have not copied them elsewhere, press "No".

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"

[Files]
Source: "{#ExeDir}\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:RunApp}"; Flags: nowait postinstall skipifsilent shellexec

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Dir := ExpandConstant('{localappdata}\OpenWrtInstaller');
    if DirExists(Dir) and not UninstallSilent then
      if MsgBox(FmtMessage(CustomMessage('AskDeleteData'), [Dir]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Dir, True, True, True);
  end;
end;
