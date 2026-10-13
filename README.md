# jobhunter

Сборщик вакансий + интерактивный Telegram-бот для поиска работы:
**Middle/Senior DevOps / SRE / Platform Engineer**, удалённо, крипто/web3/fintech, от $3–4k net.

Полностью автономен: ходит по ATS и агрегаторам, нормализует, дедуплицирует, фильтрует,
оценивает (0–100 с прозрачной раскладкой), складывает в SQLite и присылает карточки в Telegram.
Работает **без LLM-ключа** (Gemini — опция, только перескор и черновики откликов).

---

## Быстрый старт

### Docker (рекомендуемый способ)

```bash
cd jobhunter
cp .env.example .env          # вписать токен и chat id (или использовать ../.env)
docker compose up -d --build
docker compose logs -f
```

Проверка:

```bash
curl -s localhost:8090/healthz   # "ok" + HTTP 200 (503 = degraded)
open http://localhost:8090/      # веб-интерфейс (лента + воронка)
docker compose ps                # STATUS: healthy
```

Веб-UI живёт на том же порту, что `/healthz`. Опционально защитите его
`WEB_TOKEN` в `.env` — без токена интерфейс открыт (удобно локально).

### Без Docker (локальная разработка)

```bash
cd jobhunter
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m app.main --print-config   # что реально подхватилось из .env/YAML
python -m app.main --once --dry-run # один полный сбор, ничего не пишет в БД и не шлёт
python -m app.main                  # обычный режим: планировщик + бот
```

Полезно: `--no-bot` (только сбор по расписанию), `--no-llm` (отключить Gemini).

---

## Структура

```
jobhunter/
├── app/
│   ├── main.py            # точка входа: web+/healthz, --once/--dry-run/--print-config
│   ├── scheduler.py       # APScheduler: сбор, дайджест, weekly, фоллоу-апы, бэкап
│   ├── config.py          # pydantic-settings (.env) + YAML profile/sources
│   ├── models.py          # единая схема Job, canonical_url, статусы воронки
│   ├── http.py            # httpx: таймауты, экспоненциальный retry, ETag-кэш, троттлинг
│   ├── logging_setup.py   # структурированные логи + маскирование секретов
│   ├── bot/               # handlers (aiogram 3), keyboards, render (HTML), sender (очередь)
│   ├── web/               # FastAPI UI: лента, фильтры, статусы, /api/*
│   ├── sources/           # плагины: greenhouse, ashby, lever, remoteok, wwr_rss, hh, telegram
│   ├── pipeline/          # normalize → dedup → filters → runner
│   ├── scoring/           # прозрачный скоринг 0–100 с breakdown
│   ├── llm/               # Gemini REST + бюджет + кэш по хэшу описания
│   └── storage/           # SQLite (aiosqlite) + миграции + бэкапы
├── config/profile.yaml    # профиль кандидата, фильтры, веса скоринга
├── config/sources.yaml    # источники: kind/token/enabled
├── prompts/               # rescore.md, draft.md — системные промпты LLM
├── tests/                 # pytest: normalize/dedup/filters/scoring/sources/http/bot
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Переменные окружения

Секреты читаются из `.env`. Файл ищется в двух местах: `jobhunter/.env` и рядом с
репозиторием (`../.env`) — первый найденный выигрывает, отсутствующие пропускаются.
Значения секретов никогда не попадают в логи ([`app/logging_setup.py`](app/logging_setup.py)).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TELEGRAM_ENABLED` | `false` | явный рубильник бота |
| `TELEGRAM_BOT_TOKEN` | — | токен BotFather |
| `TELEGRAM_CHAT_ID` | — | chat id владельца; пустой = отвечают всем |
| `GEMINI_API_KEY` | — | пусто → LLM выключен, всё остальное работает |
| `GEMINI_MODEL` | `gemini-2.5-flash` | модель для перескора/черновиков (`GET /v1beta/models` — список доступных) |
| `LLM_DAILY_BUDGET_USD` | `0.50` | дневной бюджет, сверх него LLM молчит |
| `TG_API_ID`, `TG_API_HASH` | — | нужны только для Telethon (каналы) |
| `TG_SESSION_PATH` | `data/jobhunter-telethon` | путь `.session`-файла |
| `TIMEZONE` | `Europe/Moscow` | TZ планировщика |
| `DB_PATH` | `data/jobhunter.db` | каталог создаётся автоматически |
| `BACKUP_DIR`, `BACKUP_KEEP` | `data/backups`, `7` | ежедневный бэкап в 03:30 с ротацией |
| `LOG_LEVEL`, `LOG_JSON` | `INFO`, `false` | `LOG_JSON=true` — JSON-логи для логи-агента |
| `DIGEST_TIME` | `09:00` | время дайджеста |
| `WEEKLY_REPORT_WEEKDAY` | `0` | 0 = понедельник |
| `ALERTS_PER_HOUR` | `3` | потолок мгновенных алертов (score ≥ `ALERT_SCORE`) |
| `HTTP_TIMEOUT`, `HTTP_MAX_RETRIES` | `20.0`, `3` | сеть |
| `FETCH_CONCURRENCY` | `4` | параллельность источников |
| `DOMAIN_MIN_INTERVAL` | `1.2` | пауза между запросами к одному домену |
| `FETCH_INTERVAL_MINUTES` | `60` | период сбора (минимум 5) |
| `HEALTHZ_PORT` | `8080` | порт веб-UI и `/healthz` внутри контейнера |
| `WEB_ENABLED` | `true` | рубильник веб-интерфейса |
| `WEB_TOKEN` | — | shared secret; пусто = без авторизации |
| `FOLLOWUP_DAYS` | `6` | напоминание о фоллоу-апе |

