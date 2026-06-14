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
- фильтр по спреду, дисбалансу стакана, короткому импульсу и `time-stop`
- дневной лимит убытка и cooldown

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

- текущий live-режим рассчитан на наш measured latency и использует market orders
- это стартовый боевой каркас, а не финальная production-версия
- short по акциям по умолчанию выключен

## GitHub Auto-Update On Server

Теперь репозиторий можно держать источником истины, а сервер обновлять из GitHub автоматически ночью.

Что для этого есть в проекте:

- `scripts/run_scalper_service.sh` запускает бота как `systemd`-сервис
- `scripts/update_from_github.sh` делает `git pull`, обновляет зависимости и перезапускает сервис
- `scripts/install_server_services.sh` ставит `systemd`-юниты
- `deploy/systemd/moex-scalper-update.timer` запускает ночную проверку обновлений в `03:30` по `Europe/Moscow`

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
sudo systemctl status moex-scalper-update.timer
sudo systemctl start moex-scalper-update.service
```

Безопасность запуска:

- пока в `.env` стоит `SCALPER_MODE=paper`, сервис безопасно крутится в paper-режиме
- для реальной торговли нужно осознанно перевести `.env` в `SCALPER_MODE=live`
- `.env`, `reports/` и `runtime/` в git не коммитятся
