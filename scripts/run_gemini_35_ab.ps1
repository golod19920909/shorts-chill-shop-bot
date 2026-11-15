param(
    [ValidateSet("minimal", "low", "medium", "high")]
    [string]$TurnThinkingLevel = "minimal",
    [ValidateSet("minimal", "low", "medium", "high")]
    [string]$ChatActionThinkingLevel = "minimal"
)

$ErrorActionPreference = "Stop"

# Run the full frozen consultant dialogue benchmark against Gemini 3.1 and 3.5.
# The API key is read from the local .env file and is never printed or written
# to the benchmark artifacts.

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envFile = Join-Path $repoRoot ".env"
if (-not (Test-Path $envFile)) {
    throw "Missing .env in repository root."
}

$keyLine = Get-Content $envFile | Where-Object {
    $_ -match '^\s*GEMINI_API_KEY\s*=' -and $_ -notmatch '^\s*#'
} | Select-Object -First 1

if (-not $keyLine) {
    throw "GEMINI_API_KEY was not found in .env."
}

$apiKey = ($keyLine -replace '^\s*GEMINI_API_KEY\s*=\s*', '').Trim()
if (($apiKey.StartsWith('"') -and $apiKey.EndsWith('"')) -or
    ($apiKey.StartsWith("'") -and $apiKey.EndsWith("'"))) {
    $apiKey = $apiKey.Substring(1, $apiKey.Length - 2)
}
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "GEMINI_API_KEY in .env is empty."
}

$env:GEMINI_API_KEY = $apiKey
$env:EVAL_GEMINI_MODEL = "gemini-3.1-flash-lite"
$env:EVAL_GEMINI_LITE_MODEL = "gemini-3.5-flash-lite"
$env:EVAL_GEMINI_CONVERSATION_TURN_THINKING_LEVEL = $TurnThinkingLevel
$env:EVAL_GEMINI_CHAT_ACTION_THINKING_LEVEL = $ChatActionThinkingLevel

$reportsDir = Join-Path $repoRoot "reports"
New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$output = Join-Path $reportsDir "gemini-31-vs-35-dialogue-$stamp.json"
$mapOutput = Join-Path $reportsDir "gemini-31-vs-35-dialogue-map-$stamp.json"

python tests/eval_consultant_benchmark_runner.py `
    --gemini-only `
    --repeat 2 `
    --output $output `
    --map-output $mapOutput

if ($LASTEXITCODE -ne 0) {
    throw "Gemini 3.1 vs 3.5 dialogue benchmark failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Benchmark finished. Results:"
Write-Host $output
Write-Host "Blind identity map:"
Write-Host $mapOutput