Бот отвечает **только владельцу** (`TELEGRAM_CHAT_ID`, можно список через запятую):
остальные получают тишину, а не «неизвестная команда».

---

## Команды бота

`/start` `/help` `/digest` `/new` `/top [N]` `/saved` `/applied` `/pipeline`
`/status <id> <статус>` `/stats` `/sources` `/filters` `/block <компания>` `/unblock <компания>`
`/addchannel <имя>` `/rmchannel <имя>` `/suspicious` `/export` `/pause` `/resume` `/run`

Кнопки карточки: `🔗 Открыть · ⭐ Сохранить · ✅ Откликнулся · 📝 Черновик отклика · ❌ Не интересно`.

`❌` спрашивает причину (не remote / стек не тот / низкая вилка / компания не нравится / другое).
«компания не нравится» добавляет компанию в блок-лист, остальные причины обучают стоп-слова —
то есть отказы реально влияют на будущий скоринг.

Воронка: `new → saved → applied → screening → tech → offer | rejected | ignored`.
После `applied` включается таймер фоллоу-апа (6 дней) с кнопками `Отправил` / `Через 3 дня` / `Закрыть`.

---

## Как добавить источник

1. Открыть [`config/sources.yaml`](config/sources.yaml) и добавить пункт:

```yaml
  - name: greenhouse_example
    kind: greenhouse          # greenhouse | lever | ashby | remoteok | wwr_rss | hh | habr | telegram
    enabled: true
    token: example            # слаг ATS-борда
    company: Example          # человекочитаемое имя (для карточек)
```

2. Проверить, что токен живой, **не угадывая**:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true'
```

3. Новый `kind` = один класс в `app/sources/` с `async def fetch() -> list[RawJob]`
   (наследник `SourcePlugin`) плюс запись в `REGISTRY` ([`app/sources/__init__.py`](app/sources/__init__.py)).
   Ошибка одного источника не роняет сбор: она уходит в `source_health` и видна в `/sources` (✅/❌).

`enabled: false` — источник собран, но выключен (например, Lever: публичные слоги крипто-компаний
дали 404, а hh.ru — 403 с этого IP). Ничего не включено «на глаз».

---

## Хабр Карьера

`kind: habr` читает `https://career.habr.com/vacancies/rss` (200 без авторизации, 50 объявлений;
публичный JSON-API `/api/v1/vacancies` отдаёт 401 без сессионной куки). Две особенности формата
закрыты в [`app/sources/habr.py`](app/sources/habr.py):

- зарплата по-русски «от 300 000 ₽ до 500 000 ₽» — общий `parse_salary` её не видит (его регулярки
  требуют символ валюты **перед** цифрами), поэтому числа вытаскиваются в плагине и передаются
  явным диапазоном в `RUB`;
- «Можно удал**ё**нно» — общий `detect_remote` знает только `удаленн` (через «е»), поэтому флаг
  выставляется явно; если удалёнки нет, ставится `None` («неизвестно»), а не `False`, чтобы офисные
  объявления Хабра не отсекались жёстким фильтром.

---

## Telegram-каналы

