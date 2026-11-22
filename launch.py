#!/usr/bin/env python3
"""Launch Claude Code with a provider from the user's OpenCode config."""
import getpass
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import urllib.error
import urllib.request
from difflib import get_close_matches
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """An actionable message containing only paths or setting names, never values."""


class NetworkError(RuntimeError):
    """A model-catalogue request failed; the message never contains a key."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


PROFILES = {
    'local': {'provider': 'mlx', 'model': 'local',
              'context': 262144, 'output': 65536},
    'ai': {'provider': 'ai-lrcs', 'model': 'local',
           'context': 262144, 'output': 32768},
    'ds': {'provider': 'deepseek', 'model': 'deepseek-flash',
           'endpoint': 'https://api.deepseek.com/anthropic',
           'models_url': 'https://api.deepseek.com/models',
           'key_env': 'DEEPSEEK_API_KEY', 'model_env': 'DS_CLAUDE_MODEL',
           'title': 'DeepSeek', 'context': 1000000, 'output': 65536},
    'or': {'provider': 'openrouter',
           'endpoint': 'https://openrouter.ai/api',
           'models_url': 'https://openrouter.ai/api/v1/models',
           'key_env': 'OPENROUTER_API_KEY', 'model_env': 'OR_CLAUDE_MODEL',
           'title': 'OpenRouter', 'public_models': True,
           'context': 262144, 'output': 32768},
}

COMMAND_NAMES = {'ai': 'ai_claude', 'ds': 'ds_claude', 'or': 'or_claude'}


def command_name(profile):
    return COMMAND_NAMES.get(profile, 'launch.py ' + profile)


def resolve(value, config_path):
    if not isinstance(value, str):
        return value
    if value.startswith('{env:') and value.endswith('}'):
        name = value[5:-1]
        if not os.environ.get(name):
            raise ConfigurationError('Не задана переменная окружения ' + name + '. Задайте её в текущем терминале.')
        return os.environ[name]
    if value.startswith('{file:') and value.endswith('}'):
        path = Path(value[6:-1]).expanduser()
        if not path.is_absolute():
            path = config_path.parent / path
        try:
            return path.read_text(encoding='utf-8-sig').strip()
        except FileNotFoundError:
            raise ConfigurationError('Не найден файл API-ключа: ' + str(path)) from None
    return value


def config_path():
    return Path(os.environ.get('OPENCODE_CONFIG') or '~/.config/opencode/opencode.json').expanduser()


def state_path():
    value = os.environ.get('CLAUDE_LAUNCHERS_STATE') or '~/.config/claude-launchers/state.json'
    try:
        return Path(value).expanduser()
    except RuntimeError:
        # No HOME/USERPROFILE: launching still works, only saving a selection cannot.
        return None


def load_state():
    path = state_path()
    if path is None:
        return {}
    try:
        state = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ConfigurationError('Не удалось прочитать выбранные модели: ' + str(path) +
                                 '. Исправьте файл или удалите его.') from None
    return state if isinstance(state, dict) else {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if os.name == 'posix':
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def config_help(profile, path):
    spec = PROFILES[profile]
    example = Path(__file__).with_name('examples') / 'opencode.json'
    text = ('Не найден файл настроек: ' + str(path) + '\n'
            'Выберите файл через OPENCODE_CONFIG. Пример: ' + str(example))
    if spec.get('key_env'):
        text += ('\nДля запуска без файла настроек задайте ' + spec['key_env'] +
                 ' и уберите OPENCODE_CONFIG, если она задана.'
                 '\nЛибо сохраните ключ в файл: ' + command_name(profile) + ' --setkey')
    return text


def load_provider(profile):
    """Return (provider entry, config path, config source) for a profile."""
    spec = PROFILES[profile]
    provider_id = spec['provider']
    path = config_path()
    source = str(path)
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict):
            raise ValueError('not an object')
    except FileNotFoundError:
        if spec.get('key_env') and not os.environ.get('OPENCODE_CONFIG') and os.environ.get(spec['key_env']):
            # A fresh installation does not need OpenCode or a config file.
            # Explicitly selected configs still win.
            models = {spec['model']: {}} if spec.get('model') else {}
            config = {'provider': {provider_id: {
                'options': {'apiKey': '{env:' + spec['key_env'] + '}'},
                'models': models}}}
            source = spec['key_env'] + ' (environment)'
        else:
            raise ConfigurationError(config_help(profile, path)) from None
    except (OSError, ValueError):
        raise ConfigurationError('Не удалось прочитать ' + str(path) +
                                 ': ожидается JSON-объект. Исправьте файл или укажите другой OPENCODE_CONFIG.') from None
    providers = config.get('provider')
    if not isinstance(providers, dict) or provider_id not in providers:
        if profile == 'ai':
            # The Windows installation already keeps this connection in pi. Read it
            # there instead of copying its secret into another configuration file.
            pi_path = Path(os.environ.get('PI_MODELS_CONFIG', '~/.pi/agent/models.json')).expanduser()
            pi = json.loads(pi_path.read_text(encoding='utf-8-sig'))
            provider = pi['providers'][provider_id]
            entry = next(m for m in provider['models'] if m['id'] == spec['model'])
            return {'options': {'baseURL': provider['baseUrl'], 'apiKey': provider.get('apiKey', '')},
                    'models': {spec['model']: {'id': entry['id'], 'limit': {
                        'context': entry.get('contextWindow', spec['context']),
                        'output': entry.get('maxTokens', spec['output'])}}}}, pi_path, str(pi_path)
        raise ConfigurationError('В ' + str(path) + ' нет provider "' + provider_id + '".\n' +
                                 'Добавьте его вручную или выполните ' + command_name(profile) + ' --setkey') from None
    provider = providers[provider_id]
    if not isinstance(provider, dict):
        raise ConfigurationError('В ' + str(path) + ' provider.' + provider_id + ' должен быть объектом.')
    return provider, path, source


def api_key(profile, required=True):
    spec = PROFILES[profile]
    try:
        provider, path, _ = load_provider(profile)
    except ConfigurationError:
        key = os.environ.get(spec.get('key_env') or '', '')
        if key or not required:
            return key
        raise
    options = provider.get('options')
    options = options if isinstance(options, dict) else {}
    key = resolve(options.get('apiKey', ''), path) or ''
    if not key:
        key = os.environ.get(spec.get('key_env') or '', '')
    if not key and required:
        raise ConfigurationError('Не задан apiKey для provider "' + spec['provider'] + '". Выполните ' +
                                 command_name(profile) + ' --setkey или задайте ' + spec['key_env'] + '.')
    return key


def configuration(profile):
    spec = PROFILES[profile]
    provider, path, source = load_provider(profile)
    provider_id = spec['provider']
    model_key = spec.get('model')
    options = provider.get('options')
    options = options if isinstance(options, dict) else {}
    models = provider.get('models')
    models = models if isinstance(models, dict) else {}
    state = load_state().get(profile)
    state = state if isinstance(state, dict) else {}
    chosen = os.environ.get(spec.get('model_env') or '') or state.get('model')
    if chosen:
        entry = models.get(chosen)
        entry = entry if isinstance(entry, dict) else {}
        limit = entry.get('limit')
        limit = limit if isinstance(limit, dict) else {}
        model_id = entry.get('id', chosen)
        # Limits remembered by --setmodel belong to the model they were fetched for.
        context = limit.get('context') or (state.get('context') if chosen == state.get('model') else None)
        output = limit.get('output') or (state.get('output') if chosen == state.get('model') else None)
        context = context or spec['context']
        output = output or spec['output']
    else:
        if not model_key:
            raise ConfigurationError('Модель не выбрана. Выполните ' + command_name(profile) +
                                     ' --listmodels, затем ' + command_name(profile) + ' --setmodel <id>.')
        entry = models.get(model_key)
        if not isinstance(entry, dict):
            raise ConfigurationError('В ' + str(path) + ' нет модели "' + model_key + '" у provider "' +
                                     provider_id + '". Добавьте её или выполните ' +
                                     command_name(profile) + ' --setmodel <id>.')
        limit = entry.get('limit')
        limit = limit if isinstance(limit, dict) else {}
        model_id = entry.get('id', model_key)
        context = limit.get('context', spec['context'])
        output = limit.get('output', spec['output'])
    if profile in ('ds', 'or'):
        base = spec['endpoint']
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
        raise ConfigurationError('Не задан apiKey для provider "' + provider_id + '". Выполните ' +
                                 command_name(profile) + ' --setkey или задайте ' +
                                 str(spec.get('key_env') or 'apiKey') + '.')
    cfg = {
        'config': source, 'provider': provider_id, 'model': model_id,
        'base_url': base, 'key': key or 'local-no-key',
        'context': context, 'output': output,
        'fast_model': 'deepseek-flash' if profile == 'ds' and 'deepseek-flash' in models else model_id,
    }
    if profile == 'ds':
        cfg['effort'] = 'medium'
    return cfg


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
    if cfg.get('effort'):
        env['CLAUDE_CODE_EFFORT_LEVEL'] = cfg['effort']
    return env


def request_json(url, key='', timeout=20):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/json', 'User-Agent': 'claude-launchers/0.1'})
    if key:
        request.add_header('Authorization', 'Bearer ' + key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            detail = 'HTTP 401 — сервер отклонил API-ключ.'
        elif error.code == 403:
            detail = 'HTTP 403 — доступ к серверу запрещён (ключ или сеть).'
        else:
            detail = 'HTTP ' + str(error.code) + '.'
        raise NetworkError('Сервер моделей ответил: ' + detail, error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise NetworkError('Нет связи с сервером моделей (' + type(error).__name__ + ').') from None
    except ValueError:
        raise NetworkError('Сервер моделей вернул не JSON.') from None


def fetch_models(spec, key='', timeout=20):
    payload = request_json(spec['models_url'], key, timeout)
    entries = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise NetworkError('Ответ сервера не содержит списка моделей.')
    rows = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
            continue
        top = entry.get('top_provider')
        top = top if isinstance(top, dict) else {}
        rows.append({
            'id': entry['id'],
            'context_length': entry.get('context_length') or top.get('context_length'),
            'output': entry.get('max_completion_tokens') or top.get('max_completion_tokens'),
            'pricing': entry.get('pricing') if isinstance(entry.get('pricing'), dict) else None,
        })
    return rows


def configured_models(provider):
    models = provider.get('models')
    models = models if isinstance(models, dict) else {}
    rows = []
    for name, entry in models.items():
        entry = entry if isinstance(entry, dict) else {}
        limit = entry.get('limit')
        limit = limit if isinstance(limit, dict) else {}
        rows.append({'id': entry.get('id', name), 'context_length': limit.get('context'),
                     'output': limit.get('output'), 'pricing': None})
    return rows


def catalogue(profile):
    """List models for a profile from the provider API, else from the config."""
    spec = PROFILES[profile]
    if 'models_url' in spec:
        key = api_key(profile, required=False)
        if not key and not spec.get('public_models'):
            raise ConfigurationError('Не задан API-ключ для provider "' + spec['provider'] +
                                     '". Выполните ' + command_name(profile) + ' --setkey.')
        try:
            return fetch_models(spec, key)
        except NetworkError as error:
            try:
                provider, _, _ = load_provider(profile)
            except ConfigurationError:
                # Nothing to fall back to; the network detail is the useful part.
                raise error from None
            rows = configured_models(provider)
            if not rows:
                raise
            print('Список с сервера недоступен: ' + str(error), file=sys.stderr)
            return rows
    provider, _, _ = load_provider(profile)
    return configured_models(provider)


def current_model(profile):
    spec = PROFILES[profile]
    state = load_state().get(profile)
    state = state if isinstance(state, dict) else {}
    return os.environ.get(spec.get('model_env') or '') or state.get('model') or spec.get('model')


def read_key(spec, args):
    if '--stdin' in args:
        value = sys.stdin.readline()
    elif '--from-env' in args:
        value = os.environ.get(spec['key_env'], '')
        if not value:
            raise ConfigurationError('Переменная ' + spec['key_env'] + ' не задана.')
    else:
        value = getpass.getpass('API-ключ ' + spec['title'] + ': ')
    value = value.strip()
    if not value:
        raise ConfigurationError('Ключ не введён.')
    if any(character.isspace() for character in value):
        raise ConfigurationError('Ключ содержит пробелы или переводы строк. Скопируйте его без них.')
    return value


def command_set_key(profile, args):
    spec = PROFILES[profile]
    if 'models_url' not in spec:
        raise ConfigurationError('Для ' + command_name(profile) + ' ключ задаётся в конфигурации провайдера.')
    key = read_key(spec, args)
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict):
            raise ValueError('not an object')
    except FileNotFoundError:
        config = {'$schema': 'https://opencode.ai/config.json'}
    except (OSError, ValueError):
        raise ConfigurationError('Не удалось прочитать ' + str(path) +
                                 ': ожидается JSON-объект. Исправьте файл или укажите другой OPENCODE_CONFIG.') from None
    providers = config.setdefault('provider', {})
    if not isinstance(providers, dict):
        raise ConfigurationError('В ' + str(path) + ' поле provider должно быть объектом.')
    provider = providers.setdefault(spec['provider'], {})
    if not isinstance(provider, dict):
        raise ConfigurationError('В ' + str(path) + ' provider.' + spec['provider'] + ' должен быть объектом.')
    options = provider.setdefault('options', {})
    if not isinstance(options, dict):
        raise ConfigurationError('В ' + str(path) + ' provider.' + spec['provider'] +
                                 '.options должен быть объектом.')
    options['apiKey'] = key
    if profile == 'ds':
        models = provider.setdefault('models', {})
        if isinstance(models, dict) and spec['model'] not in models:
            models[spec['model']] = {}
    if profile == 'or':
        options.setdefault('baseURL', 'https://openrouter.ai/api/v1')
    warning = None
    try:
        entries = fetch_models(spec, key, timeout=10)
    except NetworkError as error:
        if error.status == 401:
            raise ConfigurationError('Сервер отклонил ключ (HTTP 401). Ключ не сохранён.') from None
        warning = 'Ключ сохранён; проверить его сейчас не удалось: ' + str(error)
    else:
        warning = 'Ключ принят: доступно моделей — ' + str(len(entries)) + '.'
    try:
        write_json(path, config)
    except OSError as error:
        raise ConfigurationError('Не удалось записать ' + str(path) + ' (' + type(error).__name__ +
                                 '). Проверьте права на файл и OPENCODE_CONFIG.') from None
    print('Ключ сохранён: ' + str(path) + ' (provider.' + spec['provider'] + '.options.apiKey)')
    print(warning, file=sys.stderr)
    return 0


def format_price(value):
    try:
        price = float(value) * 1000000
    except (TypeError, ValueError):
        return None
    return '$' + format(price, '.3g') + '/M'


def command_list_models(profile, args):
    as_json = '--json' in args
    names = [argument for argument in args if not argument.startswith('-')]
    query = names[0].casefold() if names else ''
    rows = catalogue(profile)
    current = current_model(profile)
    if query:
        rows = [row for row in rows if query in row['id'].casefold()]
    rows.sort(key=lambda row: row['id'])
    if as_json:
        print(json.dumps([{'id': row['id'], 'context': row['context_length'],
                           'output': row['output']} for row in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print('Моделей не найдено' + (': "' + names[0] + '"' if query else '') + '.', file=sys.stderr)
        return 1
    for row in rows:
        details = []
        if row.get('context_length'):
            details.append('ctx ' + str(row['context_length']))
        if row.get('output'):
            details.append('out ' + str(row['output']))
        if row.get('pricing'):
            prices = [format_price(row['pricing'].get('prompt')),
                      format_price(row['pricing'].get('completion'))]
            prices = [price for price in prices if price]
            if prices:
                details.append('in ' + prices[0] + ', out ' + prices[-1])
        mark = '*' if row['id'] == current else ' '
        print(mark + ' ' + row['id'] + ('  ' + ', '.join(details) if details else ''))
    return 0


def command_set_model(profile, args):
    spec = PROFILES[profile]
    force = '--force' in args
    names = [argument for argument in args if not argument.startswith('-')]
    command = command_name(profile)
    if len(names) != 1 or not names[0].strip():
        raise ConfigurationError('Использование: ' + command + ' --setmodel <id> [--force]')
    model_id = names[0].strip()
    entry = None
    try:
        rows = catalogue(profile)
    except (ConfigurationError, NetworkError) as error:
        print('Список моделей проверить не удалось: ' + str(error), file=sys.stderr)
    else:
        ids = [row['id'] for row in rows]
        matches = [row for row in rows if row['id'] == model_id]
        if matches:
            entry = matches[0]
        elif ids and not force:
            message = 'Модель ' + model_id + ' не найдена у провайдера.'
            close = get_close_matches(model_id, ids, n=3, cutoff=0.5)
            if close:
                message += ' Похожие: ' + ', '.join(close) + '.'
            message += ' Сохранить всё равно: ' + command + ' --setmodel ' + model_id + ' --force'
            raise ConfigurationError(message)
    context = (entry or {}).get('context_length') or spec['context']
    output = (entry or {}).get('output') or spec['output']
    state = load_state()
    state[profile] = {'model': model_id, 'context': context, 'output': output}
    path = state_path()
    if path is None:
        raise ConfigurationError('Не удалось определить домашний каталог для сохранения выбора модели. '
                                 'Задайте CLAUDE_LAUNCHERS_STATE.')
    try:
        write_json(path, state)
    except OSError as error:
        raise ConfigurationError('Не удалось записать ' + str(path) + ' (' + type(error).__name__ +
                                 '). Проверьте права на файл или задайте CLAUDE_LAUNCHERS_STATE.') from None
    print('Модель для ' + command + ': ' + model_id)
    print('Контекст: ' + str(context) + ', максимум ответа: ' + str(output))
    if profile == 'ds' and context != spec['context']:
        print('Учтите: лимиты взяты по умолчанию — проверьте их в документации модели.', file=sys.stderr)
    return 0


COMMANDS = {'--setkey': command_set_key, '--listmodels': command_list_models,
            '--setmodel': command_set_model}


def main():
    if len(sys.argv) < 2:
        print('Использование: launch.py <local|ai|ds|or> [аргументы Claude Code]', file=sys.stderr)
        return 2
    profile, *args = sys.argv[1:]
    if profile not in PROFILES:
        print('Неизвестный профиль "' + profile + '". Допустимые: ' + ', '.join(PROFILES) + '.',
              file=sys.stderr)
        return 2
    if args and args[0] in COMMANDS:
        try:
            return COMMANDS[args[0]](profile, args[1:])
        except (ConfigurationError, NetworkError) as error:
            print(str(error), file=sys.stderr)
            return 1
    binary = shutil.which('claude.exe' if os.name == 'nt' else 'claude')
    if args in (['--version'], ['-v'], ['--help'], ['-h']):
        if not binary:
            print('Команда claude не найдена в PATH.', file=sys.stderr)
            return 127
        return subprocess.call([binary, *args])
    try:
        cfg = configuration(profile)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1
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
