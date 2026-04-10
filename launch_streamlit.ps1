param(
    [switch]$ServerMode,
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$app = Join-Path $repoRoot "streamlit_app.py"
$healthUrl = "http://127.0.0.1:$Port/_stcore/health"
$appUrl = "http://127.0.0.1:$Port/"

function Show-ErrorDialog {
    param([string]$Message)

    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($Message, "calc_mam_hour", "OK", "Error") | Out-Null
}

function Test-AppReady {
    param([string]$Url)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "ok"
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    Show-ErrorDialog ".venv\Scripts\python.exe was not found."
    exit 1
}

if (-not (Test-Path -LiteralPath $app)) {
    Show-ErrorDialog "streamlit_app.py was not found."
    exit 1
}

if ($ServerMode) {
    Set-Location -LiteralPath $repoRoot
    & $python -m streamlit run $app --server.address 127.0.0.1 --server.port $Port --server.headless true
    exit $LASTEXITCODE
}

if (Test-AppReady -Url $healthUrl) {
    Start-Process $appUrl
    exit 0
}

Start-Process -FilePath "powershell.exe" -WorkingDirectory $repoRoot -WindowStyle Minimized -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$PSCommandPath`"",
    "-ServerMode",
    "-Port", "$Port"
) | Out-Null

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-AppReady -Url $healthUrl) {
        Start-Process $appUrl
        exit 0
    }
}

Show-ErrorDialog "Timed out waiting for Streamlit to start on port $Port."
exit 1
