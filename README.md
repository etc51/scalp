# T-Bank latency check

Небольшой модуль для проверки, годится ли связка `сервер в Москве -> T-Invest API` хотя бы для умеренного скальпинга.

Что умеет:

- мерить `DNS`, `TCP connect`, `TLS handshake`
- проверять авторизацию и `gRPC unary` через `GetAccounts`
- замерять `GetTradingStatus`, `GetLastPrices`, `GetOrderBook`
- поднимать `MarketDataStream` и `OrderStateStream`
- писать JSON-отчет для анализа
- по умолчанию безопасен: ночью и вне сессии не пытается отправлять реальные заявки

Что можно проверить вне биржевой сессии:

- сетевую задержку до `invest-public-api.tbank.ru:443`
- TLS и доступность API
- валидность токена
- unary-вызовы API
- открытие стримов и получение первых ответов
- торговый статус инструмента

Что вне сессии проверить полноценно нельзя:

- реальную пригодность для скальпинга по потоку стакана
- latency полного цикла `выставление -> событие по заявке -> отмена -> подтверждение отмены`
- риск случайного исполнения и качество отмены в живом рынке

Если хочешь финально ответить на вопрос "можно ли скальпить", модуль нужно прогнать еще раз во время `NORMAL_TRADING`.

## Быстрый запуск на Linux-сервере

Сначала проект нужно загрузить на сервер. Твоя ошибка `cd: /opt/tbank-latency-check: No such file or directory` означает, что папка и файлы еще не были созданы на сервере.

Пример загрузки с локальной машины:

```bash
scp -r . root@YOUR_SERVER_IP:/opt/tbank-latency-check
```

Если папки `/opt/tbank-latency-check` еще нет, сначала на сервере выполни:

```bash
mkdir -p /opt/tbank-latency-check
```

На Ubuntu также нужен пакет `python3.12-venv`, иначе `venv` не создастся.

```bash
apt update
apt install -y python3.12-venv python3-pip

mkdir -p /opt/tbank-latency-check
cd /opt/tbank-latency-check

# сюда уже должны быть загружены файлы проекта

python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

cp .env.example .env
```

Заполни `.env`:

```env
TBANK_TOKEN=...
TBANK_SSL_ROOTS_PATH=
TBANK_ACCOUNT_ID=...
TBANK_TICKER=SBER
TBANK_CLASS_CODE=TQBR
```

`TBANK_SSL_ROOTS_PATH` можно оставить пустым. Если на сервере нет доверия к российской цепочке сертификатов, модуль попытается автоматически использовать bundled `RussianTrustedRootCA.pem` из SDK. Это нужно для ошибок вида `CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain`.

Базовый прогон без заявок:

```bash
source .venv/bin/activate
python3 -m tbank_latency_check --write-report
```

Более плотный прогон:

```bash
source .venv/bin/activate
python3 -m tbank_latency_check --iterations 30 --stream-iterations 5 --write-report
```

Ордерный тест только в живую сессию и только осознанно:

```bash
source .venv/bin/activate
python3 -m tbank_latency_check --enable-order-tests --order-iterations 5 --write-report
```

## Что смотреть в отчете

- `dns_resolve_ms`, `tcp_connect_ms`, `tls_handshake_ms`
- `grpc_get_accounts_ms`
- `grpc_get_trading_status_ms`
- `grpc_get_last_prices_ms`
- `grpc_get_order_book_ms`
- `market_data_stream_first_response_ms`
- `market_data_stream_first_orderbook_ms`
- `order_state_stream_first_response_ms`
- `post_order_async_ms`
- `cancel_order_ms`
- `post_to_order_state_ms`
- `cancel_to_order_state_ms`

Рабочая инженерная интерпретация:

