$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

$UvExe = Join-Path $PSScriptRoot ".tools\uv\uv.exe"
if (-not (Test-Path $UvExe)) {
    throw "Önce start.ps1 çalıştırılarak bağımlılıklar kurulmalıdır."
}

$Backend = Start-Process -FilePath $UvExe `
    -ArgumentList @("run", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $PSScriptRoot `
    -PassThru `
    -WindowStyle Hidden

try {
    Set-Location (Join-Path $PSScriptRoot "frontend")
    npm run dev
}
finally {
    if (-not $Backend.HasExited) {
        Stop-Process -Id $Backend.Id
    }
}
