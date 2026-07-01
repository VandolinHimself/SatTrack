# Verify web dashboard files exist (run from repo root on Windows).
$root = Split-Path -Parent $PSScriptRoot
$required = @(
    "sattrack\live.py",
    "sattrack\status_store.py",
    "sattrack\web\app.py",
    "sattrack\web\static\index.html",
    "sattrack\web\static\css\style.css",
    "sattrack\web\static\js\app.js",
    "run.py",
    "requirements.txt"
)
Write-Host "Checking $root ..."
$missing = @()
foreach ($rel in $required) {
    $p = Join-Path $root $rel
    if (Test-Path $p) {
        $i = Get-Item $p
        Write-Host "  OK  $($i.Length) bytes  $rel"
    } else {
        Write-Host "  MISSING  $rel" -ForegroundColor Red
        $missing += $rel
    }
}
if ($missing.Count) {
    Write-Host "`nMissing $($missing.Count) file(s)." -ForegroundColor Red
    exit 1
}
Write-Host "`nAll dashboard files present."
