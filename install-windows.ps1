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

function Refresh-ProcessPath {
    $paths = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path)
    $env:Path = (($paths -join ';') -split ';' | Where-Object { $_ } | Select-Object -Unique) -join ';'
}

function Get-RuntimeCandidates([string]$Kind) {
    $commandName = @{ Node = 'node.exe'; Python = 'python.exe'; Git = 'git.exe'; Claude = 'claude.exe' }[$Kind]
    $commands = @(Get-Command $commandName -All -CommandType Application -ErrorAction SilentlyContinue)
    $paths = @($commands | ForEach-Object { $_.Source })
    switch ($Kind) {
        'Node' {
            $paths += @("$env:ProgramFiles\nodejs\node.exe", "$env:LOCALAPPDATA\Programs\nodejs\node.exe")
        }
        'Python' {
            $directories = @(Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python*", "$env:ProgramFiles\Python*" -Directory -ErrorAction SilentlyContinue)
            $paths += @($directories | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' })
        }
        'Git' {
            $paths += @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
        }
        'Claude' {
            $paths += @("$env:USERPROFILE\.local\bin\claude.exe", "$env:LOCALAPPDATA\Microsoft\WinGet\Links\claude.exe")
        }
    }
    foreach ($candidate in ($paths | Where-Object { $_ } | Select-Object -Unique)) {
        # Windows' Store stub can open the Store instead of running Python.
        if ($Kind -eq 'Python' -and $candidate -match '\\Microsoft\\WindowsApps\\') { continue }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
    }
}

function Test-Runtime([string]$Kind, [string]$Executable) {
    [string[]]$arguments = @(switch ($Kind) {
        'Node' { @('-e', "const [a,b,c]=process.versions.node.split('.').map(Number);process.exit(a>20||(a===20&&(b>18||(b===18&&c>=1)))?0:1)") }
        'Python' { @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)') }
        default { @('--version') }
    })
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $ignored = @(& $Executable @arguments 2>&1)
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
    finally { $ErrorActionPreference = $previousPreference }
}

function Find-Runtime([string]$Kind) {
    foreach ($candidate in @(Get-RuntimeCandidates $Kind)) {
        if (Test-Runtime $Kind $candidate) { return $candidate }
    }
    return $null
}

function Install-WinGetPackage([string]$PackageId, [string]$DisplayName, [switch]$UserScope) {
    $winget = Require-Command 'winget.exe' ('WinGet is required to install ' + $DisplayName + '. Install or update App Installer from https://aka.ms/getwinget, then run this installer again.')
    Write-Host ('Installing ' + $DisplayName + ' using WinGet...')
    $arguments = @('install', '--id', $PackageId, '--exact', '--source', 'winget', '--silent',
                   '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if ($UserScope) { $arguments += @('--scope', 'user') }
    Invoke-Checked $winget $arguments ('Install ' + $DisplayName)
    Refresh-ProcessPath
}

function Ensure-Runtime([string]$Kind, [string]$PackageId, [string]$DisplayName, [switch]$CheckOnly, [switch]$UserScope) {
    $runtime = Find-Runtime $Kind
    if ($runtime) { return $runtime }
    if ($CheckOnly) { throw ($DisplayName + ' is required. Run install-windows.ps1 without -CheckOnly to install it.') }
    Install-WinGetPackage $PackageId $DisplayName -UserScope:$UserScope
    $runtime = Find-Runtime $Kind
    if (-not $runtime) { throw ($DisplayName + ' was installed but a compatible executable is not available. Restart Windows if requested by the installer, then run install-windows.ps1 again.') }
    return $runtime
}

# Dot-sourcing exposes the dependency checks for offline tests; it installs nothing.
if ($MyInvocation.InvocationName -eq '.') { return }

foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude')) {
    if (-not (Test-Path -LiteralPath (Join-Path $binDirectory "$name.cmd"))) { throw "Missing launcher: $name.cmd" }
}
Refresh-ProcessPath
$node = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js >=20.18.1 with npm' -CheckOnly:$CheckOnly
$python = Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python >=3.10' -CheckOnly:$CheckOnly -UserScope
$git = Ensure-Runtime 'Git' 'Git.Git' 'Git for Windows' -CheckOnly:$CheckOnly
$claude = Ensure-Runtime 'Claude' 'Anthropic.ClaudeCode' 'Native Claude Code' -CheckOnly:$CheckOnly -UserScope
# Use the validated programs even if this terminal still has an older version first.
$runtimeDirectories = @($node, $python, $git, $claude) | ForEach-Object { Split-Path -Parent $_ } | Select-Object -Unique
$env:Path = (@($runtimeDirectories) + @($env:Path) -join ';')
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
$installPaths = @($binDirectory) + @($runtimeDirectories)
$remaining = @($userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -notin @($installPaths | ForEach-Object { $_.TrimEnd('\') }) })
[Environment]::SetEnvironmentVariable('Path', (@($installPaths) + $remaining -join ';'), 'User')
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
Write-Host 'DeepSeek on a fresh installation: set DEEPSEEK_API_KEY, then run ds_claude.'
