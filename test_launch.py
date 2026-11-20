import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import launch


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def config(self, value):
        path = self.root / 'providers.json'
        path.write_text(json.dumps(value), encoding='utf-8-sig')
        return str(path)

    def test_direct_provider_environment_uses_configured_endpoint_and_secret(self):
        path = self.config({'provider': {'ai-lrcs': {'options': {
            'baseURL': 'https://provider.example/v1', 'apiKey': '{env:FIXTURE_AI_KEY}'},
            'models': {'local': {'id': 'fixture-model'}}}}})
        with patch.dict(os.environ, {'OPENCODE_CONFIG': path, 'FIXTURE_AI_KEY': 'fixture-secret',
                                     'CLAUDE_CODE_EFFORT_LEVEL': 'high'}, clear=True):
            cfg = launch.configuration('ai')
            env = launch.environment(cfg)
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://provider.example')
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-secret')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'fixture-model')
        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')

    def test_secret_file_and_bare_authentication(self):
        (self.root / 'fixture.key').write_text('fixture-secret\n', encoding='utf-8')
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': '{file:fixture.key}'},
            'models': {'deepseek-flash': {}}}}})
        with patch.dict(os.environ, {'OPENCODE_CONFIG': path, 'ANTHROPIC_AUTH_TOKEN': 'old',
                                     'CLAUDE_CODE_EFFORT_LEVEL': 'max'}, clear=True):
            env = launch.environment(launch.configuration('ds'), bare=True)
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.deepseek.com/anthropic')
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'fixture-secret')
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)
        for name in ('ANTHROPIC_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL',
                     'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                     'ANTHROPIC_DEFAULT_HAIKU_MODEL'):
            self.assertEqual(env[name], 'deepseek-flash')
        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'medium')

    def test_launcher_info_does_not_print_secret(self):
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': 'fixture-secret'},
            'models': {'deepseek-flash': {}}}}})
        output = io.StringIO()
        with patch.dict(os.environ, {'OPENCODE_CONFIG': path}), patch('sys.argv', ['launch.py', 'ds', '--launcher-info']), contextlib.redirect_stdout(output):
            self.assertEqual(launch.main(), 0)
        self.assertNotIn('fixture-secret', output.getvalue())

    def test_unix_installer_handles_spaces_and_preserves_arguments(self):
        specification = importlib.util.spec_from_file_location('unix_installer', Path(__file__).with_name('install-unix.py'))
        installer = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(installer)
        source = self.root / 'project with spaces'
        source.mkdir()
        for name in ('gpt-launch.mjs', 'launch.py'):
            (source / name).write_text('fixture', encoding='utf-8')
        target = self.root / 'bin'
        installer.install(source, target)
        command = (target / 'ds_claude').read_text(encoding='utf-8').splitlines()[1]
        self.assertEqual(shlex.split(command)[:4], ['exec', 'python3', str(source.resolve() / 'launch.py'), 'ds'])
        self.assertTrue(command.endswith('"$@"'))


if __name__ == '__main__':
    unittest.main()
