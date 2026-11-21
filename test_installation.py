import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).parent


class FreshInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_native_version_does_not_import_adapter_dependencies_or_auth(self):
        # Only the entry point is present. A subprocess mock supplies the native
        # client, so CI needs neither Claude nor credentials nor undici here.
        shutil.copyfile(ROOT / 'gpt-launch.mjs', self.root / 'gpt-launch.mjs')
        preload = self.root / 'mock-native.mjs'
        preload.write_text("""
import cp from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {EventEmitter} from 'node:events';
cp.spawn=(command,args)=>{
  console.log(JSON.stringify({command,args}));
  const child=new EventEmitter();child.exitCode=null;child.kill=()=>{};
  queueMicrotask(()=>{child.exitCode=0;child.emit('exit',0,null);});return child;
};
syncBuiltinESMExports();
""", encoding='utf-8')
        for flag in ('--version', '--help'):
            with self.subTest(flag=flag):
                result = subprocess.run(['node', '--import', preload.as_uri(), str(self.root / 'gpt-launch.mjs'), flag],
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['args'], [flag])

    def test_missing_dependency_error_explains_how_to_install_it(self):
        for source in ROOT.glob('gpt-*.mjs'):
            shutil.copyfile(source, self.root / source.name)
        result = subprocess.run(['node', str(self.root / 'gpt-launch.mjs'), '-p', 'fixture'],
                                text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing npm dependency undici', result.stderr)
        self.assertIn('npm ci', result.stderr)

    def run_powershell(self, body):
        system = Path(os.environ['SystemRoot']) / 'System32'
        powershell = system / 'WindowsPowerShell/v1.0/powershell.exe'
        script = self.root / 'test-bootstrap.ps1'
        installer = str(ROOT / 'install-windows.ps1').replace("'", "''")
        script.write_text(". '" + installer + "'\n" + body, encoding='utf-8-sig')
        result = subprocess.run([str(powershell), '-NoProfile', '-File', str(script)],
                                text=True, encoding='utf-8', capture_output=True, timeout=20)
        if 'running scripts is disabled' in ' '.join(result.stderr.split()) or 'PSSecurityException' in result.stderr:
            self.skipTest('Local execution policy disallows PowerShell test scripts')
        return result

    @unittest.skipUnless(os.name == 'nt', 'Windows installer preflight')
    def test_missing_node_check_only_never_installs(self):
        result = self.run_powershell("""
function Find-Runtime { param($Kind) return $null }
function Install-WinGetPackage { throw 'UNEXPECTED INSTALL' }
Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js >=20.18.1 with npm' -CheckOnly
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node.js >=20.18.1 with npm is required', result.stdout + result.stderr)
        self.assertNotIn('UNEXPECTED INSTALL', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows bootstrap tests')
    def test_fresh_install_installs_dependencies_then_rediscovers_them(self):
        result = self.run_powershell("""
$script:installed = @{}
$script:calls = @(); $script:scopes = @()
function Find-Runtime { param($Kind) if ($script:installed.ContainsKey($Kind)) { return ($Kind + '.exe') } }
function Install-WinGetPackage {
    param($PackageId, $DisplayName, [switch]$UserScope, [switch]$MachineScope)
    $kind = @{ 'OpenJS.NodeJS.LTS'='Node'; 'Python.Python.3.14'='Python'; 'Git.Git'='Git'; 'Anthropic.ClaudeCode'='Claude' }[$PackageId]
    $script:installed[$kind] = $true
    $script:calls += $PackageId
    $script:scopes += $(if ($MachineScope) { 'machine' } elseif ($UserScope) { 'user' } else { 'auto' })
}
$node = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js'
$python = Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python' -MachineScope
$git = Ensure-Runtime 'Git' 'Git.Git' 'Git'
$claude = Ensure-Runtime 'Claude' 'Anthropic.ClaudeCode' 'Claude' -UserScope
# Re-running with compatible dependencies must not install again.
$again = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js'
@{paths=@($node,$python,$git,$claude); packages=$script:calls; scopes=$script:scopes} | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['paths'], ['Node.exe', 'Python.exe', 'Git.exe', 'Claude.exe'])
        self.assertEqual(data['packages'], ['OpenJS.NodeJS.LTS', 'Python.Python.3.14', 'Git.Git', 'Anthropic.ClaudeCode'])
        self.assertEqual(data['scopes'], ['auto', 'machine', 'auto', 'user'])

    @unittest.skipUnless(os.name == 'nt', 'Windows bootstrap tests')
    def test_incompatible_executables_do_not_hide_a_supported_runtime(self):
        result = self.run_powershell("""
function Get-RuntimeCandidates { param($Kind) @('old.exe','supported.exe') }
function Test-Runtime { param($Kind, $Executable) return ($Executable -eq 'supported.exe') }
function Install-WinGetPackage { throw 'UNEXPECTED INSTALL' }
Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'supported.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows bootstrap tests')
    def test_failed_package_install_stops_before_path_refresh(self):
        result = self.run_powershell("""
function Ensure-WinGet { return 'fixture-winget.exe' }
function Invoke-Checked { param($Executable,$Arguments,$Label) throw 'fixture package installation failed' }
function Refresh-ProcessPath { throw 'UNEXPECTED PATH REFRESH' }
Install-WinGetPackage 'Python.Python.3.14' 'Python' -UserScope
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture package installation failed', result.stderr)
        self.assertNotIn('UNEXPECTED PATH REFRESH', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows native argument handling')
    def test_single_version_argument_is_not_split_into_characters(self):
        fixture = self.root / 'version-fixture.cmd'
        fixture.write_text('@echo off\nif not "%~1"=="--version" exit /b 1\nif not "%~2"=="" exit /b 2\nexit /b 0\n', encoding='ascii')
        quoted = str(fixture).replace("'", "''")
        result = self.run_powershell("""
@{git=(Test-Runtime 'Git' 'FIXTURE'); claude=(Test-Runtime 'Claude' 'FIXTURE')} | ConvertTo-Json -Compress
""".replace('FIXTURE', quoted))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'git': True, 'claude': True})

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet bootstrap')
    def test_working_winget_does_not_trigger_bootstrap(self):
        result = self.run_powershell("""
function Find-WinGet { return 'existing-winget.exe' }
function Register-WinGet { throw 'UNEXPECTED REGISTRATION' }
function Install-WinGetClient { throw 'UNEXPECTED INSTALL' }
Ensure-WinGet
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'existing-winget.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet bootstrap')
    def test_missing_winget_check_only_performs_no_registration_or_download(self):
        result = self.run_powershell("""
function Find-WinGet { return $null }
function Register-WinGet { throw 'UNEXPECTED REGISTRATION' }
function Install-WinGetClient { throw 'UNEXPECTED INSTALL' }
Ensure-WinGet -CheckOnly
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WinGet >=1.8 is required', result.stderr)
        self.assertNotIn('UNEXPECTED', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet bootstrap')
    def test_registered_app_installer_avoids_downloading_winget(self):
        result = self.run_powershell("""
$script:registered = $false
function Find-WinGet { if ($script:registered) { return 'registered-winget.exe' } }
function Register-WinGet { $script:registered = $true }
function Refresh-ProcessPath {}
function Install-WinGetClient { throw 'UNEXPECTED DOWNLOAD' }
Ensure-WinGet
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'registered-winget.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet bootstrap')
    def test_fresh_pc_bootstraps_winget_before_runtime_packages(self):
        result = self.run_powershell("""
$script:installed = $false
$script:steps = @()
function Find-WinGet { if ($script:installed) { return 'new-winget.exe' } }
function Register-WinGet { $script:steps += 'register' }
function Refresh-ProcessPath {}
function Install-WinGetClient { $script:steps += 'bootstrap'; $script:installed = $true }
function Invoke-Checked { param($Executable,$Arguments,$Label) $script:steps += ('package:' + $Executable + ':' + $Arguments[2]) }
Install-WinGetPackage 'OpenJS.NodeJS.LTS' 'Node.js'
$script:steps | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Write-Host status is separate from the final JSON line.
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data, ['register', 'bootstrap', 'package:new-winget.exe:OpenJS.NodeJS.LTS'])

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet bootstrap')
    def test_failed_winget_bootstrap_cannot_continue_to_package_installation(self):
        result = self.run_powershell("""
function Find-WinGet { return $null }
function Register-WinGet {}
function Refresh-ProcessPath {}
function Install-WinGetClient { throw 'fixture bootstrap failed' }
function Invoke-Checked { throw 'UNEXPECTED PACKAGE INSTALL' }
Install-WinGetPackage 'OpenJS.NodeJS.LTS' 'Node.js'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture bootstrap failed', result.stderr)
        self.assertNotIn('UNEXPECTED PACKAGE INSTALL', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet version detection')
    def test_old_or_broken_winget_is_not_accepted(self):
        fixtures = []
        for name, body in [('old', 'echo v1.7.0\nexit /b 0'),
                           ('good', 'echo v1.12.470\nexit /b 0'),
                           ('broken', 'exit /b 1')]:
            file = self.root / (name + '.cmd')
            file.write_text('@echo off\n' + body + '\n', encoding='ascii')
            fixtures.append(str(file).replace("'", "''"))
        result = self.run_powershell("@{old=(Test-WinGet 'OLD');good=(Test-WinGet 'GOOD');broken=(Test-WinGet 'BROKEN')} | ConvertTo-Json -Compress"
                                     .replace('OLD', fixtures[0]).replace('GOOD', fixtures[1]).replace('BROKEN', fixtures[2]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'old': False, 'good': True, 'broken': False})

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair fallback')
    def test_allusers_error_retries_elevated_and_registers_original_user(self):
        result = self.run_powershell("""
$script:steps = @(); $script:repaired = $false
function Find-WinGet { if ($script:repaired) { 'repaired-winget.exe' } }
function Test-InstallerAdministrator { $false }
function Register-WinGet { $script:steps += 'register-original-user' }
function Refresh-ProcessPath {}
function Install-WinGetClient {
    param([switch]$AllUsers)
    $script:steps += ('repair-allusers:' + [bool]$AllUsers)
    throw 'Failed to repair winget. Try running with -AllUsers in administrator mode.'
}
function Invoke-ElevatedWinGetRepair { $script:steps += 'uac'; $script:repaired = $true }
$path = Ensure-WinGet
@{path=$path; steps=$script:steps} | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['path'], 'repaired-winget.exe')
        self.assertEqual(data['steps'], ['register-original-user', 'repair-allusers:False', 'uac', 'register-original-user'])

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair fallback')
    def test_administrator_uses_allusers_without_starting_another_uac_process(self):
        result = self.run_powershell("""
$script:repaired = $false
function Find-WinGet { if ($script:repaired) { 'admin-winget.exe' } }
function Test-InstallerAdministrator { $true }
function Register-WinGet {}
function Refresh-ProcessPath {}
function Invoke-ElevatedWinGetRepair { throw 'UNEXPECTED UAC' }
function Install-WinGetClient {
    param([switch]$AllUsers)
    if (-not $AllUsers) { throw 'ALLUSERS MISSING' }
    $script:repaired = $true
}
Ensure-WinGet
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'admin-winget.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair fallback')
    def test_connectivity_error_does_not_request_unnecessary_elevation(self):
        result = self.run_powershell("""
function Find-WinGet {}
function Test-InstallerAdministrator { $false }
function Register-WinGet {}
function Refresh-ProcessPath {}
function Install-WinGetClient { throw 'fixture TLS connection failed' }
function Invoke-ElevatedWinGetRepair { throw 'UNEXPECTED UAC' }
Ensure-WinGet
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture TLS connection failed', result.stderr)
        self.assertNotIn('UNEXPECTED', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair fallback')
    def test_usable_cli_after_partial_repair_failure_does_not_request_elevation(self):
        result = self.run_powershell("""
$script:repaired = $false
function Find-WinGet { if ($script:repaired) { 'usable-winget.exe' } }
function Test-InstallerAdministrator { $false }
function Register-WinGet {}
function Refresh-ProcessPath {}
function Install-WinGetClient { $script:repaired = $true; throw 'COM repair needs -AllUsers' }
function Invoke-ElevatedWinGetRepair { throw 'UNEXPECTED UAC' }
Ensure-WinGet
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'usable-winget.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair fallback')
    def test_failed_administrator_retry_preserves_both_errors_and_stops_packages(self):
        result = self.run_powershell("""
function Find-WinGet {}
function Test-InstallerAdministrator { $false }
function Register-WinGet {}
function Refresh-ProcessPath {}
function Install-WinGetClient { throw 'initial repair requires -AllUsers' }
function Invoke-ElevatedWinGetRepair { throw 'fixture UAC cancelled or repair failed' }
function Invoke-Checked { throw 'UNEXPECTED PACKAGE INSTALL' }
Install-WinGetPackage 'OpenJS.NodeJS.LTS' 'Node.js'
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('initial repair requires -AllUsers', result.stderr)
        self.assertIn('fixture UAC cancelled or repair failed', result.stderr)
        self.assertNotIn('UNEXPECTED', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet elevation arguments')
    def test_elevated_worker_quotes_paths_and_only_repairs_winget(self):
        result = self.run_powershell(r"""
$script:installerScript = 'C:\folder with spaces\install-windows.ps1'
function Start-Process {
    param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$Wait,[switch]$PassThru)
    $script:captured = @{args=$ArgumentList;verb=$Verb;style=$WindowStyle;wait=[bool]$Wait}
    $script:resultFile = $ArgumentList[-1].Trim('"')
    '{"ok":true}' | Set-Content -LiteralPath $script:resultFile -Encoding UTF8
    return @{ExitCode=0}
}
Invoke-ElevatedWinGetRepair
$script:captured.removed = -not (Test-Path -LiteralPath $script:resultFile)
$script:captured | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['verb'], 'RunAs')
        self.assertEqual(data['style'], 'Hidden')
        self.assertTrue(data['wait'])
        self.assertTrue(data['removed'])
        self.assertEqual(data['args'][:5], ['-NoProfile', '-File', '"C:\\folder with spaces\\install-windows.ps1"', '-RepairWinGetAllUsers', '-WinGetRepairResult'])
        self.assertNotIn('-ExecutionPolicy', data['args'])

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet elevation result')
    def test_elevated_worker_failure_is_not_treated_as_success(self):
        result = self.run_powershell("""
function Start-Process {
    param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$Wait,[switch]$PassThru)
    '{"ok":false,"message":"fixture system repair error"}' | Set-Content -LiteralPath $ArgumentList[-1].Trim('"') -Encoding UTF8
    return @{ExitCode=1}
}
Invoke-ElevatedWinGetRepair
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture system repair error', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows WinGet repair command')
    def test_module_repair_receives_allusers_and_restores_tls_protocol(self):
        result = self.run_powershell("""
$script:repairs = @(); $initialProtocol = [Net.ServicePointManager]::SecurityProtocol
function Get-PackageProvider { @{Version=[version]'3.0.0'} }
function Get-PSRepository { @{SourceLocation='https://www.powershellgallery.com/api/v2'} }
function Install-Module {}
function Import-Module {}
function Repair-WinGetPackageManager {
    param([switch]$AllUsers,[switch]$Latest,[switch]$Force,$ErrorAction)
    $script:repairs += @{allusers=[bool]$AllUsers;latest=[bool]$Latest;force=[bool]$Force}
}
Install-WinGetClient
Install-WinGetClient -AllUsers
@{repairs=$script:repairs; tlsRestored=([Net.ServicePointManager]::SecurityProtocol -eq $initialProtocol)} | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['repairs'], [{'allusers': False, 'latest': True, 'force': True}, {'allusers': True, 'latest': True, 'force': True}])
        self.assertTrue(data['tlsRestored'])

    @unittest.skipUnless(os.name == 'nt', 'Windows native UTF-8 output')
    def test_utf8_native_failure_is_readable_and_restores_console_encoding(self):
        message = '\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u0435\u0449\u0435\u043d\u0430 \u043f\u043e\u043b\u0438\u0442\u0438\u043a\u043e\u0439.'
        fixture = self.root / 'utf8-policy.cjs'
        fixture.write_text('process.stdout.write(Buffer.from(' + json.dumps(list(message.encode('utf-8'))) +
                           ')); process.exit(-1978334961);', encoding='ascii')
        body = """
[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(866)
$OutputEncoding = [Text.Encoding]::ASCII
try {
    Invoke-Checked (Get-Command node.exe).Source @('FIXTURE') 'Install Python'
    throw 'UNEXPECTED SUCCESS'
} catch {
    $result = @{message=$_.Exception.Message;code=$_.Exception.Data['NativeExitCode'];
                blocked=$_.Exception.Data['PolicyBlocked'];console=[Console]::OutputEncoding.CodePage;
                pipeline=$OutputEncoding.CodePage;preference=[string]$ErrorActionPreference}
}
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$result | ConvertTo-Json -Compress
""".replace('FIXTURE', str(fixture).replace("'", "''"))
        result = self.run_powershell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn(message, data['message'])
        self.assertIn('Windows blocked this installation by policy', data['message'])
        self.assertEqual(data['code'], -1978334961)
        self.assertTrue(data['blocked'])
        self.assertEqual(data['console'], 866)
        self.assertEqual(data['pipeline'], 20127)
        self.assertEqual(data['preference'], 'Stop')

    @unittest.skipUnless(os.name == 'nt', 'Windows native exit codes')
    def test_unrelated_native_error_is_not_reported_as_a_policy_block(self):
        fixture = self.root / 'ordinary-error.cmd'
        fixture.write_text('@echo off\necho fixture network failure\nexit /b 7\n', encoding='ascii')
        result = self.run_powershell("""
try { Invoke-Checked 'FIXTURE' @() 'Install Python' } catch {
    @{message=$_.Exception.Message;code=$_.Exception.Data['NativeExitCode'];blocked=$_.Exception.Data['PolicyBlocked']} | ConvertTo-Json -Compress
}
""".replace('FIXTURE', str(fixture).replace("'", "''")))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data['code'], 7)
        self.assertFalse(data['blocked'])
        self.assertIn('fixture network failure', data['message'])
        self.assertNotIn('Windows blocked this installation by policy', data['message'])

    @unittest.skipUnless(os.name == 'nt', 'Windows machine Python selection')
    def test_machine_python_is_selected_instead_of_a_user_installation(self):
        result = self.run_powershell("""
function Get-RuntimeCandidates { 'user-python.exe' }
function Get-MachinePythonCandidates { @('old-system-python.exe','system-python.exe') }
function Test-Runtime { param($Kind,$Executable) $Executable -ne 'old-system-python.exe' }
Find-Runtime 'Python' -MachineScope
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'system-python.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows machine Python installation')
    def test_existing_user_python_does_not_satisfy_machine_installation(self):
        result = self.run_powershell("""
$script:installed = $false
function Get-RuntimeCandidates { 'user-python.exe' }
function Get-MachinePythonCandidates { if ($script:installed) { 'system-python.exe' } }
function Test-Runtime { $true }
function Install-WinGetPackage {
    param($PackageId,$DisplayName,[switch]$UserScope,[switch]$MachineScope)
    if ($UserScope -or -not $MachineScope) { throw 'WRONG INSTALLATION SCOPE' }
    $script:installed = $true
}
Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python (all users)' -MachineScope
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'system-python.exe')

    @unittest.skipUnless(os.name == 'nt', 'Windows machine Python WinGet arguments')
    def test_python_installation_explicitly_selects_machine_scope(self):
        result = self.run_powershell("""
function Ensure-WinGet { 'fixture-winget.exe' }
function Refresh-ProcessPath {}
function Invoke-Checked { param($Executable,$Arguments,$Label) $script:arguments=$Arguments }
Install-WinGetPackage 'Python.Python.3.14' 'Python (all users)' -MachineScope
ConvertTo-Json -InputObject $script:arguments -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(args[args.index('--scope') + 1], 'machine')
        self.assertEqual(args[args.index('--id') + 1], 'Python.Python.3.14')
        self.assertNotIn('user', args)

    @unittest.skipUnless(os.name == 'nt', 'Windows machine Python CheckOnly')
    def test_machine_python_check_only_does_not_install_when_only_user_python_exists(self):
        result = self.run_powershell("""
function Get-RuntimeCandidates { 'user-python.exe' }
function Get-MachinePythonCandidates {}
function Test-Runtime { $true }
function Install-WinGetPackage { throw 'UNEXPECTED INSTALL' }
Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python (all users)' -MachineScope -CheckOnly
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python (all users) is required', result.stderr)
        self.assertNotIn('UNEXPECTED', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows machine Python registry discovery')
    def test_machine_python_in_custom_directory_is_found_via_hklm_registration(self):
        executable = self.root / 'system python' / 'python.exe'
        executable.parent.mkdir()
        executable.touch()
        result = self.run_powershell(r"""
$script:fixtureExecutable = 'FIXTURE'
function Get-ChildItem {
    param($LiteralPath,$Path,[switch]$Directory)
    if ($LiteralPath -eq 'HKLM:\SOFTWARE\Python\PythonCore') {
        @{PSChildName='3.14';PSPath='Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Python\PythonCore\3.14'}
    }
}
function Get-Item {
    param($LiteralPath)
    $key = New-Object PSObject
    $key | Add-Member ScriptMethod GetValue {
        param($Name)
        if ($Name -eq 'ExecutablePath') { $script:fixtureExecutable }
        if ($Name -eq '') { Split-Path $script:fixtureExecutable -Parent }
    }
    $key
}
ConvertTo-Json -InputObject @(Get-MachinePythonCandidates) -Compress
""".replace('FIXTURE', str(executable).replace("'", "''")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), [str(executable)])


if __name__ == '__main__':
    unittest.main()
