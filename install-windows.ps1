param([switch]$CheckOnly, [switch]$RepairWinGetAllUsers, [string]$WinGetRepairResult)

$ErrorActionPreference = 'Stop'
$binDirectory = Join-Path $PSScriptRoot 'bin'
$installerScript = $PSCommandPath

function Require-Command([string]$Name, [string]$HelpText) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) { throw $HelpText }
    return $command.Source
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label) {
    $previousPreference = $ErrorActionPreference
    $previousConsoleEncoding = [Console]::OutputEncoding
    $previousOutputEncoding = $OutputEncoding
    try {
        # WinGet writes UTF-8 to redirected stdout, while Windows PowerShell 5.1
        # otherwise decodes it with the console's OEM code page (e.g. CP866).
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [Console]::OutputEncoding = $utf8
        $OutputEncoding = $utf8
        $ErrorActionPreference = 'Continue'
        $output = @(& $Executable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $previousConsoleEncoding
        $OutputEncoding = $previousOutputEncoding
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        $policyBlocked = $exitCode -in @(-1978334961, 1625, -2147023271)
        $explanation = if ($policyBlocked) {
            ' Windows blocked this installation by policy (MSI 1625 / WinGet 0x8A15010F). Ask the device administrator to inspect the installer log below and approve an allowed installation method. Automatic retries stopped.'
        } else { '' }
        $message = '{0} failed (exit {1}).{2} Original output:{3}{4}' -f $Label, $exitCode, $explanation, [Environment]::NewLine, ($output -join [Environment]::NewLine)
        $failure = New-Object System.InvalidOperationException($message)
        $failure.Data['NativeExitCode'] = $exitCode
        $failure.Data['PolicyBlocked'] = $policyBlocked
        throw $failure
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

function Get-MachinePythonCandidates {
    # PEP 514 registrations cover machine installations in custom directories.
    $paths = @()
    foreach ($root in @('HKLM:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
        foreach ($versionKey in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
            $key = Get-Item -LiteralPath (Join-Path $versionKey.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($key) {
                $paths += $key.GetValue('ExecutablePath')
                $directory = $key.GetValue('')
                if ($directory) { $paths += Join-Path $directory 'python.exe' }
            }
        }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }) {
        $paths += @(Get-ChildItem -Path (Join-Path $root 'Python*') -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' })
    }
    foreach ($candidate in ($paths | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
    }
}

function Find-Runtime([string]$Kind, [switch]$MachineScope) {
    $candidates = if ($Kind -eq 'Python' -and $MachineScope) { @(Get-MachinePythonCandidates) } else { @(Get-RuntimeCandidates $Kind) }
    foreach ($candidate in $candidates) {
        if (Test-Runtime $Kind $candidate) { return $candidate }
    }
    return $null
}

function Get-WinGetCandidates {
    $paths = @(Get-Command 'winget.exe' -All -CommandType Application -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    $paths += "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    if (Get-Command 'Get-AppxPackage' -ErrorAction SilentlyContinue) {
        try {
            $paths += @(Get-AppxPackage -Name 'Microsoft.DesktopAppInstaller' -ErrorAction Stop |
                Where-Object { $_.InstallLocation } | ForEach-Object { Join-Path $_.InstallLocation 'winget.exe' })
        } catch { Write-Verbose ('App Installer discovery: ' + $_.Exception.Message) }
    }
    foreach ($candidate in ($paths | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate }
    }
}

function Test-WinGet([string]$Executable) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $Executable --version 2>&1)
        if ($LASTEXITCODE -ne 0) { return $false }
        $versionText = ($output -join ' ').Trim()
        if ($versionText -notmatch '^v?(\d+\.\d+\.\d+)') { return $false }
        return ([version]$Matches[1] -ge [version]'1.8.0')
    } catch { return $false }
    finally { $ErrorActionPreference = $previousPreference }
}

function Find-WinGet {
    foreach ($candidate in @(Get-WinGetCandidates)) {
        if (Test-WinGet $candidate) { return $candidate }
    }
    return $null
}

function Register-WinGet {
    if (Get-Command 'Add-AppxPackage' -ErrorAction SilentlyContinue) {
        try {
            Write-Host 'Registering App Installer for the current Windows user...'
            Add-AppxPackage -RegisterByFamilyName -MainPackage 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe' -ErrorAction Stop | Out-Null
        } catch { Write-Verbose ('App Installer registration: ' + $_.Exception.Message) }
    }
}

function Test-InstallerAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } finally { $identity.Dispose() }
}

function Install-WinGetClient([switch]$AllUsers) {
    Write-Host 'Installing WinGet using the Microsoft.WinGet.Client module from PowerShell Gallery...'
    $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
        if (-not (Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue |
                  Where-Object { $_.Version -ge [version]'2.8.5.201' })) {
            Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -Scope CurrentUser -Force -Confirm:$false -ErrorAction Stop | Out-Null
        }
        $gallery = Get-PSRepository -Name PSGallery -ErrorAction SilentlyContinue
        if (-not $gallery) {
            Register-PSRepository -Default -ErrorAction Stop
            $gallery = Get-PSRepository -Name PSGallery -ErrorAction Stop
        }
        if ($gallery.SourceLocation.TrimEnd('/') -ine 'https://www.powershellgallery.com/api/v2') {
            throw 'PSGallery points to a nonstandard source. Restore the official PowerShell Gallery before continuing.'
        }
        Install-Module -Name Microsoft.WinGet.Client -Repository PSGallery -Scope CurrentUser -Force -AllowClobber -Confirm:$false -ErrorAction Stop
        # Import only repair: the module also exports Install-WinGetPackage,
        # which must not replace this installer's dependency wrapper.
        Import-Module Microsoft.WinGet.Client -Cmdlet Repair-WinGetPackageManager -Function @() -Alias @() -ErrorAction Stop
        $repairArguments = @{ Latest = $true; Force = $true; ErrorAction = 'Stop' }
        if ($AllUsers) { $repairArguments.AllUsers = $true }
        Repair-WinGetPackageManager @repairArguments | Out-Null
    } finally { [Net.ServicePointManager]::SecurityProtocol = $previousProtocol }
}