- если вне сессии уже плохие `TCP/TLS/gRPC`, дальше в скальпинг идти рано
- если во время `NORMAL_TRADING` `p95` по выставлению и отмене стабильно выше `200-250 ms`, для нормального скальпинга это уже слабый результат
- если рыночный поток приходит редко или нестабильно, это ограничит стратегию даже при хорошем `ping`

## Важные замечания

- Для честной latency-оценки используй отдельный аккаунт или хотя бы не торгуй руками параллельно.
- Ордерный тест идет только по явному флагу `--enable-order-tests`.
- Скрипт пытается ставить максимально пассивную лимитную заявку и сразу отменять ее, но это все равно реальная торговая операция.
- Финальный вывод по скальпингу делай только по результатам во время живой торговой сессии.

## Если видишь CERTIFICATE_VERIFY_FAILED

Симптом:

- `SSL_ERROR_SSL`
- `CERTIFICATE_VERIFY_FAILED`
- `self signed certificate in certificate chain`

Что делать:

```bash
cd /opt/tbank-latency-check
source .venv/bin/activate
python3 -m tbank_latency_check --iterations 5 --stream-iterations 2 --write-report
```

Начиная с этой версии модуль сам пытается подключить bundled root CA из SDK.

Если нужно задать путь вручную:

```bash
cd /opt/tbank-latency-check
source .venv/bin/activate
export TBANK_SSL_ROOTS_PATH="$(python3 - <<'PY'
from importlib.resources import files
print(files('t_tech.invest.certs').joinpath('RussianTrustedRootCA.pem'))
PY
)"
python3 -m tbank_latency_check --iterations 5 --stream-iterations 2 --write-report
```

## First Bot Skeleton

В репозитории теперь есть первый каркас бота в пакете `moex_scalper`.

Что уже есть:

- `paper` режим по умолчанию
- `live` режим с реальными market orders
- стратегия под `moderate scalping`, а не под HFT
- учет комиссии Premium для акций Мосбиржи по умолчанию как `0.04%` на сторону (`4.0 bps`)
- фильтр по спреду, дисбалансу стакана, короткому импульсу, минимальному net take-profit после комиссии и `time-stop`
- отдельный target-buffer по чистой цели после комиссии через `SCALPER_TARGET_NET_TAKE_PROFIT_BUFFER_BPS`, чтобы autotune не оставлял стратегию без запаса на издержки
- опциональный minute-regime filter через `SCALPER_REGIME_FILTER_MODE` для новых входов: `off`, `trend_not_bearish`, `trend_bullish`, `macd_positive`, `rsi_50_70`
- paper-контур умеет считать gross leverage и buying power через `SCALPER_PAPER_MAX_GROSS_LEVERAGE`
- дневной лимит убытка и cooldown
- watchdog и dashboard теперь отдельно подсвечивают конфиг, который сам по себе блокирует новые входы

Быстрая проверка конфига:

```bash
python3 -m moex_scalper doctor --mode paper
```

Безопасный запуск в paper-режиме:

```bash
python3 -m moex_scalper run --mode paper
```

Version-controlled paper risk profile:

- tracked file `config/paper_profile.env` now fixes the current paper contour at `300000 RUB` and `1.2x` max gross leverage
- current strategy decision is deliberately conservative: keep paper margin at `1.2x` while we validate that expectancy is truly positive after Premium roundtrip fees
- only consider raising to `1.5x` after at least `100+` closed paper trades on the current logic, `profit factor >= 1.15`, positive expectancy and no repeated daily loss-limit breaches
- this file contains no secrets and is intended to be changed from GitHub
- `.env` still stores token, account and machine-local settings
- on startup the bot loads `.env` first and then applies the tracked paper profile, so nightly GitHub auto-updates can change budget/leverage without touching secrets

Version-controlled strategy profile:

