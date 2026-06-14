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

Осторожный live-запуск:

```bash
python3 -m moex_scalper run --mode live
```

Важно:

- разрешена только `paper`-торговля, live-режим остается заблокирован до явного разрешения пользователя
- short по акциям по умолчанию выключен
- `SCALPER_MIN_NET_TAKE_PROFIT_BPS` задает минимальную чистую цель в `bps` после roundtrip-комиссии Premium; это режет слишком тесные сделки даже если импульс формально проходит
- `SCALPER_TARGET_NET_TAKE_PROFIT_BUFFER_BPS` задает желаемый запас сверх этого floor; `doctor`, `summary`, `dashboard` и autotune показывают и используют рекомендуемый минимальный `take-profit`
- `SCALPER_REGIME_FILTER_MODE` влияет только на новые входы и использует только уже закрытую предыдущую 1m-минуту инструмента:
  - `off` — без regime-filter
  - `trend_not_bearish` — не входить, если предыдущая закрытая минута выглядит bearish по RSI/EMA/MACD regime
  - `trend_bullish` — входить только если предыдущая закрытая минута выглядит bullish по RSI/EMA/MACD regime
  - `macd_positive` — входить только если у предыдущей закрытой минуты `MACD histogram > 0`
  - `rsi_50_70` — входить только если у предыдущей закрытой минуты `RSI14` в диапазоне `50-70`
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
- по умолчанию таймер срабатывает в `18:26 MSK` по `понедельник-пятница`

## Safe Paper Autotune

Теперь поверх `analysis` и `optimizer` есть еще один слой: safe autotune только для `paper`-режима.

Команда:

```bash
python3 -m moex_scalper tune --apply --write-report
```

Что делает:

- читает `runtime/analysis/latest.json` и `runtime/optimizer/latest.json`
- читает `runtime/research/latest.json` и может отдельно применить лучший regime-filter из regime-replay
- проверяет, что мы все еще в `paper`-режиме
- не меняет параметры, если идет торговое окно новых входов
- не меняет параметры, если в `paper_session.json` есть открытые позиции
- требует достаточный sample по сделкам
- если candidate из optimizer реально пригоден, обновляет параметры стратегии в `.env`
- если optimizer пока не готов, но у текущего конфига слишком маленький запас после комиссии, может все равно безопасно поднять `take-profit` до рекомендованного минимума
- если research показывает устойчиво лучший regime-filter против baseline, autotune может сам включить его в `.env` через `SCALPER_REGIME_FILTER_MODE`
- пишет решение в `runtime/tuning/latest.json` и историю в `runtime/tuning/history.jsonl`
- после успешного apply перезапускает только `paper`-сервис бота

Для regime-autotune есть отдельные флаги:

- `SCALPER_AUTO_APPLY_REGIME_FILTER=1` — разрешает auto-apply research regime candidate
- `SCALPER_AUTO_TUNE_MIN_REGIME_DELTA_RUB=0` — минимальный `delta_vs_baseline_rub`, чтобы regime-filter считался достойным apply

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-tune.service`
- автоматический таймер: `moex-scalper-tune.timer`
- по умолчанию таймер срабатывает в `18:14 MSK` по `понедельник-пятница`

Это влияет только на новые входы после рестарта. Уже сохраненные открытые `paper`-позиции продолжают жить со своими параметрами, записанными в session-state.

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
- пишет решение в `runtime/restrictions/latest.json`
- сохраняет активные ограничения в `runtime/restrictions/active.json`
- после успешного apply перезапускает только `paper`-сервис бота

На сервере это можно запускать и вручную, и автоматически:

- ручной запуск: `sudo systemctl start moex-scalper-restrict.service`
- автоматический таймер: `moex-scalper-restrict.timer`
- по умолчанию таймер срабатывает в `18:18 MSK` по `понедельник-пятница`

Важно:

- это влияет только на новые входы
- уже открытые `paper`-позиции не закрываются и не пересчитываются из-за смены ограничений

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
