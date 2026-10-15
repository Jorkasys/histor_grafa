<#
.SYNOPSIS
    Собирает комплект historian2grafana для компьютера без интернета.

.DESCRIPTION
    Запускается на компьютере С интернетом. Скачивает Python (переносной, без
    установки), библиотеки приложения, PostgreSQL и Grafana для Windows и
    складывает всё вместе с приложением в одну папку и архив:

        dist\historian2grafana-offline.zip

    Архив переносится на компьютер без интернета, распаковывается, и там
    приложение запускается файлом start.bat. Docker не нужен.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File offline\build-offline.ps1
#>
param(
    [string]$PythonVersion = '3.13.16',
    [string]$PostgresVersion = '16.15-1',
    [string]$GrafanaVersion = '11.6.0',
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # иначе Invoke-WebRequest качает в разы медленнее
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Project = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Project 'dist' }
$Cache = Join-Path $OutDir 'downloads'
$Bundle = Join-Path $OutDir 'historian2grafana-offline'
$Zip = "$Bundle.zip"
$Unpack = Join-Path $OutDir 'unpack'

function Step([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Fetch([string]$Url, [string]$Name) {
    $file = Join-Path $Cache $Name
    if (Test-Path $file) {
        Write-Host "   уже скачан: $Name"
        return $file
    }
    Write-Host "   скачиваю $Url"
    $part = "$file.part"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
    } catch {
        throw "не удалось скачать $Url : $($_.Exception.Message)"
    }
    Move-Item $part $file
    return $file
}

function Exists([string]$Path) {
    # Только что удалённая папка несколько секунд «удаляется», и Test-Path
    # отвечает «Access denied» — считаем, что она ещё есть
    try { return (Test-Path $Path) } catch { return $true }
}

function Wipe([string]$Path) {
    # Remove-Item в Windows PowerShell 5.1 спотыкается о глубокие деревья папок
    for ($i = 0; $i -lt 15 -and (Exists $Path); $i++) {
        cmd /c "rmdir /s /q `"$Path`" 2>nul"
        if (Exists $Path) { Start-Sleep -Seconds 2 }
    }
    if (Exists $Path) { throw "не удалось удалить $Path — закройте программы, которые его используют" }
}

function Unzip([string]$Archive, [string]$Destination) {
    Wipe $Destination
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Destination)
}

function UnzipPostgres([string]$Archive, [string]$Destination) {
    # Из архива EDB берём только сервер (bin, lib, share) и лицензии:
    # pgAdmin, StackBuilder, документация и символы не нужны
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if ($name.EndsWith('/')) { continue }
            if ($name -notmatch '^pgsql/((bin|lib|share)/.+|[^/]+\.txt)$') { continue }
            $target = Join-Path $Destination $name.Substring(6).Replace('/', '\')
            $dir = Split-Path $target
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally {
        $zip.Dispose()
    }
}

function Run([string]$Exe, [string[]]$Arguments) {
    # Предупреждения программ в stderr — не повод останавливать сборку: смотрим код выхода
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments
    $code = $LASTEXITCODE
    $ErrorActionPreference = $saved
    if ($code -ne 0) { throw "команда завершилась с ошибкой ($code): $Exe $($Arguments -join ' ')" }
}

New-Item -ItemType Directory -Force $Cache | Out-Null
Wipe $Bundle
Wipe $Unpack
New-Item -ItemType Directory -Force "$Bundle\runtime" | Out-Null

# --------------------------------------------------------------- Python
Step "Python $PythonVersion (переносной, без установки)"
$pyZip = Fetch "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
               "python-$PythonVersion-embed-amd64.zip"
$PyDir = "$Bundle\runtime\python"
Unzip $pyZip $PyDir
# Переносной Python по умолчанию не видит библиотеки и папку приложения:
# разрешаем Lib\site-packages и корень комплекта (..\..)
$pth = Get-ChildItem $PyDir -Filter 'python*._pth' | Select-Object -First 1
$stdlib = (Get-Content $pth.FullName | Where-Object { $_ -match '\.zip$' }) -join "`r`n"
Set-Content $pth.FullName -Encoding ASCII -Value "$stdlib`r`n.`r`nLib\site-packages`r`n..\..`r`nimport site"

Step 'Библиотеки приложения'
$getPip = Fetch 'https://bootstrap.pypa.io/get-pip.py' 'get-pip.py'
$python = "$PyDir\python.exe"
Run $python @($getPip, '--no-warn-script-location', '--disable-pip-version-check', '--quiet')
Run $python @('-m', 'pip', 'install', '--no-warn-script-location', '--disable-pip-version-check',
              '--quiet', '--no-cache-dir', '-r', "$Project\requirements.txt")
# Без интернета pip на целевом компьютере не пригодится
Run $python @('-m', 'pip', 'uninstall', '--yes', '--quiet', 'pip')

# ----------------------------------------------------------- PostgreSQL
Step "PostgreSQL $PostgresVersion"
$pgZip = Fetch "https://get.enterprisedb.com/postgresql/postgresql-$PostgresVersion-windows-x64-binaries.zip" `
               "postgresql-$PostgresVersion-windows-x64-binaries.zip"
UnzipPostgres $pgZip "$Bundle\runtime\pgsql"
if (-not (Test-Path "$Bundle\runtime\pgsql\bin\pg_ctl.exe")) {
    throw 'в архиве PostgreSQL не найден bin\pg_ctl.exe'
}
# PostgreSQL нужна библиотека Visual C++. На чистой Windows её может не быть —
# кладём рядом (так Microsoft разрешает распространять эти файлы)
foreach ($dll in 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll') {
    $source = "$env:WINDIR\System32\$dll"
    if (Test-Path $source) {
        Copy-Item $source "$Bundle\runtime\pgsql\bin\$dll" -Force
    } else {
        Write-Warning "$dll не найден на этом компьютере — на целевом нужен Visual C++ Redistributable"
    }
}

# --------------------------------------------------------------- Grafana
Step "Grafana $GrafanaVersion"
$gfZip = Fetch "https://dl.grafana.com/oss/release/grafana-$GrafanaVersion.windows-amd64.zip" `
               "grafana-$GrafanaVersion.windows-amd64.zip"
Unzip $gfZip $Unpack
$top = Get-ChildItem $Unpack -Directory | Select-Object -First 1
Move-Item $top.FullName "$Bundle\runtime\grafana"
Wipe $Unpack
# Документация, storybook и карты исходников для работы не нужны
foreach ($extra in 'docs', 'storybook', 'npm-artifacts', 'packaging', 'public\test') {
    Wipe "$Bundle\runtime\grafana\$extra"
}
Get-ChildItem "$Bundle\runtime\grafana\public" -Recurse -File -Filter '*.map' | Remove-Item -Force

# ------------------------------------------------------------ приложение
Step 'Приложение'
foreach ($dir in 'app', 'web', 'grafana', 'docker') {
    Copy-Item "$Project\$dir" "$Bundle\$dir" -Recurse
}
foreach ($dir in 'app', 'web', 'grafana', 'docker') {
    Get-ChildItem "$Bundle\$dir" -Directory -Recurse -Filter '__pycache__' |
        ForEach-Object { Wipe $_.FullName }
}
Copy-Item "$Project\offline\launcher.py" $Bundle
Copy-Item "$Project\offline\*.bat" $Bundle
Copy-Item "$Project\README.md", "$Project\LICENSE" $Bundle
Copy-Item "$Project\.env.example" "$Bundle\.env.example"

Step 'Проверка: библиотеки импортируются'
Push-Location $Bundle
try {
    # Без кавычек внутри: Windows PowerShell 5.1 теряет их при передаче программе
    Run $python @('-c', 'import app.main, asyncpg, pyodbc, pymssql, uvicorn')
    Write-Host '   ok'
} finally {
    Pop-Location
}

# ----------------------------------------------------------------- архив
Step 'Архив'
if (Test-Path $Zip) { Remove-Item $Zip -Force }
[IO.Compression.ZipFile]::CreateFromDirectory($Bundle, $Zip,
    [IO.Compression.CompressionLevel]::Optimal, $true)
$size = [math]::Round((Get-Item $Zip).Length / 1MB)
Write-Host ''
Write-Host "Готово: $Zip ($size МБ)" -ForegroundColor Green
Write-Host 'Перенесите архив на компьютер без интернета, распакуйте в C:\ и запустите start.bat'