- tracked file `config/strategy_profile.env` now overlays a safe subset of strategy and paper-risk keys after `.env`
- current first use is deliberate downside control: `SCALPER_MAX_SPREAD_BPS=1.5` is now tracked from GitHub because the corrected paper replay shows wide-spread entries remain a fee drag
- intraday guard settings can also live there, so server updates no longer require manual `.env` edits for routine paper strategy tweaks
- pre-existing real environment variables still win, so an explicit server-side override can temporarily bypass the tracked profile if needed

Осторожный live-запуск:

```bash
python3 -m moex_scalper run --mode live
```

Важно:

- разрешена только `paper`-торговля, live-режим остается заблокирован до явного разрешения пользователя
- short по акциям по умолчанию выключен
- `SCALPER_MIN_NET_TAKE_PROFIT_BPS` задает минимальную чистую цель в `bps` после roundtrip-комиссии Premium; это режет слишком тесные сделки даже если импульс формально проходит
- `SCALPER_MIN_EXPECTED_EDGE_BPS` теперь реально фильтрует слабые входы: ожидаемый edge ограничивается самим `take-profit`, поэтому слишком маленький импульс уже не проходит только из-за высокого configured `take-profit`
- `SCALPER_TARGET_NET_TAKE_PROFIT_BUFFER_BPS` задает желаемый запас сверх этого floor; `doctor`, `summary`, `dashboard` и autotune показывают и используют рекомендуемый минимальный `take-profit`
- `SCALPER_REGIME_FILTER_MODE` влияет только на новые входы и использует только уже закрытую предыдущую 1m-минуту инструмента:
  - `off` — без regime-filter
  - `trend_not_bearish` — не входить, если предыдущая закрытая минута выглядит bearish по RSI/EMA/MACD regime
  - `trend_bullish` — входить только если предыдущая закрытая минута выглядит bullish по RSI/EMA/MACD regime
  - `macd_positive` — входить только если у предыдущей закрытой минуты `MACD histogram > 0`
  - `rsi_50_70` — входить только если у предыдущей закрытой минуты `RSI14` в диапазоне `50-70`
- `SCALPER_INTRADAY_TICKER_LOSS_LIMIT_RUB` и `SCALPER_INTRADAY_TICKER_MAX_CONSECUTIVE_LOSSES` добавляют узкий intraday-guard по отдельному тикеру:
  - guard влияет только на новые входы по конкретному тикеру
  - уже открытая или восстановленная после рестарта позиция не закрывается этим guard автоматически
  - остальные тикеры из watchlist продолжают работать
- это рабочий paper-контур для накопления статистики и data-driven тюнинга

## GitHub Auto-Update On Server

Теперь репозиторий можно держать источником истины, а сервер обновлять из GitHub автоматически ночью.

Что для этого есть в проекте:

- `scripts/run_scalper_service.sh` запускает бота как `systemd`-сервис
- `scripts/update_from_github.sh` делает `git pull`, обновляет зависимости и перезапускает сервис
- `scripts/install_server_services.sh` ставит `systemd`-юниты
- `deploy/systemd/moex-scalper-update.timer` запускает ночную проверку обновлений в `03:30` по `Europe/Moscow`
- `deploy/systemd/moex-scalper-preopen.timer` прогоняет `doctor + watchdog + summary` в `10:05` по будням перед окном новых входов

Базовая схема на сервере:

```bash
cd /opt
sudo git clone https://github.com/etc51/scalp.git tbank-latency-check
sudo chown -R codex:codex /opt/tbank-latency-check
cd /opt/tbank-latency-check
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
cp .env.example .env
```

Установка сервисов:

```bash
cd /opt/tbank-latency-check
bash scripts/install_server_services.sh /opt/tbank-latency-check codex
sudo systemctl start moex-scalper.service
```

Полезные команды:

```bash
sudo systemctl status moex-scalper.service
sudo systemctl restart moex-scalper.service
sudo systemctl stop moex-scalper.service
sudo journalctl -u moex-scalper.service -f
sudo journalctl -u moex-scalper-preopen.service -f
sudo systemctl status moex-scalper-update.timer
sudo systemctl start moex-scalper-update.service
```

