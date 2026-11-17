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

    @unittest.skipUnless(os.name == 'nt', 'Windows installer preflight')
    def test_missing_node_is_reported_before_installing_or_changing_path(self):
        shutil.copyfile(ROOT / 'install-windows.ps1', self.root / 'install-windows.ps1')
        shutil.copytree(ROOT / 'bin', self.root / 'bin')
        system = Path(os.environ['SystemRoot']) / 'System32'
        powershell = system / 'WindowsPowerShell/v1.0/powershell.exe'
        env = dict(os.environ, PATH=str(system))
        result = subprocess.run([str(powershell), '-NoProfile', '-File', str(self.root / 'install-windows.ps1'), '-CheckOnly'],
                                env=env, text=True, capture_output=True, timeout=15)
        if 'running scripts is disabled' in ' '.join(result.stderr.split()) or 'PSSecurityException' in result.stderr:
            self.skipTest('Local execution policy disallows PowerShell test scripts')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node.js >=20.18.1 is required', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