function Invoke-ElevatedWinGetRepair {
    # Only WinGet repair runs elevated. Runtime installs, provider files and PATH
    # remain in the original user's process, even with different UAC credentials.
    $resultFile = [IO.Path]::GetTempFileName()
    try {
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments = @('-NoProfile', '-File', ('"{0}"' -f $installerScript),
                       '-RepairWinGetAllUsers', '-WinGetRepairResult', ('"{0}"' -f $resultFile))
        Write-Host 'WinGet requires administrator repair. Approve the Windows UAC prompt to continue...'
        try {
            $process = Start-Process -FilePath $powershell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        } catch { throw ('Could not start administrator WinGet repair (UAC may have been cancelled): ' + $_.Exception.Message) }
        $result = $null
        if ((Get-Item -LiteralPath $resultFile).Length) {
            $result = Get-Content -LiteralPath $resultFile -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        if ($process.ExitCode -ne 0 -or -not $result -or -not $result.ok) {
            $detail = if ($result -and $result.message) { $result.message } else { 'No repair result was returned. Check PowerShell policy and the UAC prompt.' }
            throw ('Administrator WinGet repair failed (exit {0}): {1}' -f $process.ExitCode, $detail)
        }
    } finally { Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue }
}

function Ensure-WinGet([switch]$CheckOnly) {
    $winget = Find-WinGet
    if ($winget) { return $winget }
    if ($CheckOnly) { throw 'WinGet >=1.8 is required. Run install-windows.ps1 without -CheckOnly to install it.' }
    Register-WinGet
    Refresh-ProcessPath
    $winget = Find-WinGet
    if ($winget) { return $winget }
    $administrator = Test-InstallerAdministrator
    $repairFailure = $null
    try { Install-WinGetClient -AllUsers:$administrator }
    catch { $repairFailure = $_.Exception.Message }
    Refresh-ProcessPath
    $winget = Find-WinGet
    # Some module failures concern COM registration after a usable CLI was installed.
    if ($winget) { return $winget }
    if ($repairFailure -and -not $administrator -and $repairFailure -match 'AllUsers|administrator mode') {
        Write-Host ('Current-user WinGet repair failed: ' + $repairFailure)
        try { Invoke-ElevatedWinGetRepair }
        catch { throw ('WinGet bootstrap failed. Initial error: ' + $repairFailure + ' Administrator retry: ' + $_.Exception.Message) }
        Register-WinGet
        Refresh-ProcessPath
        $winget = Find-WinGet
        if ($winget) { return $winget }
    } elseif ($repairFailure) {
        throw ('WinGet bootstrap failed: ' + $repairFailure + ' Check connectivity and App Installer policy. Manual installer: https://aka.ms/getwinget')
    }
    throw 'WinGet repair finished but no working WinGet >=1.8 was found for this user. Restart Windows if requested and retry. Administrator-only repair: powershell -NoProfile -File .\install-windows.ps1 -RepairWinGetAllUsers'
}

function New-DownloadDirectory {
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('claude-launchers-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    return $directory
}

function Save-Download([string]$Url, [string]$Destination) {
    Write-Host ('Downloading ' + $Url)
    $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 900
    } finally { [Net.ServicePointManager]::SecurityProtocol = $previousProtocol }
    Unblock-File -LiteralPath $Destination -ErrorAction SilentlyContinue
}

function Assert-SignedInstaller([string]$Path) {
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if (-not $signature -or $signature.Status -ne 'Valid') {
        $status = if ($signature) { [string]$signature.Status } else { 'unknown' }
        throw ('Downloaded installer signature is not valid (' + $status + '): ' + $Path + '. Installation stopped.')
    }
}

function Get-LatestNodeRelease {
    $releases = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json'
    $entry = $releases | Where-Object {
        $_.lts -and ([version](([string]$_.version).TrimStart('v')) -ge [version]'20.18.1')
    } | Select-Object -First 1
    if (-not $entry) { throw 'Could not find a Node.js LTS release >= 20.18.1 on nodejs.org.' }
    return @{ Url = 'https://nodejs.org/dist/' + $entry.version + '/node-' + $entry.version + '-x64.msi'
              Version = $entry.version }
}

function Get-LatestPythonRelease {
    $index = Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/' -UseBasicParsing -TimeoutSec 60
    $versions = @([regex]::Matches($index.Content, '3\.14\.\d+') | ForEach-Object { $_.Value } | Select-Object -Unique)
    $version = $versions | Sort-Object { [version]$_ } -Descending | Select-Object -First 1
    if (-not $version) { throw 'Could not find a Python 3.14 installer on python.org.' }
    return @{ Url = 'https://www.python.org/ftp/python/' + $version + '/python-' + $version + '-amd64.exe'
              Version = $version }
}

function Get-LatestGitRelease {
    $release = Invoke-RestMethod -Headers @{ 'User-Agent' = 'claude-provider-launchers' } `
        -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest'
    $asset = @($release.assets) | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1
    if (-not $asset) { throw 'Could not find the 64-bit Git for Windows installer in the latest release.' }
    return @{ Url = $asset.browser_download_url; Version = $release.tag_name }
}

function Invoke-ElevatedInstaller([string]$FilePath, [string]$Arguments, [string]$Label) {
    Write-Host ('Installing ' + $Label + '. Approve the Windows administrator prompt if it appears...')
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Verb RunAs -Wait -PassThru
    $exitCode = if ($process -and $null -ne $process.ExitCode) { $process.ExitCode } else { 0 }
    if ($exitCode -notin @(0, 3010, 1641)) {
        throw ($Label + ' installer failed (exit ' + $exitCode + ').')
    }
    if ($exitCode -eq 3010) { Write-Host ('A reboot is required to finish installing ' + $Label + '.') }
}

function Install-ClaudeNative([string]$Directory) {
    $script = Join-Path $Directory 'install-claude.ps1'
    Save-Download 'https://claude.ai/install.ps1' $script
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Invoke-Checked $powershell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script) 'Install Claude Code'
}

function Install-DirectPackage([string]$PackageId, [string]$DisplayName) {
    # Fallback for machines without a usable WinGet: download official installers,
    # verify their Authenticode signature and run them silently.
    $directory = New-DownloadDirectory
    try {
        switch ($PackageId) {
            'OpenJS.NodeJS.LTS' {
                $release = Get-LatestNodeRelease
                $file = Join-Path $directory 'node.msi'
                Save-Download $release.Url $file
                Assert-SignedInstaller $file
                Invoke-ElevatedInstaller 'msiexec.exe' ('/i "' + $file + '" /qn /norestart') 'Node.js'
            }
            'Python.Python.3.14' {
                $release = Get-LatestPythonRelease
                $file = Join-Path $directory 'python.exe'
                Save-Download $release.Url $file
                Assert-SignedInstaller $file
                Invoke-ElevatedInstaller $file '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' 'Python (all users)'
            }
            'Git.Git' {
                $release = Get-LatestGitRelease
                $file = Join-Path $directory 'git-setup.exe'
                Save-Download $release.Url $file
                Assert-SignedInstaller $file
                Invoke-ElevatedInstaller $file '/VERYSILENT /NORESTART /NOCANCEL /SUPPRESSMSGBOXES' 'Git for Windows'
            }
            'Anthropic.ClaudeCode' {
                Install-ClaudeNative $directory
            }
            default { throw ('No direct installer is known for ' + $PackageId + '.') }
        }
    } finally {
        Remove-Item -LiteralPath $directory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-WinGetPackage([string]$PackageId, [string]$DisplayName, [switch]$UserScope, [switch]$MachineScope) {
    if ($UserScope -and $MachineScope) { throw 'Choose either user or machine installation scope.' }
    try {
        $winget = Ensure-WinGet
    } catch {
        $bootstrapFailure = $_.Exception.Message
        Write-Host ('WinGet is unavailable: ' + $bootstrapFailure)
        Write-Host ('Falling back to the official ' + $DisplayName + ' installer.')
        try { Install-DirectPackage $PackageId $DisplayName }
        catch {
            throw ('WinGet is unavailable (' + $bootstrapFailure + ') and the direct installation of ' +
                   $DisplayName + ' failed: ' + $_.Exception.Message)
        }
        Refresh-ProcessPath
        return
    }
    Write-Host ('Installing ' + $DisplayName + ' using WinGet...')
    $arguments = @('install', '--id', $PackageId, '--exact', '--source', 'winget', '--silent',
                   '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    if ($UserScope) { $arguments += @('--scope', 'user') }
    if ($MachineScope) { $arguments += @('--scope', 'machine') }
    Invoke-Checked $winget $arguments ('Install ' + $DisplayName)
    Refresh-ProcessPath
}

function Ensure-Runtime([string]$Kind, [string]$PackageId, [string]$DisplayName, [switch]$CheckOnly, [switch]$UserScope, [switch]$MachineScope) {
    if ($UserScope -and $MachineScope) { throw 'Choose either user or machine installation scope.' }
    $runtime = Find-Runtime $Kind -MachineScope:$MachineScope
    if ($runtime) { return $runtime }
    if ($CheckOnly) { throw ($DisplayName + ' is required. Run install-windows.ps1 without -CheckOnly to install it.') }
    Install-WinGetPackage $PackageId $DisplayName -UserScope:$UserScope -MachineScope:$MachineScope
    $runtime = Find-Runtime $Kind -MachineScope:$MachineScope
    if (-not $runtime) { throw ($DisplayName + ' was installed but a compatible executable is not available. Restart Windows if requested by the installer, then run install-windows.ps1 again.') }
    return $runtime
}

# Dot-sourcing exposes the dependency checks for offline tests; it installs nothing.
if ($MyInvocation.InvocationName -eq '.') { return }

if ($RepairWinGetAllUsers) {
    try {
        if ($CheckOnly) { throw '-CheckOnly cannot be combined with -RepairWinGetAllUsers.' }
        if (-not (Test-InstallerAdministrator)) { throw '-RepairWinGetAllUsers must run in an administrator PowerShell window.' }
        Install-WinGetClient -AllUsers
        if ($WinGetRepairResult) {
            @{ok=$true} | ConvertTo-Json -Compress | Set-Content -LiteralPath $WinGetRepairResult -Encoding UTF8
        }
    } catch {
        if ($WinGetRepairResult) {
            @{ok=$false;message=$_.Exception.Message} | ConvertTo-Json -Compress | Set-Content -LiteralPath $WinGetRepairResult -Encoding UTF8
        }
        throw
    }
    return
}

foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude', 'or_claude')) {
    if (-not (Test-Path -LiteralPath (Join-Path $binDirectory "$name.cmd"))) { throw "Missing launcher: $name.cmd" }
}
Refresh-ProcessPath
# Bootstrap WinGet lazily, only when a missing runtime actually needs installing.
$node = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js >=20.18.1 with npm' -CheckOnly:$CheckOnly
$python = Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python >=3.10 (all users)' -CheckOnly:$CheckOnly -MachineScope
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
    foreach ($name in @('gpt_claude', 'ai_claude', 'ds_claude', 'or_claude')) {
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
Get-Command gpt_claude, ai_claude, ds_claude, or_claude | Select-Object Name, Source
Write-Host 'Launchers installed. Open a new terminal and configure providers using README.md.'
Write-Host 'DeepSeek on a fresh installation: set DEEPSEEK_API_KEY, then run ds_claude.'
