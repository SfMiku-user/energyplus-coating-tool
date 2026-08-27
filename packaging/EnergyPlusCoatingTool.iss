#define AppName "辐射制冷涂料建筑节能计算工具"
#define AppVersion "0.1.0-test"
#define AppPublisher "SfMiku-user"
#define AppExeName "EnergyPlusCoatingTool.exe"
#define PayloadDir SourcePath + "..\build\windows-x64\payload"
#define ReleaseDir SourcePath + "..\release"

[Setup]
AppId={{fd5777b1-53eb-4cd7-b667-2255945844cb}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\EnergyPlusCoatingTool
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=EnergyPlusCoatingTool-0.1.0-test-windows-x64-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
InfoBeforeFile={#PayloadDir}\THIRD_PARTY_NOTICES.txt
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动{#AppName}"; Flags: nowait postinstall skipifsilent
