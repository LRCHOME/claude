# Claude Desktop через DeepSeek (неофициально)

> **Важно.** Это не поддерживаемый Anthropic сценарий. Документация Claude
> прямо говорит: *«Anthropic doesn't … support routing Claude Code to
> non-Claude models through any gateway»*. Режим третьего провайдера
> предназначен для Claude-моделей на вашем шлюзе. Схема ниже работает
> технически, но может сломаться после обновления приложения. Трафик бесед
> уходит в DeepSeek и оплачивается по вашему договору с ним.

Шлюз `desktop_gateway.py` принимает от приложения Claude-модели
(`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`), подменяет их
на модели DeepSeek и проксирует запросы в Anthropic-совместимый endpoint
DeepSeek. Стриминг и вызовы инструментов проходят без изменений.

## 1. Ключ DeepSeek

Шлюз берёт ключ из той же конфигурации, что и `ds_claude`:

```powershell
ds_claude --setkey
```

или задайте `DEEPSEEK_API_KEY` в текущем терминале. Ключ никогда не
передаётся аргументом командной строки и не печатается.

## 2. Запустите шлюз

Windows (команда из `bin`, уже в PATH после установки):

```powershell
claude_gateway
```

Linux / macOS:

```sh
python3 desktop_gateway.py
```

Проверка ключа и endpoint без запуска сервера:

```powershell
claude_gateway --check
```

Показать значения для окна настройки Desktop:

```powershell
claude_gateway --print-config
```

По умолчанию шлюз слушает `http://127.0.0.1:8787`, принимает ключ `local`
(схема `bearer` или `x-api-key`) и перенаправляет в
`https://api.deepseek.com/anthropic`. Полезные флаги:

| Флаг | По умолчанию | Значение |
| --- | --- | --- |
| `--port` | `8787` | порт шлюза |
| `--host` | `127.0.0.1` | адрес; не открывайте наружу без нужды |
| `--token` | `local` | ключ, который приложение отправляет шлюзу |
| `--upstream` | `https://api.deepseek.com/anthropic` | endpoint провайдера |
| `--opus-model` | `deepseek-v4-pro` | что отвечает на `claude-opus-*` |
| `--sonnet-model` | `deepseek-v4-pro` | что отвечает на `claude-sonnet-*` |
| `--haiku-model` | `deepseek-flash` | что отвечает на `claude-haiku-*` |
| `--default-model` | `deepseek-v4-pro` | прочие Claude-ID |
| `--strip` | `context_management,mcp_servers,container,fallbacks,task_budget,betas` | поля, которые удаляются из запроса |

Шлюз должен быть запущен всё время, пока приложение работает: при старте
Claude Desktop проверяет доступность base URL.

## 3. Настройте Claude Desktop

1. Установите обычный Claude Desktop и **не входите** в аккаунт Anthropic
   (в третьем режиме вход не нужен).
2. **Help → Troubleshooting → Enable Developer Mode** — приложение
   перезапустится.
3. **Developer → Configure Third-Party Inference…**
4. В разделе **Connection**:
   - **Inference provider** → `Gateway`;
   - **Gateway base URL** → `http://127.0.0.1:8787` (без `/v1`);
   - **Credential kind** → `Static API key`;
   - **Gateway API key** → `local`;
   - **Auth scheme** → `bearer`.
5. Модели: оставьте включённым автообнаружение — шлюз отдаёт
   `GET /v1/models` со списком `claude-opus-5-5`, `claude-sonnet-5-5`,
   `claude-haiku-5-5`. Если поле `inferenceModels` заполняется вручную,
   перечислите эти же ID.
6. **Apply Changes → Relaunch now**.

## 4. Что происходит с запросами

| Приложение отправляет | Уходит в DeepSeek |
| --- | --- |
| `claude-opus-5-5`, `claude-fable-*`, `claude-mythos-*` | `deepseek-v4-pro` |
| `claude-sonnet-5-5` | `deepseek-v4-pro` |
| `claude-haiku-5-5` | `deepseek-flash` |
| `deepseek-*` (если задан напрямую) | без изменений |

Перед отправкой из тела удаляются beta-поля, которых нет у провайдера
(`context_management`, `mcp_servers`, `container`, `fallbacks`,
`task_budget`, `betas`). Список меняется флагом `--strip`;
`--strip ""` отключает удаление. Заголовок `anthropic-beta` не пересылается.

`POST /v1/messages/count_tokens` DeepSeek не предоставляет, поэтому шлюз
возвращает локальную оценку (длина JSON / 4). Это влияет только на
индикацию контекста, не на сам ответ.

## 5. Если не работает

| Симптом | Причина и действие |
| --- | --- |
| `401 invalid gateway token` в приложении | Ключ в окне настройки не совпадает с `--token` (по умолчанию `local`) |
| `Gateway was unreachable` | Шлюз не запущен или другой порт; проверьте `--port` и окно приложения |
| `502 upstream unreachable` | Нет доступа к `api.deepseek.com` или неверный `--upstream` |
| `400` про `thinking`/неизвестное поле | Добавьте поле в `--strip`, например `--strip context_management,mcp_servers,container,fallbacks,task_budget,betas,thinking` |
| Пустой пикер моделей | Проверьте `GET http://127.0.0.1:8787/v1/models`; автопоиск в приложении включён |
| Долгие ответы обрываются | Проверьте сеть; клиентский таймаут Desktop фиксирован (15 минут) |

Логи шлюза пишутся в stderr: видно, какая модель была запрошена, во что
преобразована и какой статус вернул провайдер. Тела запросов и ключи не
логируются.

## 6. Ограничения

- В третьем режиме недоступны облачные окружения Anthropic и Remote Control.
- Часть функций Desktop (артефакты, beta-инструменты) может не работать с
  DeepSeek; это зависит от того, что приложение присылает в запросе.
- Качество и поведение инструментов определяет DeepSeek, а не Anthropic.
- Шлюз слушает только loopback и требует токен, но любой процесс под вашей
  учётной записью может обратиться к нему, если знает токен.
- Anthropic не поддерживает маршрутизацию на не-Claude модели; при обновлении
  Desktop формат запросов может измениться.

Для повседневной работы надёжнее CLI-лаунчеры: `ds_claude`, `or_claude`,
`ai_claude` — там `ANTHROPIC_BASE_URL` официально поддерживается.