Безопасность запуска:

- пока в `.env` стоит `SCALPER_MODE=paper`, сервис безопасно крутится в paper-режиме
- даже если кто-то переведет `.env` в `SCALPER_MODE=live`, бот не стартует без `SCALPER_ALLOW_LIVE_TRADING=1`
- `.env`, `reports/` и `runtime/` в git не коммитятся

Торговое окно для новых входов:

- бот может работать на сервере постоянно
- новые входы разрешены только по `Europe/Moscow`
- по умолчанию это `понедельник-пятница`, `10:15-17:45`
- уже открытые позиции бот продолжает сопровождать и закрывать даже вне окна новых входов

## Persistent Paper Stats

В paper-режиме бот теперь может крутиться `24/7` и переживать рестарты сервиса без потери виртуального портфеля.

Что сохраняется в `runtime/`:

- `paper_session.json` — текущий paper-кэш, открытые позиции, текущий дневной PnL, cooldown и последние paper-сделки
- `paper_trades.jsonl` — append-only журнал всех закрытых paper-сделок
- `stats/overview.json` — накопительная статистика за все время
- `stats/daily/YYYY-MM-DD.json` — дневная статистика по сделкам
- `dashboard_state.json` — текущее состояние для внешнего dashboard
- `doctor/latest.json` — последний session-readiness report по API, watchlist и стратегии перед торговым окном
- `analysis/latest.json` — nightly trade-analysis по реальным paper-сделкам
- `research/latest.json` — nightly indicator research по market snapshots внутри торгового окна
- `summary/latest.json` — nightly daily digest со сводкой состояния paper-контура и next action
- `tuning/latest.json` — последнее решение safe autotune по параметрам стратегии
- `restrictions/latest.json` — последнее решение по авто-ограничениям входов
- `restrictions/active.json` — активные ограничения новых входов по тикерам и часам
- `watchdog/latest.json` — последний health-check paper-контура и решение о self-heal

Это позволяет:

- держать paper-бота в фоне постоянно
- после ночного автообновления или перезапуска сервиса поднимать его с сохраненным состоянием
- смотреть накопительную статистику через dashboard и напрямую из `runtime/`

Важно:

- paper-день и daily-статистика считаются в `Europe/Moscow`, а не по UTC
- это относится к `paper_session.json`, `stats/daily/*`, dashboard-полю `today` и внутридневному cooldown

## Strategy Tuning

Для вывода paper-торговли в устойчивый плюс бот теперь может накапливать сырой поток top-of-book снапшотов в:

- `runtime/market/YYYY-MM-DD.jsonl`

По умолчанию runtime теперь пишет туда только снапшоты, попавшие в разрешенное окно новых входов по `Europe/Moscow`.
Это уменьшает off-hours/weekend шум в market-history, а dashboard отдельно показывает:

- сколько снапшотов всего обработано потоком
- сколько снапшотов реально записано в in-window sample за текущий московский день
- сколько полезных in-window снапшотов накоплено суммарно

Это дает базу для офлайн-тюнинга параметров стратегии без доступа к API.

Команда анализа:

```bash
python3 -m moex_scalper optimize --days 5 --write-report
```

Что делает:

- загружает накопленные market snapshots за день
- может смотреть rolling history по нескольким торговым дням
- отбрасывает snapshots вне разрешенного entry-window стратегии, чтобы weekend/off-hours не искажали оптимизацию
- прогоняет набор candidate-конфигураций стратегии
- сравнивает их по `score`, `equity_delta_rub`, `max_drawdown_rub`, `profit_factor`, `trade_count`
- строит `signal_coverage` по текущему конфигу: сколько in-window snapshots проходят spread / imbalance / impulse и доходят до готового сигнала
- пишет отчет в `runtime/optimizer/latest.json`
- добавляет `recommendation`, если найден конфиг, который достаточно лучше baseline

