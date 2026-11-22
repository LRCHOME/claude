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

    def test_fresh_deepseek_install_uses_environment_key_without_creating_config(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'fixture-secret'}, clear=True), \
                patch('launch.Path.expanduser', return_value=self.root / 'missing.json'):
            cfg = launch.configuration('ds')
            env = launch.environment(cfg)
        self.assertEqual(cfg['config'], 'DEEPSEEK_API_KEY (environment)')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'deepseek-flash')
        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'medium')
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.deepseek.com/anthropic')
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-secret')
        self.assertFalse((self.root / 'missing.json').exists())

    def test_explicit_missing_config_is_reported_instead_of_using_environment_key(self):
        missing = self.root / 'explicit.json'
        output = io.StringIO()
        with patch.dict(os.environ, {'OPENCODE_CONFIG': str(missing), 'DEEPSEEK_API_KEY': 'fixture-secret'}, clear=True), \
                patch('sys.argv', ['launch.py', 'ds', '--launcher-info']), contextlib.redirect_stderr(output):
            self.assertEqual(launch.main(), 1)
        self.assertIn(str(missing), output.getvalue())
        self.assertIn('OPENCODE_CONFIG', output.getvalue())
        self.assertNotIn('fixture-secret', output.getvalue())

    def test_missing_default_config_and_key_explains_fresh_setup(self):
        missing = self.root / 'missing.json'
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('launch.Path.expanduser', return_value=missing), \
                patch('sys.argv', ['launch.py', 'ds', '--launcher-info']), contextlib.redirect_stderr(output):
            self.assertEqual(launch.main(), 1)
        self.assertIn(str(missing), output.getvalue())
        self.assertIn('DEEPSEEK_API_KEY', output.getvalue())

    def test_missing_key_file_is_reported_without_configuration_values(self):
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': '{file:missing.key}'},
            'models': {'deepseek-flash': {}}}}})
        with patch.dict(os.environ, {'OPENCODE_CONFIG': path}, clear=True):
            with self.assertRaises(launch.ConfigurationError) as failure:
                launch.configuration('ds')
        self.assertIn(str(self.root / 'missing.key'), str(failure.exception))

    def run_main(self, arguments, environment, stdin=''):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), \
                patch('sys.argv', ['launch.py', *arguments]), \
                patch('sys.stdin', io.StringIO(stdin)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = launch.main()
        return code, output.getvalue(), errors.getvalue()

    def test_setkey_preserves_other_settings_and_never_prints_the_secret(self):
        path = self.root / 'providers.json'
        path.write_text(json.dumps({'model': 'keep/me', 'provider': {
            'other': {'options': {'apiKey': 'other-secret'}},
            'deepseek': {'options': {'apiKey': '{env:OLD}'}, 'models': {}}}}))
        state = self.root / 'state.json'
        with patch.object(launch, 'fetch_models', return_value=[{'id': 'deepseek-flash'}]):
            code, output, errors = self.run_main(
                ['ds', '--setkey', '--stdin'], {'OPENCODE_CONFIG': str(path),
                                                'CLAUDE_LAUNCHERS_STATE': str(state)},
                stdin='fixture-secret\n')
        self.assertEqual(code, 0)
        self.assertNotIn('fixture-secret', output + errors)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['provider']['deepseek']['options']['apiKey'], 'fixture-secret')
        self.assertEqual(saved['provider']['other']['options']['apiKey'], 'other-secret')
        self.assertEqual(saved['model'], 'keep/me')
        self.assertIn(str(path), output)

    def test_setkey_creates_a_missing_config_from_the_environment(self):
        path = self.root / 'missing' / 'providers.json'
        with patch.object(launch, 'fetch_models', return_value=[]):
            code, output, errors = self.run_main(
                ['or', '--setkey', '--from-env'],
                {'OPENCODE_CONFIG': str(path), 'OPENROUTER_API_KEY': 'fixture-secret'})
        self.assertEqual(code, 0)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['$schema'], 'https://opencode.ai/config.json')
        self.assertEqual(saved['provider']['openrouter']['options']['apiKey'], 'fixture-secret')
        self.assertEqual(saved['provider']['openrouter']['options']['baseURL'],
                         'https://openrouter.ai/api/v1')
        self.assertNotIn('fixture-secret', output + errors)

    def test_setkey_refuses_a_key_rejected_with_401_and_keeps_the_file(self):
        path = self.root / 'providers.json'
        path.write_text(json.dumps({'provider': {'deepseek': {'options': {'apiKey': 'old-key'},
                                                              'models': {}}}}))
        with patch.object(launch, 'fetch_models', side_effect=launch.NetworkError('bad', 401)):
            code, output, errors = self.run_main(
                ['ds', '--setkey', '--stdin'], {'OPENCODE_CONFIG': str(path),
                                                'CLAUDE_LAUNCHERS_STATE': str(self.root / 'state.json')},
                stdin='fixture-secret\n')
        self.assertEqual(code, 1)
        self.assertIn('401', errors)
        self.assertNotIn('fixture-secret', output + errors)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['provider']['deepseek']['options']['apiKey'], 'old-key')

    def test_setmodel_is_used_by_the_next_launch_and_environment_wins(self):
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': 'fixture'},
            'models': {'deepseek-flash': {}}}}})
        state = self.root / 'state.json'
        rows = [{'id': 'deepseek-flash', 'context_length': None, 'output': None},
                {'id': 'deepseek-v4-pro', 'context_length': 200000, 'output': 16000}]
        with patch.object(launch, 'catalogue', return_value=rows):
            code, output, errors = self.run_main(
                ['ds', '--setmodel', 'deepseek-v4-pro'],
                {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(state)})
        self.assertEqual(code, 0)
        with patch.dict(os.environ, {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(state)},
                        clear=True):
            cfg = launch.configuration('ds')
            env = launch.environment(cfg)
            self.assertEqual(env['ANTHROPIC_MODEL'], 'deepseek-v4-pro')
            self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '200000')
            self.assertEqual(env['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'deepseek-flash')
            with patch.dict(os.environ, {'DS_CLAUDE_MODEL': 'deepseek-flash'}):
                self.assertEqual(launch.configuration('ds')['model'], 'deepseek-flash')

    def test_setmodel_rejects_an_unknown_model_with_suggestions(self):
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': 'fixture'},
            'models': {'deepseek-flash': {}}}}})
        state = self.root / 'state.json'
        rows = [{'id': 'deepseek-flash'}, {'id': 'deepseek-v4-pro'}]
        with patch.object(launch, 'catalogue', return_value=rows):
            code, output, errors = self.run_main(
                ['ds', '--setmodel', 'deepseek-v4'],
                {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(state)})
        self.assertEqual(code, 1)
        self.assertIn('deepseek-v4-pro', errors)
        self.assertIn('--force', errors)
        self.assertFalse(state.exists())

    def test_listmodels_marks_the_current_model_and_filters(self):
        path = self.config({'provider': {'deepseek': {'options': {'apiKey': 'fixture'},
            'models': {'deepseek-flash': {}}}}})
        state = self.root / 'state.json'
        state.write_text(json.dumps({'ds': {'model': 'deepseek-flash'}}))
        rows = [{'id': 'deepseek-flash', 'context_length': 1000000, 'output': 65536},
                {'id': 'deepseek-v4-pro', 'context_length': 200000, 'output': 16000}]
        with patch.object(launch, 'catalogue', return_value=rows):
            code, output, errors = self.run_main(
                ['ds', '--listmodels', 'v4'],
                {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(state)})
        self.assertEqual(code, 0)
        self.assertIn('deepseek-v4-pro', output)
        self.assertNotIn('deepseek-flash', output)

    def test_openrouter_requires_a_model_then_uses_the_anthropic_endpoint(self):
        path = self.config({'provider': {'openrouter': {'options': {'apiKey': '{env:OR_KEY}'}}}})
        state = self.root / 'state.json'
        environment = {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(state), 'OR_KEY': 'fixture'}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(launch.ConfigurationError) as failure:
                launch.configuration('or')
        self.assertIn('--setmodel', str(failure.exception))
        state.write_text(json.dumps({'or': {'model': 'vendor/model-1', 'context': 131072, 'output': 8192}}))
        with patch.dict(os.environ, environment, clear=True):
            env = launch.environment(launch.configuration('or'))
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://openrouter.ai/api')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'vendor/model-1')
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture')
        self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '131072')

    def test_listmodels_falls_back_to_the_config_when_the_server_is_unreachable(self):
        path = self.config({'provider': {'openrouter': {'options': {'apiKey': 'fixture'},
            'models': {'main': {'id': 'vendor/model-1', 'limit': {'context': 131072}}}}}})
        with patch.object(launch, 'fetch_models', side_effect=launch.NetworkError('HTTP 403 — заблокировано.', 403)):
            code, output, errors = self.run_main(
                ['or', '--listmodels'],
                {'OPENCODE_CONFIG': path, 'CLAUDE_LAUNCHERS_STATE': str(self.root / 'state.json')})
        self.assertEqual(code, 0)
        self.assertIn('vendor/model-1', output)
        self.assertIn('403', errors)

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
