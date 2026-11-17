param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$binDirectory = Join-Path $PSScriptRoot 'bin'

function Require-Command([string]$Name, [string]$HelpText) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw $HelpText }
    return $command.Source
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $Executable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) {
        throw ('{0} failed (exit {1}). Original output:{2}{3}' -f $Label, $exitCode, [Environment]::NewLine, ($output -join [Environment]::NewLine))
    }
    foreach ($line in $output) { Write-Host $line }
}

foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude')) {
    if (-not (Test-Path -LiteralPath (Join-Path $binDirectory "$name.cmd"))) { throw "Missing launcher: $name.cmd" }
}
$node = Require-Command 'node.exe' 'Node.js >=20.18.1 is required. Install Node.js with npm, reopen PowerShell and retry.'
$python = Require-Command 'python.exe' 'Python >=3.10 is required. Install Python with its PATH option and reopen PowerShell.'
$claude = Require-Command 'claude.exe' 'Native Claude Code (claude.exe) was not found in PATH. Install Claude Code and reopen PowerShell.'
Invoke-Checked $node @('-e', "const [a,b,c]=process.versions.node.split('.').map(Number); if(a<20||(a===20&&(b<18||(b===18&&c<1)))){console.error('Node.js >=20.18.1 is required; found '+process.versions.node);process.exit(1)};console.log('Node.js '+process.versions.node)") 'Node.js check'
Invoke-Checked $python @('-c', "import sys; print('Python ' + sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3,10) else 1)") 'Python >=3.10 check'
Invoke-Checked $claude @('--version') 'Native Claude Code version'

# Resolve from this checkout, rather than the caller's current directory.
Push-Location $PSScriptRoot
try {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $dependencyOutput = @(& $node --input-type=module -e "import('undici').catch(e=>{console.error(e.message);process.exit(1)})" 2>&1)
        $dependencyExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($dependencyExit -ne 0) {
        if ($CheckOnly) { throw ('npm dependencies are missing or unusable. Run npm.cmd ci in ' + $PSScriptRoot + '. Details: ' + ($dependencyOutput -join ' ')) }
        $npm = Require-Command 'npm.cmd' 'npm.cmd was not found. Repair Node.js with npm included.'
        Write-Host 'Installing locked npm dependencies...'
        Invoke-Checked $npm @('ci', '--ignore-scripts', '--no-audit') 'npm ci'
        Invoke-Checked $node @('--input-type=module', '-e', "import('undici')") 'GPT dependency check'
    }
} finally { Pop-Location }

Push-Location $env:TEMP
try {
    foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude')) {
        # Explicit paths avoid selecting an older launcher in PATH.
        Invoke-Checked (Join-Path $binDirectory "$name.cmd") @('--version') "$name --version"
    }
} finally { Pop-Location }
if ($CheckOnly) { Write-Host 'All checks passed. PATH was not changed.'; return }

# Change PATH only after all dependency and launcher checks succeeded.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$remaining = @($userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $binDirectory.TrimEnd('\') })
[Environment]::SetEnvironmentVariable('Path', (@($binDirectory) + $remaining -join ';'), 'User')
$env:Path = $binDirectory + ';' + $env:Path
if (-not ('ClaudeLauncherEnvironment' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ClaudeLauncherEnvironment {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
        string lParam, uint flags, uint timeout, out UIntPtr result);
}
'@
}
$broadcastResult = [UIntPtr]::Zero
[void][ClaudeLauncherEnvironment]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$broadcastResult)
Get-Command gpt_claude, ai_claude, ds_claude | Select-Object Name, Source
Write-Host 'Launchers installed. Open a new terminal and configure providers using README.md.'
