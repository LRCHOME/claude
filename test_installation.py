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
                                text=True, capture_output=True, timeout=20)
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
$script:calls = @()
function Find-Runtime { param($Kind) if ($script:installed.ContainsKey($Kind)) { return ($Kind + '.exe') } }
function Install-WinGetPackage {
    param($PackageId, $DisplayName, [switch]$UserScope)
    $kind = @{ 'OpenJS.NodeJS.LTS'='Node'; 'Python.Python.3.14'='Python'; 'Git.Git'='Git'; 'Anthropic.ClaudeCode'='Claude' }[$PackageId]
    $script:installed[$kind] = $true
    $script:calls += $PackageId
}
$node = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js'
$python = Ensure-Runtime 'Python' 'Python.Python.3.14' 'Python' -UserScope
$git = Ensure-Runtime 'Git' 'Git.Git' 'Git'
$claude = Ensure-Runtime 'Claude' 'Anthropic.ClaudeCode' 'Claude' -UserScope
# Re-running with compatible dependencies must not install again.
$again = Ensure-Runtime 'Node' 'OpenJS.NodeJS.LTS' 'Node.js'
@{paths=@($node,$python,$git,$claude); packages=$script:calls} | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['paths'], ['Node.exe', 'Python.exe', 'Git.exe', 'Claude.exe'])
        self.assertEqual(data['packages'], ['OpenJS.NodeJS.LTS', 'Python.Python.3.14', 'Git.Git', 'Anthropic.ClaudeCode'])

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


if __name__ == '__main__':
    unittest.main()
