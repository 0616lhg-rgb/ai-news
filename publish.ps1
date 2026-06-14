# Daily auto-run: collect -> GitHub push (-> GitHub Pages auto-update)
# NOTE: kept ASCII-only on purpose. PowerShell 5.1 reads .ps1 as cp949 by
# default, so a Korean path literal here would break. Use $PSScriptRoot instead.
$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$dir = $PSScriptRoot
$py  = "C:\Users\remote\AppData\Local\Programs\Python\Python312\python.exe"
$git = "C:\Program Files\Git\cmd\git.exe"
$log = Join-Path $dir "collect.log"
$stateFile = Join-Path $dir "data\_state.json"

Set-Location -LiteralPath $dir

# 최근에 이미 성공적으로 돌았으면 건너뜀 (로그인 트리거 등으로 인한 중복/낭비 방지)
if (Test-Path $stateFile) {
  try {
    $last = (Get-Content $stateFile -Raw -Encoding utf8 | ConvertFrom-Json).last_run_utc
    if ($last) {
      $ageH = ((Get-Date).ToUniversalTime() - [datetime]::Parse($last).ToUniversalTime()).TotalHours
      if ($ageH -lt 4) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm') skip (last run $([math]::Round($ageH,1))h ago)" |
          Out-File -LiteralPath $log -Append -Encoding utf8
        exit 0
      }
    }
  } catch {}
}

"`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm') run =====" | Out-File -LiteralPath $log -Append -Encoding utf8

# 1) collect + digest + briefing
& $py collect.py *>> $log

# 2) commit & push (no-op safe if nothing changed)
& $git add -A
& $git commit -m "auto collect $(Get-Date -Format 'yyyy-MM-dd')" *>> $log
& $git push origin main *>> $log
"push exit: $LASTEXITCODE" | Out-File -LiteralPath $log -Append -Encoding utf8