Работают **без Telethon**: парсится публичное веб-превью `https://t.me/s/<channel>`
([`app/sources/telegram.py`](app/sources/telegram.py)) — ни сессии, ни `TG_API_ID` не нужны.
Каналы берутся из `queries` в `sources.yaml` (проверены `devops_vacancies`, `habr_career`,
`getjob`, `hrlunapark` / [Luna Park](https://t.me/hrlunapark)) и расширяются из бота:
`/addchannel <имя>` / `/rmchannel <имя>` — список живёт в таблице `kv` и подмешивается перед
каждым сбором. Для Luna Park парсер читает `tl;dr:` как заголовок и компанию из строки
вида `Reelful — …`, а превью листается на ~5 страниц (`?before=`), иначе видны только
последние ~20 постов. Пустой или недоступный канал не валит остальные:
источник падает только когда не спарсилось ничего.

`min_score` больше **не выкидывает** вакансии из БД: низкий скор всё равно попадает в веб-UI
(фильтр по каналу/score), а порог режет только дайджест, алерты в Telegram и LLM-перескор.

Telethon остаётся опциональным апгрейдом (нужен для приватных каналов и истории глубже превью):

```bash
pip install -e ".[telethon]"
# в .env: TG_API_ID / TG_API_HASH (https://my.telegram.org), TG_SESSION_PATH
```

Авторизация один раз интерактивно (код подтверждается в самом Telegram), создаётся
`data/jobhunter-telethon.session` — его **нельзя** коммитить.

---

## Деплой

### Docker Compose

`docker-compose.yml` уже делает всё нужное: `restart: unless-stopped`, том `./data:/app/data`,
`env_file: ../.env`, `TZ=Europe/Moscow`, healthcheck по `/healthz`.
Порт наружу — `8090:8080` (переопределяется переменной `HEALTHZ_HOST_PORT`),
потому что 8080/8081/8082 на хосте уже заняты другими сервисами.

```bash
docker compose up -d --build     # деплой / обновление
docker compose down              # остановка (данные остаются в ./data)
docker compose exec jobhunter python -m app.main --once --dry-run
```

### systemd (альтернатива без Docker)

```ini
# /etc/systemd/system/jobhunter.service
[Unit]
Description=jobhunter vacancy bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=appuser
WorkingDirectory=/opt/jobhunter
EnvironmentFile=/opt/jobhunter/.env
ExecStart=/opt/jobhunter/.venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now jobhunter
```

### Healthcheck

`GET /healthz` на `HEALTHZ_PORT` (в контейнере 8080, на хосте 8090) — 200, пока процесс жив.
Реализован на голом asyncio, без зависимостей ([`app/main.py`](app/main.py)).

---

## Бэкапы

Ежедневно в 03:30 планировщик делает снапшот SQLite в `BACKUP_DIR` и держит
последних `BACKUP_KEEP` (по умолчанию 7). Ручной запуск:

```bash
docker compose exec jobhunter python -c \
  "import asyncio; from app.config import get_settings; from app.storage import Database; \
   s=get_settings(); d=Database(s.db_path); asyncio.run(d.connect()); \
   print(d.backup(s.backup_dir, s.backup_keep))"
```

Файлы лежат в `data/backups/` — том примонтирован наружу, так что бэкапы переживают `docker compose down`.

---

## Тесты и линтер

```bash
.venv/bin/ruff check .    # All checks passed!
.venv/bin/pytest -q       # 295 passed
```

Покрытие: нормализация (URL/HTML/зарплаты/даты), дедупликация, жёсткие фильтры,
скоринг с breakdown, все парсеры источников + happy/failure пути `fetch()` через моки,
`HttpFetcher` (ETag/304, retry, 429, троттлинг по домену), SQLite-слой и
обработчики бота, прогнанные через настоящий `Dispatcher` aiogram с фейковой сессией.

---

## Известные ограничения

- Источники: 8 Greenhouse-бордов, 1 Ashby, RemoteOK, WWR RSS, Хабр Карьера, 4 Telegram-канала
  (включая Luna Park — там чаще ML/full-stack, чем DevOps, так что score часто низкий).
  Lever выключен (публичные слоги крипто-компаний дают 404), hh.ru выключен (API отвечает
  403 `{"errors":[{"type":"forbidden"}]}` на все варианты UA/HTTP — блокировка по IP/гео;
  лечится прокси).
- Роль больше не жёсткий фильтр: несовпадение = флаг + штраф `-20` в скоринге
  (`role_strict: false` в `profile.yaml`), иначе 883 из 898 вакансий отсеивались только из-за
  слова в заголовке (наборщик слов Bybit даёт `manager`/`product`/`director`).
- Вилка считается **чистыми**: `$3 000–$4 000 net` при `tax_rate: 0.13` — это `≈$3 448–$4 598 gross`
  в месяц и `$41 379–$55 172/год gross`; разбор виден в карточке и в `/filters`.
  Зарплаты в RUB пересчитываются в USD через курс-кэш (`RUB → 0.011`).
- LinkedIn не парсится сознательно (антибот).
- Вилка в агрегаторах часто отсутствует → скоринг опирается на ключевые слова и стек,
  а не на зарплату; при отсутствии вилки карточка не отбрасывается.
- Вакансии с гео-ограничением получают пометку в карточке, а не молча отсеиваются.
- LLM не выдумывает опыт: черновик собирается из блока `facts:` профиля и текста вакансии.
# jobhunter
