$ErrorActionPreference = 'Stop'
$binDirectory = Join-Path $PSScriptRoot 'bin'
foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude')) {
    if (-not (Test-Path -LiteralPath (Join-Path $binDirectory "$name.cmd"))) {
        throw "Missing launcher: $name.cmd"
    }
}
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$alreadyPresent = @($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $binDirectory.TrimEnd('\') }).Count -gt 0
if (-not $alreadyPresent) {
    $newPath = if ([string]::IsNullOrWhiteSpace($userPath)) { $binDirectory } else { $userPath.TrimEnd(';') + ';' + $binDirectory }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}
if (-not @($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -ieq $binDirectory.TrimEnd('\') }).Count) {
    $env:Path = $env:Path.TrimEnd(';') + ';' + $binDirectory
}
# Notify Explorer so new terminals inherit the changed user PATH.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ClaudeLauncherEnvironment {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
        string lParam, uint flags, uint timeout, out UIntPtr result);
}
'@
$broadcastResult = [UIntPtr]::Zero
[void][ClaudeLauncherEnvironment]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero,
    'Environment', 2, 5000, [ref]$broadcastResult)
Get-Command gpt_claude, ai_claude, ds_claude | Select-Object Name, Source
Push-Location $env:TEMP
try {
    foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude')) {
        & $name --version
        if ($LASTEXITCODE -ne 0) { throw "$name --version failed: $LASTEXITCODE" }
    }
} finally {
    Pop-Location
}
Write-Output 'Launchers installed. Open a new terminal, then configure the providers described in README.md.'
