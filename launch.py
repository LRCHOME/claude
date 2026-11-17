#!/usr/bin/env python3
"""Launch Claude Code with a provider from the user's OpenCode config."""
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
from urllib.parse import urlsplit, urlunsplit


def resolve(value, config_path):
    if not isinstance(value, str):
        return value
    if value.startswith('{env:') and value.endswith('}'):
        name = value[5:-1]
        if not os.environ.get(name):
            raise ValueError('Не задана переменная ' + name)
        return os.environ[name]
    if value.startswith('{file:') and value.endswith('}'):
        path = Path(value[6:-1]).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        return path.read_text(encoding='utf-8-sig').strip()
    return value


def configuration(profile):
    path = Path(os.environ.get('OPENCODE_CONFIG', '~/.config/opencode/opencode.json')).expanduser()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    provider_id, model_key = {
        'local': ('mlx', 'local'),
        'ai': ('ai-lrcs', 'local'),
        'ds': ('deepseek', 'deepseek-v4-pro'),
    }[profile]
    if profile == 'ai' and provider_id not in config.get('provider', {}):
        # The Windows installation already keeps this connection in pi. Read it
        # there instead of copying its secret into another configuration file.
        path = Path(os.environ.get('PI_MODELS_CONFIG', '~/.pi/agent/models.json')).expanduser()
        pi = json.loads(path.read_text(encoding='utf-8-sig'))
        provider = pi['providers'][provider_id]
        entry = next(m for m in provider['models'] if m['id'] == model_key)
        model = {'id': entry['id'], 'limit': {
            'context': entry.get('contextWindow', 262144),
            'output': entry.get('maxTokens', 32768),
        }}
        options = {'baseURL': provider['baseUrl'], 'apiKey': provider.get('apiKey', '')}
    else:
        provider = config['provider'][provider_id]
        model = provider['models'][model_key]
        options = provider.get('options', {})
    model_id = model.get('id', model_key)
    if profile == 'ds':
        base = 'https://api.deepseek.com/anthropic'
    else:
        base = resolve(options['baseURL'], path).rstrip('/')
        parts = urlsplit(base)
        if parts.scheme not in ('http', 'https') or parts.username or parts.password:
            raise ValueError('Ожидается HTTP(S) baseURL без credentials в URL')
        route = parts.path.rstrip('/')
        if route.endswith('/v1'):
            route = route[:-3]
        host = parts.netloc
        if profile == 'local' and parts.hostname == 'localhost':
            host = '127.0.0.1' + (':' + str(parts.port) if parts.port else '')
        base = urlunsplit((parts.scheme, host, route, '', ''))
    key = resolve(options.get('apiKey', ''), path)
    if not key and profile != 'local':
        raise ValueError('В OpenCode не задан apiKey для ' + provider_id)
    limit = model.get('limit', {})
    return {
        'config': str(path), 'provider': provider_id, 'model': model_id,
        'base_url': base, 'key': key or 'local-no-key',
        'context': limit.get('context', 1000000 if profile == 'ds' else 262144),
        'output': limit.get('output', 65536 if profile != 'ai' else 32768),
        'fast_model': 'deepseek-flash' if profile == 'ds' and 'deepseek-flash' in provider['models'] else model_id,
    }


def environment(cfg, bare=False):
    env = os.environ.copy()
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                 'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_USE_BEDROCK',
                 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                 'ANTHROPIC_DEFAULT_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL'):
        env.pop(name, None)
    env.update({
        'ANTHROPIC_BASE_URL': cfg['base_url'],
        'ANTHROPIC_MODEL': cfg['model'],
        'ANTHROPIC_DEFAULT_FABLE_MODEL': cfg['model'],
        'ANTHROPIC_DEFAULT_OPUS_MODEL': cfg['model'],
        'ANTHROPIC_DEFAULT_SONNET_MODEL': cfg['model'],
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': cfg['fast_model'],
        'CLAUDE_CODE_SUBAGENT_MODEL': cfg['model'],
        'CLAUDE_CODE_MAX_CONTEXT_TOKENS': str(cfg['context']),
        'CLAUDE_CODE_AUTO_COMPACT_WINDOW': str(int(cfg['context'] * 0.75)),
        'CLAUDE_CODE_MAX_OUTPUT_TOKENS': str(cfg['output']),
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'CLAUDE_CODE_AUTO_MODE_SERVER': '0',
        'CLAUDE_CODE_AUTO_MODE_MODEL': os.environ.get('CLAUDE_CODE_AUTO_MODE_MODEL') or cfg['model'],
    })
    env['ANTHROPIC_API_KEY' if bare else 'ANTHROPIC_AUTH_TOKEN'] = cfg['key']
    return env


def main():
    profile, *args = sys.argv[1:]
    binary = shutil.which('claude.exe' if os.name == 'nt' else 'claude')
    if args in (['--version'], ['-v'], ['--help'], ['-h']):
        if not binary:
            print('Команда claude не найдена в PATH.', file=sys.stderr)
            return 127
        return subprocess.call([binary, *args])
    try:
        cfg = configuration(profile)
    except (OSError, ValueError, KeyError) as error:
        # Do not include config values or parser source text: they may contain keys.
        print('Не удалось прочитать подключение из OpenCode (' + type(error).__name__ + '). Проверь provider/model/apiKey и OPENCODE_CONFIG.', file=sys.stderr)
        return 1
    if args == ['--launcher-info']:
        print(json.dumps({k: v for k, v in cfg.items() if k != 'key'}, ensure_ascii=False, indent=2))
        return 0
    if not binary:
        print('Команда claude не найдена в PATH.', file=sys.stderr)
        return 127
    print(f"Claude → {cfg['provider']}/{cfg['model']} ({cfg['base_url']})", file=sys.stderr)
    if profile == 'local':
        from local_adapter import start
        server, proxy_url, proxy_key = start(cfg['base_url'], cfg['key'])
        adapted = dict(cfg, base_url=proxy_url, key=proxy_key)
        process = None
        try:
            process = subprocess.Popen([binary, *args], env=environment(adapted, bare='--bare' in args))
            return process.wait()
        except KeyboardInterrupt:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return 130
        finally:
            server.shutdown()
            server.server_close()
    if os.name != 'nt':
        os.execve(binary, [binary, *args], environment(cfg, bare='--bare' in args))
    process = subprocess.Popen([binary, *args], env=environment(cfg, bare='--bare' in args))
    try:
        return process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return 130


if __name__ == '__main__':
    sys.exit(main())
