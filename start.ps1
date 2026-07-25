$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

$UvVersion = "0.11.19"
$ToolsDir = Join-Path $PSScriptRoot ".tools"
$UvDir = Join-Path $ToolsDir "uv"
$UvExe = Join-Path $UvDir "uv.exe"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "FFmpeg PATH içinde bulunamadı. FFmpeg'i kurup terminali yeniden açın."
}

if (-not (Test-Path $UvExe)) {
    Write-Host "uv $UvVersion indiriliyor..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $Archive = Join-Path $ToolsDir "uv.zip"
    $Url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
    Invoke-WebRequest -Uri $Url -OutFile $Archive
    if (Test-Path $UvDir) {
        Remove-Item -LiteralPath $UvDir -Recurse -Force
    }
    Expand-Archive -Path $Archive -DestinationPath $UvDir
    Remove-Item -LiteralPath $Archive -Force
}

Write-Host "Python 3.12 ortamı hazırlanıyor..." -ForegroundColor Cyan
& $UvExe sync --python 3.12 --dev
if ($LASTEXITCODE -ne 0) { throw "Python bağımlılıkları kurulamadı." }

Write-Host "Web arayüzü hazırlanıyor..." -ForegroundColor Cyan
Push-Location (Join-Path $PSScriptRoot "frontend")
try {
    if (Test-Path "package-lock.json") {
        npm ci
    } else {
        npm install
    }
    if ($LASTEXITCODE -ne 0) { throw "Frontend bağımlılıkları kurulamadı." }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend derlenemedi." }
}
finally {
    Pop-Location
}

$Url = "http://127.0.0.1:8000"
Write-Host ""
Write-Host "Shorts Studio: $Url" -ForegroundColor Green
Write-Host "Kapatmak için bu pencerede Ctrl+C tuşlarına basın." -ForegroundColor DarkGray
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process $using:Url
} | Out-Null

& $UvExe run uvicorn app.main:app --host 127.0.0.1 --port 8000
