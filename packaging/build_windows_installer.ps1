param(
    [string]$OpenStudioRoot = "D:\OpenStudio-3.11.0",
    [string]$EnergyPlusRoot = "D:\energe plus\energe plus",
    [string]$PythonExecutable = "python",
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "build\windows-x64"))
$allowedBuildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "build"))
$payload = Join-Path $buildRoot "payload"
$releaseDir = Join-Path $projectRoot "release"

if ([string]::IsNullOrWhiteSpace($InnoCompiler)) {
    $innoCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $InnoCompiler = $innoCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($InnoCompiler)) {
        throw "未找到 Inno Setup 6 编译器 ISCC.exe。请安装后通过 -InnoCompiler 指定路径。"
    }
}

if (-not $buildRoot.StartsWith($allowedBuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝清理项目 build 目录之外的路径：$buildRoot"
}

$requiredFiles = @(
    (Join-Path $OpenStudioRoot "bin\openstudio.exe"),
    (Join-Path $OpenStudioRoot "LICENSE.md"),
    (Join-Path $EnergyPlusRoot "energyplus.exe"),
    (Join-Path $EnergyPlusRoot "LICENSE.txt"),
    $InnoCompiler
)
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "缺少打包所需文件：$required"
    }
}

if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $payload, $releaseDir | Out-Null

& $PythonExecutable -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name EnergyPlusCoatingTool `
    --distpath (Join-Path $buildRoot "app") `
    --workpath (Join-Path $buildRoot "pyinstaller") `
    --specpath (Join-Path $buildRoot "spec") `
    --add-data "$projectRoot\model_builder\openstudio_worker.py;model_builder" `
    --add-data "$projectRoot\model_builder\geometry_quality.py;model_builder" `
    (Join-Path $projectRoot "app.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $buildRoot "app\EnergyPlusCoatingTool.exe") -Destination $payload
foreach ($directory in @("templates", "sample_projects", "examples")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $directory) -Destination $payload -Recurse
}
foreach ($file in @("README.md", "测试版使用说明.md", "THIRD_PARTY_NOTICES.txt", "settings.example.json")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $payload
}

$runtimeRoot = Join-Path $payload "runtimes"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

function Copy-RuntimeTree([string]$source, [string]$destination) {
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    & robocopy $source $destination /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NP
    if ($LASTEXITCODE -gt 7) {
        throw "复制运行时失败：$source，robocopy退出码：$LASTEXITCODE"
    }
}

Copy-RuntimeTree $OpenStudioRoot (Join-Path $runtimeRoot "OpenStudio-3.11.0")
Copy-RuntimeTree $EnergyPlusRoot (Join-Path $runtimeRoot "EnergyPlus-26.1.0")

& $InnoCompiler /Qp (Join-Path $PSScriptRoot "EnergyPlusCoatingTool.iss")
if ($LASTEXITCODE -ne 0) {
    throw "安装包生成失败，退出码：$LASTEXITCODE"
}

$installer = Join-Path $releaseDir "EnergyPlusCoatingTool-0.1.0-test-windows-x64-setup.exe"
$hash = Get-FileHash -LiteralPath $installer -Algorithm SHA256
@(
    "SHA256  $($hash.Hash)",
    "FILE    $($hash.Path)",
    "OpenStudio 3.11.0 bundled unmodified",
    "EnergyPlus 26.1.0 bundled unmodified"
) | Set-Content -LiteralPath (Join-Path $releaseDir "SHA256SUMS.txt") -Encoding UTF8

Write-Host "INSTALLER=$installer"
Write-Host "SHA256=$($hash.Hash)"
