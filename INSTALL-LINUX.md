# Установка на Linux

Установщик не требует root: всё ставится в домашний каталог. Права администратора
нужны только если Python 3.10+ ещё не установлен и вы ставите его пакетным менеджером.

## 1. Проверьте зависимости

```sh
bash install-linux.sh --check-only
```

Установщик проверит Python, Node.js, Git и Claude Code, ничего не скачивая
и не меняя PATH. Требования:

- **Python 3.10+** — обязателен для `ds_claude`, `or_claude` и `ai_claude`.
  В современных дистрибутивах уже есть; если нет:
  `sudo apt-get install -y python3` (Debian/Ubuntu),
  `sudo dnf install -y python3` (Fedora/RHEL),
  `sudo pacman -S --needed python` (Arch),
  `sudo zypper install -y python3` (openSUSE).
- **Node.js 20.18.1+ с npm** — нужен только для `gpt_claude`. Установщик
  скачает официальную сборку с nodejs.org, если подходящей нет.
- **Claude Code** — нативный клиент; установщик запустит официальный
  `https://claude.ai/install.sh`, если `claude` не найден.
- **Git** — только чтобы склонировать или обновить репозиторий.

## 2. Запустите установщик

```sh
git clone https://github.com/lrchome/claude.git
cd claude
bash install-linux.sh
```

Что делает установщик:

1. Находит `python3` версии 3.10+ и проверяет, что команда `python3` доступна обёрткам.
2. Если Node.js нет или он старше 20.18.1 — скачивает последний LTS
   с nodejs.org для `x86_64` или `aarch64`, сверяет SHA256 по официальному
   `SHASUMS256.txt`, распаковывает в `~/.local/share/claude-launchers/node`
   и создаёт ссылки в `~/.local/bin`.
3. Если `claude` не найден — скачивает официальный установщик Anthropic,
   проверяет, что это действительно shell-скрипт, и запускает его.
4. При необходимости выполняет `npm ci` в папке репозитория.
5. Создаёт обёртки `gpt_claude`, `ai_claude`, `ds_claude`, `or_claude`
   через `install-unix.py` и проверяет каждую командой `--version`.

Каталог для обёрток можно изменить:

```sh
bash install-linux.sh --bin-dir "$HOME/bin"
```

## 3. Добавьте каталог в PATH

Если `~/.local/bin` ещё не в PATH, установщик напечатает строку для вашего shell:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Добавьте её в `~/.profile`, `~/.bashrc` или `~/.zshrc` — в зависимости от shell.
Проверьте команды:

```sh
gpt_claude --version
ds_claude --version
or_claude --version
ai_claude --version
```

## 4. Настройте провайдеров

`ds_claude` и `or_claude` умеют сохранить ключ и выбрать модель сами:

```sh
ds_claude --setkey
or_claude --setkey
ds_claude --listmodels
or_claude --listmodels
ds_claude --setmodel deepseek-v4-pro
or_claude --setmodel <id-модели>
```

Ключ пишется в `OPENCODE_CONFIG` или `~/.config/opencode/opencode.json`,
выбранная модель — в `~/.config/claude-launchers/state.json`. Для `ai_claude`
и `{env:...}`/`{file:...}` подробности в
[README.md](README.md#настройка-ds_claude-or_claude-и-ai_claude).
Для GPT-аккаунта:

```sh
gpt_claude --login
gpt_claude --models
```

## 5. Запустите из своего проекта

```sh
cd /path/to/your/project
ds_claude
# или:
or_claude
ai_claude
gpt_claude
```

## 6. Обновление и тесты

```sh
git pull --ff-only
bash install-linux.sh --check-only
npm ci
npm test
python3 -m unittest discover -p 'test_*.py'
```

Если переместили папку репозитория, повторите `bash install-linux.sh`:
обёртки содержат абсолютный путь к `launch.py`.