Если в выбранных файлах есть snapshots, но ни один не попал в торговое окно новых входов, optimizer вернет `status=no_entry_window_data` вместо ложного вывода по пустому sample.
`signal_coverage` отражает именно фильтры сигнала и помогает понять, почему сделок мало даже до учета cooldown, дневного лимита и max-open-positions.

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-optimize.service`
- автоматический таймер: `moex-scalper-optimize.timer`
- по умолчанию таймер срабатывает в `18:10 MSK` по `понедельник-пятница`

Это не гарантирует “магический плюс”, но дает нам уже не ручную настройку на глаз, а рабочий контур data-driven улучшения стратегии.

## Trade Analysis

Команда trade-analysis:

```bash
python3 -m moex_scalper analyze --days 5 --write-report
```

Что делает:

- читает реальные закрытые `paper`-сделки из `runtime/paper_trades.jsonl`
- строит summary по rolling window
- показывает worst/best breakdown по тикерам и по часам закрытия
- выделяет focus-зоны: слабый тикер, слабый час, проблемный `exit_reason`
- пишет отчет в `runtime/analysis/latest.json`

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-analyze.service`
- автоматический таймер: `moex-scalper-analyze.timer`
- по умолчанию таймер срабатывает в `18:06 MSK` по `понедельник-пятница`

## Indicator Research

Команда indicator-research:

```bash
python3 -m moex_scalper research --days 5 --write-report
```

Что делает:

- читает накопленные `runtime/market/*.jsonl`
- берет только snapshots внутри разрешенного entry-window
- агрегирует их в `1m`-свечи по тикерам
- считает `EMA`, `RSI14`, `MACD` и intraday volatility
- строит regime-replay preview по тем же snapshot-данным без lookahead bias
  - snapshot минуты `10:31` использует только индикаторы уже закрытой минуты `10:30`
  - сравнивает baseline против нескольких long-only regime-фильтров
- если установлен `pandas_ta`, может использовать его; иначе считает индикаторы через `pandas`
- пишет отчет в `runtime/research/latest.json`

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-research.service`
- автоматический таймер: `moex-scalper-research.timer`
- по умолчанию таймер срабатывает в `18:22 MSK` по `понедельник-пятница`

## Daily Summary

Команда daily-summary:

```bash
python3 -m moex_scalper summarize --write-report
```

Что делает:

- собирает `dashboard_state`, `doctor`, `analysis`, `optimizer`, `research`, `tuning`, `restrictions`, `watchdog`
- формирует единый nightly digest для оператора
- пишет `headline`, `focus` и `next_action`
- сохраняет отчет в `runtime/summary/latest.json`

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-summary.service`
- автоматический таймер: `moex-scalper-summary.timer`
- по умолчанию таймер срабатывает в `18:30 MSK` по `понедельник-пятница`, уже после nightly autotune

## Safe Paper Autotune

Теперь поверх `analysis` и `optimizer` есть еще один слой: safe autotune только для `paper`-режима.

Команда:

```bash
python3 -m moex_scalper tune --apply --write-report
```

Что делает:

- перед самим apply сначала обновляет свежие `analysis`, `optimizer` и `research`, чтобы autotune видел актуальные same-day отчеты даже если nightly timers сработали с задержкой или tune запустили вручную
- читает `runtime/analysis/latest.json` и `runtime/optimizer/latest.json`
- читает `runtime/research/latest.json` и может отдельно применить лучший regime-filter из regime-replay
- проверяет, что мы все еще в `paper`-режиме
- не меняет параметры, если идет торговое окно новых входов
- не меняет параметры, если в `paper_session.json` есть открытые позиции
- требует достаточный sample по сделкам
- если candidate из optimizer реально пригоден, обновляет параметры стратегии в `.env`
- если optimizer пока не готов, но у текущего конфига слишком маленький запас после комиссии, может все равно безопасно поднять `take-profit` до рекомендованного минимума
- если trade sample еще мал, но `optimizer.signal_coverage` показывает большой in-window sample с почти нулевым ready-rate и одним доминирующим blocker, autotune может сделать один безопасный шаг ослабления именно этого фильтра
- если research показывает устойчиво лучший regime-filter против baseline, autotune может сам включить его в `.env` через `SCALPER_REGIME_FILTER_MODE`
- пишет решение в `runtime/tuning/latest.json` и историю в `runtime/tuning/history.jsonl`
- после успешного apply перезапускает только `paper`-сервис бота

