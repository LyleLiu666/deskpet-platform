$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows.' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check the error above and retry.' }
} else {
    $python = $null
    $pythonArgs = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'
        if ($LASTEXITCODE -eq 0) { $python = 'py'; $pythonArgs = @('-3') }
    }
    if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
        & python -c 'import sys; sys.exit(sys.version_info < (3, 11))'
        if ($LASTEXITCODE -eq 0) { $python = 'python' }
    }
    if (-not $python) { throw 'Python 3.11 or newer or uv is required: https://www.python.org/downloads/windows/' }
    & $python @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local environment.' }
    & .\.venv\Scripts\python.exe -m pip install .
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check the error above and retry.' }
}
$executable = Join-Path $PSScriptRoot '.venv\Scripts\deskpet-platform.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Installation finished without the DeskPet executable.' }
Write-Host "Pair: & `"$executable`" pair"
Write-Host "MCP:  & `"$executable`" mcp"
