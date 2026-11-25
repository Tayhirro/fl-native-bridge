# fl-native-bridge - installer (Windows x64)
#
# The bridge ships a compiled extension built against the Python 3.12 runtime
# bundled with FL Studio, so it only runs on Windows x64. No virtual MIDI port is
# needed: the MCP server and the controller talk over a file-queue RPC.

$ErrorActionPreference = 'Stop'

Write-Host "fl-native-bridge - installer" -ForegroundColor Cyan
Write-Host ""

# Step 1: platform
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "This bridge requires 64-bit Windows."
}
Write-Host "[1/4] Windows x64 detected" -ForegroundColor Green

# Step 2: locate FL's Hardware folder
$hardwareRoot = Join-Path $env:USERPROFILE 'Documents\Image-Line\FL Studio\Settings\Hardware'
if (-not (Test-Path $hardwareRoot)) {
    throw "FL Studio's Settings folder was not found at $hardwareRoot. Start FL Studio once, then re-run."
}
$target = Join-Path $hardwareRoot 'FLStudioMCP'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Write-Host "[2/4] Target: $target" -ForegroundColor Green

# Step 3: copy the controller files
$files = @(
    'device_FLStudioMCP.py',
    'semantic_native.py',
    'bridge_extension.py',
    'native_rpc.py',
    '_flmcp_native.pyd'
)
$source = Join-Path $PSScriptRoot 'fl_controller'
foreach ($file in $files) {
    $from = Join-Path $source $file
    if (-not (Test-Path $from)) { throw "Missing fl_controller\$file" }
    Copy-Item $from (Join-Path $target $file) -Force
}
Write-Host "[3/4] 5 files copied (controller + native extension)" -ForegroundColor Green

# Step 4: Python dependencies
Write-Host "[4/4] Installing Python dependencies..." -ForegroundColor Yellow
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Install uv first: irm https://astral.sh/uv/install.ps1 | iex" -ForegroundColor Yellow
    throw "uv is not installed."
}
Push-Location $PSScriptRoot
try { & uv sync } finally { Pop-Location }
Write-Host "Dependencies installed" -ForegroundColor Green

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart FL Studio"
Write-Host "  2. Options -> MIDI settings: controller type FLStudioMCP, enable its port"
Write-Host "     (no virtual MIDI device is required)"
Write-Host "  3. Register the MCP server:  uv run fl-native-bridge"
Write-Host ""
Write-Host "The extension is version-locked; a different FL engine profile is refused."
