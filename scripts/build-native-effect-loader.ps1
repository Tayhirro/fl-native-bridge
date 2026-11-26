param(
    [Parameter(Mandatory)]
    [string] $PythonInclude,
    [string] $OutputDirectory,
    [string] $ModuleSuffix = ''
)

$ErrorActionPreference = 'Stop'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PSScriptRoot '..\fl_controller'
}
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\fl_controller\native')).Path
$PythonInclude = (Resolve-Path -LiteralPath $PythonInclude).Path
if (-not (Test-Path -LiteralPath (Join-Path $PythonInclude 'Python.h'))) {
    throw 'PythonInclude must contain CPython headers.'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$importLibrary = Join-Path $OutputDirectory ('libflmcp-' + [guid]::NewGuid().ToString('N') + '.a')
$moduleName = '_flmcp_native'
if ($ModuleSuffix) {
    if ($ModuleSuffix -notmatch '^[A-Za-z0-9_]+$') { throw 'Invalid module suffix.' }
    $moduleName += '_' + $ModuleSuffix
}
$extension = Join-Path $OutputDirectory ($moduleName + '.pyd')
$moduleFlags = @()
if ($ModuleSuffix) {
    # Pass a bare identifier token; bridge.c stringifies it into the module
    # name and pastes it into PyInit_<token>. Quoted -D values are mangled by
    # PowerShell 7 -> GCC argument escaping on Windows runners.
    $moduleFlags = @("-DFLMCP_MODULE_NAME_TOKEN=$moduleName")
} else {
    $moduleFlags = @()
}

try {
    & dlltool.exe -m i386:x86-64 -d (Join-Path $source 'python312.def') -l $importLibrary
    if ($LASTEXITCODE -ne 0) { throw 'Failed to build the CPython import library.' }
    & gcc.exe -std=c17 -Wall -Wextra -Werror -O2 -shared @moduleFlags "-I$PythonInclude" (Join-Path $source 'bridge.c') (Join-Path $source 'dependencies.c') (Join-Path $source 'mixer_slots.c') (Join-Path $source 'channels.c') (Join-Path $source 'playlist.c') (Join-Path $source 'pattern_notes.c') (Join-Path $source 'timeline.c') (Join-Path $source 'automation.c') $importLibrary -ladvapi32 -o $extension
    if ($LASTEXITCODE -ne 0) { throw 'Failed to build the FL native extension.' }
} finally {
    Remove-Item -LiteralPath $importLibrary -ErrorAction SilentlyContinue
}
Write-Output $extension

