# Собирает MiniDock.exe (PyInstaller) и установщик (Inno Setup) → release\MiniDock-Setup-<версия>.exe
# Запуск: powershell -ExecutionPolicy Bypass -File build\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$ver = python -c "import sys; sys.path.insert(0, '.'); from minidock.version import __version__; print(__version__)"
$parts = ($ver.Split(".") + @("0", "0", "0"))[0..3] -join ", "
Write-Host "MiniDock $ver"

# сведения о версии для свойств MiniDock.exe
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'MiniDock'),
      StringStruct('FileDescription', 'MiniDock'),
      StringStruct('FileVersion', '$ver'),
      StringStruct('ProductName', 'MiniDock'),
      StringStruct('ProductVersion', '$ver'),
      StringStruct('OriginalFilename', 'MiniDock.exe')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"@ | Set-Content "$PSScriptRoot\version_info.txt" -Encoding utf8

python "$PSScriptRoot\make_icon.py" "$PSScriptRoot\MiniDock.ico" "$PSScriptRoot\MiniDock.png"
# Собираем в короткой папке: пути внутри сборки длинные, а Inno Setup не любит пути длиннее 260 символов
$out = Join-Path $env:LOCALAPPDATA "MiniDockBuild"
Remove-Item -Recurse -Force "$out\dist" -ErrorAction SilentlyContinue
python -m PyInstaller --noconfirm --clean --distpath "$out\dist" --workpath "$out\work" "$PSScriptRoot\MiniDock.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 не найден (winget install JRSoftware.InnoSetup)" }
New-Item -ItemType Directory -Force "$root\release" | Out-Null
& $iscc "/DMyAppVersion=$ver" "/DDistDir=$out\dist\MiniDock" "/O$out\release" "$PSScriptRoot\MiniDock.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Copy-Item "$out\release\MiniDock-Setup-$ver.exe" "$root\release\" -Force
Get-ChildItem "$root\release\MiniDock-Setup-$ver.exe" | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
