$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows.' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $PSScriptRoot '.venv'
        & uv sync --locked --python ">=3.11,<3.15"
        if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check the error above and retry.' }
    } finally {
        $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    }
} else {
    $python = $null
    $pythonArgs = @()
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info < (3, 15)))'
        if ($LASTEXITCODE -eq 0) { $python = 'python' }
    }
    if (-not $python -and (Get-Command py -ErrorAction SilentlyContinue)) {
        foreach ($version in @('-3.14', '-3.13', '-3.12', '-3.11')) {
            & py $version -c 'import sys; sys.exit(not ((3, 11) <= sys.version_info < (3, 15)))' 2>$null
            if ($LASTEXITCODE -eq 0) { $python = 'py'; $pythonArgs = @($version); break }
        }
    }
    if (-not $python) { throw 'Python 3.11 to 3.14 or uv is required: https://www.python.org/downloads/windows/' }
    & $python @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the local environment.' }
    & .\.venv\Scripts\python.exe -m pip install .
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Check the error above and retry.' }
}
$executable = Join-Path $PSScriptRoot '.venv\Scripts\deskpet-platform.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Installation finished without the DeskPet executable.' }
Write-Host "Pair: & `"$executable`" pair"
Write-Host "MCP:  & `"$executable`" mcp"