Для regime-autotune есть отдельные флаги:

- `SCALPER_AUTO_APPLY_REGIME_FILTER=1` — разрешает auto-apply research regime candidate
- `SCALPER_AUTO_TUNE_MIN_REGIME_DELTA_RUB=0` — минимальный `delta_vs_baseline_rub`, чтобы regime-filter считался достойным apply

Для coverage-fallback есть отдельные флаги:

- `SCALPER_AUTO_TUNE_USE_COVERAGE_FALLBACK=1` — разрешает один безопасный step-down по доминирующему blocker
- `SCALPER_AUTO_TUNE_COVERAGE_MIN_SNAPSHOTS=500` — минимальный in-window sample по market-data
- `SCALPER_AUTO_TUNE_COVERAGE_MAX_READY_RATE_PCT=0.10` — ready-rate должен быть почти нулевым
- `SCALPER_AUTO_TUNE_COVERAGE_MIN_BLOCK_SHARE_PCT=60` — один blocker должен доминировать в coverage-summary
- `SCALPER_AUTO_TUNE_COVERAGE_ALLOWED_BLOCK_REASONS=expected_edge_too_low,impulse_too_small,imbalance_too_low` — какие blockers можно ослаблять автоматически

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-tune.service`
- автоматический таймер: `moex-scalper-tune.timer`
- timer unit сохранен для ручного включения, но штатный nightly apply теперь делает `moex-scalper-govern.timer`

Это влияет только на новые входы после рестарта. Уже сохраненные открытые `paper`-позиции продолжают жить со своими параметрами, записанными в session-state.

## Nightly Governor

Теперь для nightly apply есть единый coordinator:

```bash
python3 -m moex_scalper govern --apply --write-report
```

Что делает:

- обновляет свежие `analysis`, `optimizer` и `research`
- строит preview для `tune` и `restrict`
- если готовы несколько кандидатов, выбирает только один nightly change
- приоритет у governor такой:
  - сначала global unblockers из `tuning` вроде `headroom_guard` или `coverage_unblocker`
  - затем более узкие `restrictions`
  - затем остальные tuning-candidates
- внутри этих правил governor еще считает явный score для `tuning` и `restrictions` по силе evidence, ожидаемой пользе и ширине изменения
- после любого apply governor теперь ждет новый post-change sample и не наслаивает следующую автоправку на тот же самый набор evidence
- для снятия этого guard нужен либо прирост закрытых paper-сделок, либо новый in-window market sample; текущее состояние guard видно в `governance`-отчете и на dashboard
- кроме guard, `governance` теперь ведет `active_experiment` по последнему auto-change: сколько уже post-change сделок накоплено, какой у них net PnL, expectancy и не выглядит ли последняя правка вредной
- пишет единый отчет в `runtime/governance/latest.json`
- требует не более одного nightly рестарта `paper`-сервиса

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-govern.service`
- автоматический таймер: `moex-scalper-govern.timer`
- по умолчанию таймер срабатывает в `18:24 MSK` по `понедельник-пятница`

Важно:

- `moex-scalper-tune.service` и `moex-scalper-restrict.service` остаются для ручного запуска и отладки
- installer теперь включает nightly `govern` timer и отключает отдельные nightly timers для `tune` и `restrict`, чтобы убрать двойные рестарты
- в `governance`-отчете и на dashboard видно `selected_action`, `deferred_actions`, `selection_reason` и scores обоих кандидатов

