# Установка на Mac

## 1. Проверьте зависимости

Установите нативный Claude Code, Python 3.10+ и Node.js 20.18.1+ с npm.
Для двух прямых лаунчеров Node.js не нужен.

```sh
command -v claude python3 node npm
claude --version
python3 --version
node --version
```

## 2. Скачайте репозиторий и установите команды

```sh
git clone https://github.com/lrchome/claude.git
cd claude
npm ci
python3 install-unix.py
export PATH="$HOME/.local/bin:$PATH"
```

Добавьте `export PATH="$HOME/.local/bin:$PATH"` в `~/.zshrc`, если используете
стандартный zsh. Для другого shell используйте его файл конфигурации.
Обёртки используют `node` и `python3` из PATH и подходят для Intel и Apple Silicon.
Если перенесли папку репозитория, повторите `python3 install-unix.py`.

```sh
gpt_claude --version
ds_claude --version
ai_claude --version
```

## 3. GPT через свой аккаунт ChatGPT

```sh
gpt_claude --login
gpt_claude --models
gpt_claude --launcher-info
```

Завершите вход в браузере. Авторизация создаётся на этом Mac отдельно и хранится
в `~/.config/gpt_claude`, вне репозитория. Переносить чужие токены не нужно.
Доступность token sharing зависит от аккаунта и условий провайдера.

## 4. DeepSeek и ваш AI-сервер

Скопируйте пример в личную папку. Блок ниже сохраняет уже существующий файл:

```sh
mkdir -p "$HOME/.config/claude-launchers"
export OPENCODE_CONFIG="$HOME/.config/claude-launchers/providers.json"
if [ ! -f "$OPENCODE_CONFIG" ]; then
  cp examples/opencode.json "$OPENCODE_CONFIG"
fi
chmod 600 "$OPENCODE_CONFIG"
```

Откройте `$OPENCODE_CONFIG` в редакторе. Для `ai_claude` замените
`YOUR_MODEL_ID` на ID вашей модели и укажите её лимиты. Ключи передайте через
переменные окружения:

```sh
export DEEPSEEK_API_KEY='YOUR_DEEPSEEK_KEY'
export AI_CLAUDE_BASE_URL='https://provider.example'
export AI_CLAUDE_API_KEY='YOUR_AI_KEY'
```

Замените заглушки своими значениями. Для DeepSeek нужны только его ключ
и provider `deepseek`; для AI-сервера — URL, ключ и provider `ai-lrcs`.
Повторяйте настройку окружения в новом терминале или настройте её локальное
хранение вне репозитория. Ключ также можно хранить в личном файле через
`{file:/absolute/path/to/key}` в поле `apiKey`.

Если уже используете OpenCode, можно добавить providers из примера в существующий
`~/.config/opencode/opencode.json` и не задавать `OPENCODE_CONFIG`.
Подробности формата и альтернативы pi: [README.md](README.md#настройка-ds_claude-и-ai_claude).

```sh
ds_claude --launcher-info
ai_claude --launcher-info
```

## 5. Запустите из своего проекта

```sh
cd /path/to/your/project
gpt_claude
# Или другая команда:
ds_claude
ai_claude
```

Для восстановления сессии:

```sh
gpt_claude --resume
```

Для GPT-запросов с длительными промежутками без сетевых данных:

```sh
GPT_CLAUDE_IDLE_TIMEOUT_MS=300000 GPT_CLAUDE_DEBUG=1 gpt_claude --resume
```

Это явная установка стандартного пятиминутного таймаута отсутствия сетевых данных. Служебные SSE-комментарии
тоже подтверждают активность соединения. Общий предел одной попытки — 15 минут;
он меняется через `GPT_CLAUDE_RESPONSE_TIMEOUT_MS`. Таймауты относятся к запросу,
а не ко всей задаче. На прямые подключения DS и AI эти переменные не влияют.
Новые настройки применяются после перезапуска лаунчера.

Путь к журналу метаданных показывается при запуске. По умолчанию это отдельный
JSONL-файл в `~/.config/gpt_claude/logs`: без промптов, reasoning, аргументов
инструментов и авторизационных заголовков. Для отключения сохранения задайте
`GPT_CLAUDE_LOGGING=0`.

## 6. Обновление и тесты

Из папки репозитория:

```sh
git pull --ff-only
npm ci
npm test
python3 -m unittest discover -p 'test_*.py'
```

Сквозные тесты расходуют токены подписки:

```sh
npm run smoke:gpt
npm run smoke:resume
npm run smoke:partial
```

Не копируйте `node_modules` с Windows: устанавливайте зависимости на самом Mac.
Ночные задачи требуют доступной сети и работающего Mac; не допускайте сна машины.
Сквозная проверка именно на macOS ещё не подтверждена.
