# 매일 자동 실행용: 수집 → GitHub push (→ GitHub Pages 자동 반영)
$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$dir = "C:\Users\remote\Desktop\AI 뉴스"
$py  = "C:\Users\remote\AppData\Local\Programs\Python\Python312\python.exe"
$git = "C:\Program Files\Git\cmd\git.exe"
$log = Join-Path $dir "collect.log"

Set-Location $dir
"`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm') 자동 실행 =====" | Out-File $log -Append -Encoding utf8

# 1) 수집 + 다이제스트 + 브리핑
& $py collect.py *>> $log

# 2) 변경분 커밋 & 푸시 (변경 없으면 조용히 통과)
& $git add -A
& $git commit -m "자동 수집 $(Get-Date -Format 'yyyy-MM-dd')" *>> $log
& $git push origin main *>> $log
"push 종료코드: $LASTEXITCODE" | Out-File $log -Append -Encoding utf8