## Entry Restrictions

Теперь поверх `analysis` появился еще один безопасный слой: paper-only ограничения новых входов по слабым тикерам и часам.

Команда:

```bash
python3 -m moex_scalper restrict --apply --write-report
```

Что делает:

- читает `runtime/analysis/latest.json`
- смотрит на худшие buckets по тикерам и часам
- работает только в `paper`-режиме
- не меняет ограничения во время окна новых входов
- не меняет ограничения, если есть открытые позиции в `paper_session.json`
- может как добавить новые ограничения, так и снять старые, если свежий анализ больше не подтверждает слабые buckets
- если trade sample еще мал, может безопасно fallback-нуться к `optimizer.signal_coverage` и предложить ограничения по тикерам/часам с почти нулевым ready-rate и доминирующим микроструктурным blocker
- пишет решение в `runtime/restrictions/latest.json`
- сохраняет активные ограничения в `runtime/restrictions/active.json`
- после успешного apply перезапускает только `paper`-сервис бота

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-restrict.service`
- автоматический таймер: `moex-scalper-restrict.timer`
- timer unit сохранен для ручного включения, но штатный nightly apply теперь делает `moex-scalper-govern.timer`

Важно:

- это влияет только на новые входы
- уже открытые `paper`-позиции не закрываются и не пересчитываются из-за смены ограничений
- coverage-fallback по умолчанию очень консервативен и требует большой in-window sample по market-data, низкий signal-ready rate и высокий share одного доминирующего blocker

## Runtime Watchdog

Чтобы paper-бот реально жил `24/7`, поверх него есть watchdog-контур.

Команда:

```bash
python3 -m moex_scalper watchdog --write-report
```

Что делает:

- проверяет, существует ли `runtime/dashboard_state.json`
- смотрит, не устарел ли `updated_at` в `dashboard_state.json`
- отдельно проверяет, не пропал ли поток `market-data` даже если сам процесс еще жив
- проверяет, читается ли `paper_session.json`
- проверяет локальный `http://127.0.0.1:8080/health`
- пишет отчет в `runtime/watchdog/latest.json`
- если контур завис, server wrapper перезапускает `moex-scalper.service` и `moex-scalper-dashboard.service`

Технически это теперь устроено так:

- рантайм пишет heartbeat-состояние в `dashboard_state.json` даже если в эту секунду нет новой сделки
- `market_data.last_received_at` хранится отдельно, поэтому watchdog различает
  - `процесс жив, но поток данных свежий`
  - `процесс жив, но market-data завис`
- проверка свежести `market-data` включается только внутри торгового окна `10:15-17:45 MSK` по `понедельник-пятница`
- сам market-data stream пытается переподключиться до того, как watchdog дойдет до внешнего перезапуска

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-watchdog.service`
- автоматический таймер: `moex-scalper-watchdog.timer`
- по умолчанию таймер срабатывает каждые `5 минут`

Это не меняет торговую логику и не влияет на реальные сделки, потому что проект остается строго в `paper`-режиме.

## Session Readiness

Перед торговым окном теперь есть отдельный readiness-контур.

Команда:

```bash
python3 -m moex_scalper doctor --mode paper --write-report
```

Что делает:

- проверяет доступность API и разрешение watchlist-инструментов
- пишет текущее состояние торгового окна и время следующего окна входов
- считает strategy diagnostics по комиссии, net take-profit и headroom
- сохраняет отчет в `runtime/doctor/latest.json`
- попадает на внешний dashboard в блок `Readiness & Watchdog`

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-preopen.service`
- автоматический таймер: `moex-scalper-preopen.timer`
- по умолчанию таймер срабатывает в `10:05 MSK` по `понедельник-пятница`
