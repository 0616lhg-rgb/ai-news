# Daily auto-run: collect -> GitHub push (-> GitHub Pages auto-update)
# NOTE: kept ASCII-only on purpose. PowerShell 5.1 reads .ps1 as cp949 by
# default, so a Korean path literal here would break. Use $PSScriptRoot instead.
$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$dir = $PSScriptRoot
$py  = "C:\Users\remote\AppData\Local\Programs\Python\Python312\python.exe"
$git = "C:\Program Files\Git\cmd\git.exe"
$log = Join-Path $dir "collect.log"

Set-Location -LiteralPath $dir
"`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm') run =====" | Out-File -LiteralPath $log -Append -Encoding utf8

# 1) collect + digest + briefing
& $py collect.py *>> $log

# 2) commit & push (no-op safe if nothing changed)
& $git add -A
& $git commit -m "auto collect $(Get-Date -Format 'yyyy-MM-dd')" *>> $log
& $git push origin main *>> $log
"push exit: $LASTEXITCODE" | Out-File -LiteralPath $log -Append -Encoding utf8
