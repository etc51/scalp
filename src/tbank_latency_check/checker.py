from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import json
import os
import socket
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, AsyncIterable, Callable

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import (
    InstrumentIdType,
    MarketDataRequest,
    OrderBookInstrument,
    OrderDirection,
    OrderIdType,
    OrderType,
    OrderStateStreamRequest,
    PingDelaySettings,
    PostOrderAsyncRequest,
    PriceType,
    Quotation,
    SecurityTradingStatus,
    SubscribeOrderBookRequest,
    SubscriptionAction,
    TimeInForceType,
)

DEFAULT_HOST = "invest-public-api.tbank.ru"
DEFAULT_PORT = 443
DEFAULT_TICKER = "SBER"
DEFAULT_CLASS_CODE = "TQBR"
REPORT_DIR = Path("reports")
NANOS_IN_SECOND = Decimal("1000000000")
ORDER_EVENT_TIMEOUT_SECONDS = 5.0
ORDER_CANCEL_RETRY_WINDOW_SECONDS = 1.0
ORDER_CANCEL_RETRY_INTERVAL_SECONDS = 0.05
ORDER_CANCEL_TERMINAL_STATUSES = {
    "EXECUTION_REPORT_STATUS_CANCELLED",
    "EXECUTION_REPORT_STATUS_REJECTED",
    "EXECUTION_REPORT_STATUS_FILL",
}
ORDER_FILL_TERMINAL_STATUSES = {
    "EXECUTION_REPORT_STATUS_FILL",
    "EXECUTION_REPORT_STATUS_REJECTED",
    "EXECUTION_REPORT_STATUS_CANCELLED",
}
NORMAL_TRADING_STATUSES = {
    SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
}


@dataclass(slots=True)
class CheckConfig:
    token: str
    target: str
    port: int
    account_id: str | None
    instrument_id: str | None
    ticker: str | None
    class_code: str
    iterations: int
    stream_iterations: int
    order_iterations: int
    connect_timeout_seconds: float
    stream_timeout_seconds: float
    order_quantity_lots: int
    order_offset_steps: int
    enable_order_tests: bool
    enable_market_roundtrip_test: bool
    write_report: bool
    report_path: Path | None


class OrderStateInbox:
    def __init__(self) -> None:
        self._events: list[tuple[int, Any]] = []
        self._condition = asyncio.Condition()

    async def add(self, event: Any) -> None:
        async with self._condition:
            self._events.append((time.perf_counter_ns(), event))
            self._condition.notify_all()

    async def wait_for(
        self,
        predicate: Callable[[Any], bool],
        *,
        timeout_seconds: float,
        since_ns: int | None = None,
    ) -> tuple[int, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        while True:
            async with self._condition:
                for event_ts_ns, event in self._events:
                    if since_ns is not None and event_ts_ns < since_ns:
                        continue
                    if predicate(event):
                        return event_ts_ns, event

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for order stream event")

                await asyncio.wait_for(self._condition.wait(), timeout=remaining)


def configure_grpc_root_certificates() -> dict[str, Any]:
    configured_from = None
    cert_path = os.getenv("TBANK_SSL_ROOTS_PATH", "").strip()

    if cert_path:
        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = cert_path
        configured_from = "TBANK_SSL_ROOTS_PATH"
    else:
        try:
            bundled = importlib.resources.files("t_tech.invest.certs").joinpath(
                "RussianTrustedRootCA.pem"
            )
            bundled_path = str(bundled)
            if bundled_path:
                os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", bundled_path)
                cert_path = os.environ.get("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", bundled_path)
                configured_from = "sdk_bundle"
        except Exception:  # noqa: BLE001
            cert_path = os.environ.get("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", "")
            if cert_path:
                configured_from = "grpc_env"

    return {
        "grpc_ssl_roots_file_path": cert_path or None,
        "grpc_ssl_roots_source": configured_from,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tbank_latency_check",
        description="Check latency and connectivity to T-Bank Invest API.",
    )
    parser.add_argument("--target", default=os.getenv("TBANK_TARGET", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TBANK_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--account-id",
        default=os.getenv("TBANK_ACCOUNT_ID") or None,
    )
    parser.add_argument(
        "--instrument-id",
        default=os.getenv("TBANK_INSTRUMENT_ID") or None,
    )
    parser.add_argument(
        "--ticker",
        default=os.getenv("TBANK_TICKER", DEFAULT_TICKER) or None,
    )
    parser.add_argument(
        "--class-code",
        default=os.getenv("TBANK_CLASS_CODE", DEFAULT_CLASS_CODE),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=int(os.getenv("TBANK_ITERATIONS", "10")),
    )
    parser.add_argument(
        "--stream-iterations",
        type=int,
        default=int(os.getenv("TBANK_STREAM_ITERATIONS", "3")),
    )
    parser.add_argument(
        "--order-iterations",
        type=int,
        default=int(os.getenv("TBANK_ORDER_ITERATIONS", "3")),
    )
    parser.add_argument(
        "--connect-timeout-seconds",
        type=float,
        default=float(os.getenv("TBANK_CONNECT_TIMEOUT_SECONDS", "5")),
    )
    parser.add_argument(
        "--stream-timeout-seconds",
        type=float,
        default=float(os.getenv("TBANK_STREAM_TIMEOUT_SECONDS", "5")),
    )
    parser.add_argument(
        "--order-quantity-lots",
        type=int,
        default=int(os.getenv("TBANK_ORDER_QUANTITY_LOTS", "1")),
    )
    parser.add_argument(
        "--order-offset-steps",
        type=int,
        default=int(os.getenv("TBANK_ORDER_OFFSET_STEPS", "20")),
    )
    parser.add_argument(
        "--enable-order-tests",
        action="store_true",
        default=False,
        help="Send a real passive limit order and cancel it immediately.",
    )
    parser.add_argument(
        "--enable-market-roundtrip-test",
        action="store_true",
        default=os.getenv("TBANK_ENABLE_MARKET_ROUNDTRIP_TEST", "0").strip().lower()
        in {"1", "true", "yes", "on"},
        help="Send a real market buy and then a real market sell immediately.",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        default=False,
        help="Write the JSON report to ./reports by default.",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Custom JSON report path. Implies --write-report.",
    )
    return parser.parse_args(argv)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def build_config(args: argparse.Namespace) -> CheckConfig:
    token = os.getenv("TBANK_TOKEN")
    if not token:
        raise SystemExit("TBANK_TOKEN is not set. Fill .env or export the variable.")

    report_path = Path(args.report_path).expanduser() if args.report_path else None
    write_report = args.write_report or report_path is not None

    return CheckConfig(
        token=token,
        target=args.target,
        port=args.port,
        account_id=args.account_id,
        instrument_id=args.instrument_id,
        ticker=args.ticker,
        class_code=args.class_code,
        iterations=max(1, args.iterations),
        stream_iterations=max(1, args.stream_iterations),
        order_iterations=max(1, args.order_iterations),
        connect_timeout_seconds=max(0.5, args.connect_timeout_seconds),
        stream_timeout_seconds=max(0.5, args.stream_timeout_seconds),
        order_quantity_lots=max(1, args.order_quantity_lots),
        order_offset_steps=max(1, args.order_offset_steps),
        enable_order_tests=args.enable_order_tests,
        enable_market_roundtrip_test=args.enable_market_roundtrip_test,
        write_report=write_report,
        report_path=report_path,
    )


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ns_to_ms(value_ns: int) -> float:
    return round(value_ns / 1_000_000, 3)


def sanitize_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def build_stats(samples_ms: list[float]) -> dict[str, Any]:
    if not samples_ms:
        return {
            "count": 0,
            "min_ms": None,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }

    ordered = sorted(samples_ms)
    count = len(ordered)

    def percentile(level: float) -> float:
        if count == 1:
            return ordered[0]
        position = (count - 1) * level
        lower = int(position)
        upper = min(lower + 1, count - 1)
        weight = position - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)

    return {
        "count": count,
        "min_ms": round(ordered[0], 3),
        "avg_ms": round(sum(ordered) / count, 3),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(ordered[-1], 3),
    }


def build_series(
    samples_ms: list[float],
    *,
    errors: list[str] | None = None,
    skipped_reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "samples_ms": [round(value, 3) for value in samples_ms],
        "stats": build_stats(samples_ms),
        "errors": errors or [],
        "skipped_reason": skipped_reason,
    }
    if details:
        payload["details"] = details
    return payload


def quotation_to_decimal(quotation: Quotation | None) -> Decimal | None:
    if quotation is None:
        return None
    return Decimal(quotation.units) + (Decimal(quotation.nano) / NANOS_IN_SECOND)


def decimal_to_quotation(value: Decimal) -> Quotation:
    nanos = int((value * NANOS_IN_SECOND).to_integral_value(rounding=ROUND_HALF_UP))
    units, nano = divmod(nanos, 1_000_000_000)
    return Quotation(units=units, nano=nano)


def classify_market_data_event(event: Any) -> str:
    mapping = [
        ("subscribe_order_book_response", event.subscribe_order_book_response),
        ("orderbook", event.orderbook),
        ("trading_status", event.trading_status),
        ("last_price", event.last_price),
        ("ping", event.ping),
    ]
    for name, value in mapping:
        if value is not None:
            return name
    return "unknown"


def classify_order_state_event(event: Any) -> str:
    mapping = [
        ("subscription", event.subscription),
        ("order_state", event.order_state),
        ("ping", event.ping),
    ]
    for name, value in mapping:
        if value is not None:
            return name
    return "unknown"


def dns_probe(host: str, port: int, iterations: int) -> dict[str, Any]:
    samples_ms: list[float] = []
    errors: list[str] = []
    resolved_addresses: set[str] = set()

    for _ in range(iterations):
        started_ns = time.perf_counter_ns()
        try:
            info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            elapsed_ms = ns_to_ms(time.perf_counter_ns() - started_ns)
            samples_ms.append(elapsed_ms)
            resolved_addresses.update(sockaddr[0] for *_, sockaddr in info)
        except OSError as exc:
            errors.append(sanitize_error(exc))

    return build_series(
        samples_ms,
        errors=errors,
        details={"resolved_addresses": sorted(resolved_addresses)},
    )


def tcp_tls_probe(
    host: str,
    port: int,
    iterations: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    tcp_samples_ms: list[float] = []
    tls_samples_ms: list[float] = []
    errors: list[str] = []
    peer_details: dict[str, Any] = {}

    for _ in range(iterations):
        raw_socket: socket.socket | None = None
        wrapped_socket: ssl.SSLSocket | None = None

        try:
            addr_info = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            family, socktype, proto, _, sockaddr = addr_info[0]

            raw_socket = socket.socket(family, socktype, proto)
            raw_socket.settimeout(timeout_seconds)

            tcp_started_ns = time.perf_counter_ns()
            raw_socket.connect(sockaddr)
            tcp_samples_ms.append(ns_to_ms(time.perf_counter_ns() - tcp_started_ns))

            context = ssl.create_default_context()
            wrapped_socket = context.wrap_socket(
                raw_socket,
                server_hostname=host,
                do_handshake_on_connect=False,
            )

            tls_started_ns = time.perf_counter_ns()
            wrapped_socket.do_handshake()
            tls_samples_ms.append(ns_to_ms(time.perf_counter_ns() - tls_started_ns))

            cipher = wrapped_socket.cipher()
            peer_details = {
                "peer_ip": sockaddr[0],
                "peer_port": sockaddr[1],
                "cipher": cipher[0] if cipher else None,
                "tls_version": wrapped_socket.version(),
            }
        except OSError as exc:
            errors.append(sanitize_error(exc))
        finally:
            if wrapped_socket is not None:
                wrapped_socket.close()
            elif raw_socket is not None:
                raw_socket.close()

    tcp_series = build_series(tcp_samples_ms, errors=errors, details=peer_details)
    tls_series = build_series(tls_samples_ms, errors=errors, details=peer_details)
    return tcp_series, tls_series


async def event_loop_lag_probe(iterations: int) -> dict[str, Any]:
    samples_ms: list[float] = []
    interval_seconds = 0.05
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval_seconds

    for _ in range(iterations):
        await asyncio.sleep(interval_seconds)
        actual = loop.time()
        lag_ms = max(0.0, (actual - expected) * 1000)
        samples_ms.append(round(lag_ms, 3))
        expected = actual + interval_seconds

    return build_series(samples_ms)


async def measure_async_calls(
    iterations: int,
    coro_factory: Callable[[], Any],
) -> tuple[dict[str, Any], Any | None]:
    samples_ms: list[float] = []
    errors: list[str] = []
    last_response: Any | None = None

    for _ in range(iterations):
        started_ns = time.perf_counter_ns()
        try:
            last_response = await coro_factory()
            samples_ms.append(ns_to_ms(time.perf_counter_ns() - started_ns))
        except Exception as exc:  # noqa: BLE001
            errors.append(sanitize_error(exc))

    return build_series(samples_ms, errors=errors), last_response


async def resolve_account(services: Any, preferred_account_id: str | None) -> dict[str, Any]:
    response = await services.users.get_accounts()
    accounts = [
        {
            "id": account.id,
            "name": account.name,
            "type": account.type.name,
            "status": account.status.name,
        }
        for account in response.accounts
    ]

    selected_account = None
    if preferred_account_id:
        selected_account = next(
            (account for account in response.accounts if account.id == preferred_account_id),
            None,
        )
        if selected_account is None:
            raise ValueError(f"Account {preferred_account_id} was not returned by GetAccounts")
    elif response.accounts:
        selected_account = response.accounts[0]

    return {
        "selected_account_id": selected_account.id if selected_account else None,
        "accounts": accounts,
    }


async def resolve_instrument(services: Any, config: CheckConfig) -> dict[str, Any] | None:
    instrument = None
    errors: list[str] = []

    if config.instrument_id:
        for id_type in (
            InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
            InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
            InstrumentIdType.INSTRUMENT_ID_TYPE_ID,
        ):
            try:
                response = await services.instruments.share_by(id_type=id_type, id=config.instrument_id)
                instrument = response.instrument
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{id_type.name}: {sanitize_error(exc)}")

    elif config.ticker:
        try:
            response = await services.instruments.share_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
                class_code=config.class_code,
                id=config.ticker,
            )
            instrument = response.instrument
        except Exception as exc:  # noqa: BLE001
            errors.append(sanitize_error(exc))

    if instrument is None:
        if errors:
            return {"errors": errors}
        return None

    min_price_increment = quotation_to_decimal(instrument.min_price_increment)
    return {
        "ticker": instrument.ticker,
        "class_code": instrument.class_code,
        "figi": instrument.figi,
        "instrument_id": instrument.uid,
        "lot": instrument.lot,
        "currency": instrument.currency,
        "exchange": instrument.exchange,
        "api_trade_available_flag": instrument.api_trade_available_flag,
        "buy_available_flag": instrument.buy_available_flag,
        "sell_available_flag": instrument.sell_available_flag,
        "trading_status": instrument.trading_status.name,
        "min_price_increment": str(min_price_increment) if min_price_increment is not None else None,
        "errors": errors,
    }


async def market_data_stream_probe(
    services: Any,
    *,
    instrument_id: str,
    depth: int,
    iterations: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    first_response_samples_ms: list[float] = []
    first_orderbook_samples_ms: list[float] = []
    errors: list[str] = []
    response_types: list[str] = []

    for _ in range(iterations):
        keep_open = asyncio.Event()

        async def request_iterator() -> AsyncIterable[MarketDataRequest]:
            yield MarketDataRequest(
                subscribe_order_book_request=SubscribeOrderBookRequest(
                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                    instruments=[
                        OrderBookInstrument(
                            instrument_id=instrument_id,
                            depth=depth,
                        )
                    ],
                )
            )
            yield MarketDataRequest(
                ping_settings=PingDelaySettings(ping_delay_ms=1000),
            )
            await keep_open.wait()

        stream = services.market_data_stream.market_data_stream(request_iterator())
        started_ns = time.perf_counter_ns()
        first_response_recorded = False

        try:
            async with asyncio.timeout(timeout_seconds):
                async for event in stream:
                    event_type = classify_market_data_event(event)
                    elapsed_ms = ns_to_ms(time.perf_counter_ns() - started_ns)
                    response_types.append(event_type)

                    if not first_response_recorded:
                        first_response_samples_ms.append(elapsed_ms)
                        first_response_recorded = True

                    if event_type == "orderbook":
                        first_orderbook_samples_ms.append(elapsed_ms)
                        break
        except TimeoutError:
            errors.append("TimeoutError: market data stream did not produce expected events in time")
        except Exception as exc:  # noqa: BLE001
            errors.append(sanitize_error(exc))
        finally:
            keep_open.set()
            if hasattr(stream, "aclose"):
                await stream.aclose()

    return {
        "first_response_ms": build_series(
            first_response_samples_ms,
            errors=errors,
            details={"response_types_seen": response_types},
        ),
        "first_orderbook_ms": build_series(
            first_orderbook_samples_ms,
            errors=errors,
            details={"response_types_seen": response_types},
        ),
    }


async def start_order_state_monitor(
    services: Any,
    *,
    account_id: str,
) -> tuple[OrderStateInbox, asyncio.Task[None], int]:
    inbox = OrderStateInbox()
    started_ns = time.perf_counter_ns()

    async def consume() -> None:
        async for event in services.orders_stream.order_state_stream(
            request=OrderStateStreamRequest(
                accounts=[account_id],
                ping_delay_millis=1000,
            )
        ):
            await inbox.add(event)

    task = asyncio.create_task(consume(), name="tbank-order-state-stream")
    return inbox, task, started_ns


def is_normal_trading(status_name: str | None) -> bool:
    if not status_name:
        return False
    try:
        status = SecurityTradingStatus[status_name]
    except KeyError:
        return False
    return status in NORMAL_TRADING_STATUSES


def build_passive_price(
    *,
    best_bid: Decimal | None,
    min_increment: Decimal,
    offset_steps: int,
) -> Decimal:
    base = best_bid if best_bid is not None else min_increment * Decimal(offset_steps + 1)
    price = base - (min_increment * Decimal(offset_steps))
    if price <= 0:
        price = min_increment
    return price.quantize(Decimal("0.000000001"))


async def wait_for_order_event(
    inbox: OrderStateInbox,
    *,
    order_request_id: str,
    timeout_seconds: float,
    since_ns: int | None = None,
    allowed_statuses: set[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    def predicate(event: Any) -> bool:
        order_state = event.order_state
        if order_state is None:
            return False

        identifiers = {
            order_state.order_request_id,
            order_state.order_id,
        }
        if order_request_id not in identifiers:
            return False

        if not allowed_statuses:
            return True

        return order_state.execution_report_status.name in allowed_statuses

    event_ts_ns, event = await inbox.wait_for(
        predicate,
        timeout_seconds=timeout_seconds,
        since_ns=since_ns,
    )

    order_state = event.order_state
    payload = {
        "order_id": order_state.order_id,
        "order_request_id": order_state.order_request_id,
        "execution_report_status": order_state.execution_report_status.name,
        "ticker": order_state.ticker,
        "class_code": order_state.class_code,
    }
    return ns_to_ms(event_ts_ns - (since_ns or event_ts_ns)), payload


async def poll_order_state(
    services: Any,
    *,
    account_id: str,
    order_id: str,
    order_id_type: OrderIdType,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.05,
    allowed_statuses: set[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    started_ns = time.perf_counter_ns()
    last_error: Exception | None = None

    while loop.time() < deadline:
        try:
            state = await services.orders.get_order_state(
                account_id=account_id,
                order_id=order_id,
                order_id_type=order_id_type,
            )
            payload = {
                "order_id": state.order_id,
                "order_request_id": getattr(state, "order_request_id", None),
                "execution_report_status": state.execution_report_status.name,
                "ticker": state.ticker,
                "class_code": state.class_code,
            }
            if allowed_statuses and payload["execution_report_status"] not in allowed_statuses:
                await asyncio.sleep(poll_interval_seconds)
                continue
            return ns_to_ms(time.perf_counter_ns() - started_ns), payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(poll_interval_seconds)

    if last_error is not None:
        raise TimeoutError(
            f"Timed out waiting for order state visibility: {sanitize_error(last_error)}"
        ) from last_error
    raise TimeoutError("Timed out waiting for order state visibility")


async def wait_for_order_registration(
    services: Any,
    inbox: OrderStateInbox,
    *,
    account_id: str,
    request_identifier: str,
    timeout_seconds: float,
    since_ns: int,
) -> tuple[float, dict[str, Any]]:
    try:
        return await wait_for_order_event(
            inbox,
            order_request_id=request_identifier,
            timeout_seconds=timeout_seconds,
            since_ns=since_ns,
        )
    except TimeoutError:
        return await poll_order_state(
            services,
            account_id=account_id,
            order_id=request_identifier,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
            timeout_seconds=timeout_seconds,
        )


async def wait_for_terminal_order_state(
    services: Any,
    inbox: OrderStateInbox,
    *,
    account_id: str,
    request_identifier: str,
    order_identifier: str | None,
    timeout_seconds: float,
    since_ns: int,
) -> tuple[float, dict[str, Any]]:
    try:
        return await wait_for_order_event(
            inbox,
            order_request_id=request_identifier,
            timeout_seconds=timeout_seconds,
            since_ns=since_ns,
            allowed_statuses=ORDER_CANCEL_TERMINAL_STATUSES,
        )
    except TimeoutError:
        if order_identifier:
            try:
                return await poll_order_state(
                    services,
                    account_id=account_id,
                    order_id=order_identifier,
                    order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
                    timeout_seconds=timeout_seconds,
                    allowed_statuses=ORDER_CANCEL_TERMINAL_STATUSES,
                )
            except TimeoutError:
                pass
        return await poll_order_state(
            services,
            account_id=account_id,
            order_id=request_identifier,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
            timeout_seconds=timeout_seconds,
            allowed_statuses=ORDER_CANCEL_TERMINAL_STATUSES,
        )


async def wait_for_filled_order_state(
    services: Any,
    inbox: OrderStateInbox,
    *,
    account_id: str,
    request_identifier: str,
    order_identifier: str | None,
    timeout_seconds: float,
    since_ns: int,
) -> tuple[float, dict[str, Any]]:
    try:
        return await wait_for_order_event(
            inbox,
            order_request_id=request_identifier,
            timeout_seconds=timeout_seconds,
            since_ns=since_ns,
            allowed_statuses=ORDER_FILL_TERMINAL_STATUSES,
        )
    except TimeoutError:
        if order_identifier:
            try:
                return await poll_order_state(
                    services,
                    account_id=account_id,
                    order_id=order_identifier,
                    order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
                    timeout_seconds=timeout_seconds,
                    allowed_statuses=ORDER_FILL_TERMINAL_STATUSES,
                )
            except TimeoutError:
                pass
        return await poll_order_state(
            services,
            account_id=account_id,
            order_id=request_identifier,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
            timeout_seconds=timeout_seconds,
            allowed_statuses=ORDER_FILL_TERMINAL_STATUSES,
        )


async def cancel_order_with_retries(
    services: Any,
    *,
    account_id: str,
    request_identifier: str,
    exchange_order_id: str | None,
    timeout_seconds: float = ORDER_CANCEL_RETRY_WINDOW_SECONDS,
    retry_interval_seconds: float = ORDER_CANCEL_RETRY_INTERVAL_SECONDS,
) -> tuple[float, dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    started_ns = time.perf_counter_ns()
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None

    candidates: list[tuple[str, OrderIdType]] = [
        (request_identifier, OrderIdType.ORDER_ID_TYPE_REQUEST),
    ]
    if exchange_order_id:
        candidates.append((exchange_order_id, OrderIdType.ORDER_ID_TYPE_EXCHANGE))

    while loop.time() < deadline:
        for order_id, order_id_type in candidates:
            try:
                await services.orders.cancel_order(
                    account_id=account_id,
                    order_id=order_id,
                    order_id_type=order_id_type,
                )
                elapsed_ms = ns_to_ms(time.perf_counter_ns() - started_ns)
                return elapsed_ms, {
                    "cancelled_with_order_id": order_id,
                    "cancelled_with_order_id_type": order_id_type.name,
                    "attempts": attempts,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                attempts.append(
                    {
                        "order_id": order_id,
                        "order_id_type": order_id_type.name,
                        "error": sanitize_error(exc),
                    }
                )
        await asyncio.sleep(retry_interval_seconds)

    if last_error is not None:
        raise TimeoutError(
            f"Timed out cancelling order after retries: {sanitize_error(last_error)}"
        ) from last_error
    raise TimeoutError("Timed out cancelling order after retries")


async def order_cycle_probe(
    services: Any,
    *,
    account_id: str,
    instrument: dict[str, Any],
    inbox: OrderStateInbox,
    order_iterations: int,
    order_quantity_lots: int,
    order_offset_steps: int,
) -> dict[str, Any]:
    post_order_async_samples_ms: list[float] = []
    cancel_order_samples_ms: list[float] = []
    post_to_order_state_samples_ms: list[float] = []
    cancel_to_order_state_samples_ms: list[float] = []
    errors: list[str] = []
    last_events: list[dict[str, Any]] = []
    cancel_attempts: list[dict[str, Any]] = []

    min_increment_str = instrument.get("min_price_increment")
    instrument_id = instrument.get("instrument_id")

    if not instrument_id or not min_increment_str:
        return {
            "post_order_async_ms": build_series([], skipped_reason="Instrument metadata is incomplete"),
            "cancel_order_ms": build_series([], skipped_reason="Instrument metadata is incomplete"),
            "post_to_order_state_ms": build_series([], skipped_reason="Instrument metadata is incomplete"),
            "cancel_to_order_state_ms": build_series([], skipped_reason="Instrument metadata is incomplete"),
        }

    min_increment = Decimal(min_increment_str)

    for _ in range(order_iterations):
        try:
            order_book = await services.market_data.get_order_book(instrument_id=instrument_id, depth=1)
            best_bid = quotation_to_decimal(order_book.bids[0].price) if order_book.bids else None
            passive_price = build_passive_price(
                best_bid=best_bid,
                min_increment=min_increment,
                offset_steps=order_offset_steps,
            )
            client_order_id = str(uuid.uuid4())

            post_request = PostOrderAsyncRequest(
                instrument_id=instrument_id,
                quantity=order_quantity_lots,
                price=decimal_to_quotation(passive_price),
                direction=OrderDirection.ORDER_DIRECTION_BUY,
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id=client_order_id,
                time_in_force=TimeInForceType.TIME_IN_FORCE_DAY,
                price_type=PriceType.PRICE_TYPE_CURRENCY,
                confirm_margin_trade=False,
            )

            post_started_ns = time.perf_counter_ns()
            post_response = await services.orders.post_order_async(post_request)
            post_order_async_samples_ms.append(ns_to_ms(time.perf_counter_ns() - post_started_ns))
            request_identifier = post_response.order_request_id or client_order_id

            post_to_state_ms, first_event = await wait_for_order_registration(
                services,
                inbox,
                account_id=account_id,
                request_identifier=request_identifier,
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=post_started_ns,
            )
            post_to_order_state_samples_ms.append(post_to_state_ms)
            last_events.append({"phase": "post", **first_event})

            exchange_order_id = first_event.get("order_id")
            if exchange_order_id == request_identifier:
                exchange_order_id = None
            cancel_target_id = exchange_order_id or request_identifier
            cancel_target_type = (
                OrderIdType.ORDER_ID_TYPE_EXCHANGE
                if exchange_order_id
                else OrderIdType.ORDER_ID_TYPE_REQUEST
            )

            cancel_started_ns = time.perf_counter_ns()
            cancel_ms, cancel_details = await cancel_order_with_retries(
                services,
                account_id=account_id,
                request_identifier=request_identifier,
                exchange_order_id=exchange_order_id,
            )
            cancel_order_samples_ms.append(cancel_ms)
            cancel_attempts.append(
                {
                    "request_identifier": request_identifier,
                    "exchange_order_id": exchange_order_id,
                    "preferred_cancel_target_id": cancel_target_id,
                    "preferred_cancel_target_type": cancel_target_type.name,
                    **cancel_details,
                }
            )

            cancel_to_state_ms, cancel_event = await wait_for_terminal_order_state(
                services,
                inbox,
                account_id=account_id,
                request_identifier=request_identifier,
                order_identifier=exchange_order_id,
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=cancel_started_ns,
            )
            cancel_to_order_state_samples_ms.append(cancel_to_state_ms)
            last_events.append({"phase": "cancel", **cancel_event})

            if post_response.execution_report_status is not None:
                last_events.append(
                    {
                        "phase": "post_ack",
                        "order_request_id": request_identifier,
                        "execution_report_status": post_response.execution_report_status.name,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(sanitize_error(exc))

    return {
        "post_order_async_ms": build_series(post_order_async_samples_ms, errors=errors),
        "cancel_order_ms": build_series(cancel_order_samples_ms, errors=errors),
        "post_to_order_state_ms": build_series(
            post_to_order_state_samples_ms,
            errors=errors,
            details={"last_events": last_events[-6:]},
        ),
        "cancel_to_order_state_ms": build_series(
            cancel_to_order_state_samples_ms,
            errors=errors,
            details={
                "last_events": last_events[-6:],
                "cancel_attempts": cancel_attempts[-6:],
            },
        ),
    }


def build_skipped_market_roundtrip(skipped_reason: str) -> dict[str, Any]:
    return {
        "buy_post_order_async_ms": build_series([], skipped_reason=skipped_reason),
        "buy_to_fill_ms": build_series([], skipped_reason=skipped_reason),
        "sell_post_order_async_ms": build_series([], skipped_reason=skipped_reason),
        "sell_to_fill_ms": build_series([], skipped_reason=skipped_reason),
        "roundtrip_cycle_ms": build_series([], skipped_reason=skipped_reason),
    }


async def market_roundtrip_probe(
    services: Any,
    *,
    account_id: str,
    instrument: dict[str, Any],
    inbox: OrderStateInbox,
    order_iterations: int,
    order_quantity_lots: int,
) -> dict[str, Any]:
    buy_post_order_async_samples_ms: list[float] = []
    buy_to_fill_samples_ms: list[float] = []
    sell_post_order_async_samples_ms: list[float] = []
    sell_to_fill_samples_ms: list[float] = []
    roundtrip_cycle_samples_ms: list[float] = []
    errors: list[str] = []
    last_events: list[dict[str, Any]] = []

    instrument_id = instrument.get("instrument_id")
    if not instrument_id:
        return build_skipped_market_roundtrip("Instrument metadata is incomplete")

    for _ in range(order_iterations):
        try:
            roundtrip_started_ns = time.perf_counter_ns()

            buy_client_order_id = str(uuid.uuid4())
            buy_request = PostOrderAsyncRequest(
                instrument_id=instrument_id,
                quantity=order_quantity_lots,
                direction=OrderDirection.ORDER_DIRECTION_BUY,
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=buy_client_order_id,
                confirm_margin_trade=False,
            )

            buy_started_ns = time.perf_counter_ns()
            buy_response = await services.orders.post_order_async(buy_request)
            buy_post_order_async_samples_ms.append(ns_to_ms(time.perf_counter_ns() - buy_started_ns))
            buy_request_identifier = buy_response.order_request_id or buy_client_order_id

            buy_registration_ms, buy_registration_event = await wait_for_order_registration(
                services,
                inbox,
                account_id=account_id,
                request_identifier=buy_request_identifier,
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=buy_started_ns,
            )
            last_events.append({"phase": "buy_post", **buy_registration_event})

            buy_fill_ms, buy_fill_event = await wait_for_filled_order_state(
                services,
                inbox,
                account_id=account_id,
                request_identifier=buy_request_identifier,
                order_identifier=(
                    buy_registration_event.get("order_id")
                    if buy_registration_event.get("order_id") != buy_request_identifier
                    else None
                ),
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=buy_started_ns,
            )
            buy_to_fill_samples_ms.append(buy_fill_ms)
            last_events.append({"phase": "buy_fill", **buy_fill_event})

            if buy_fill_event["execution_report_status"] != "EXECUTION_REPORT_STATUS_FILL":
                errors.append(
                    f"Buy market order did not fill: {buy_fill_event['execution_report_status']}"
                )
                continue

            sell_client_order_id = str(uuid.uuid4())
            sell_request = PostOrderAsyncRequest(
                instrument_id=instrument_id,
                quantity=order_quantity_lots,
                direction=OrderDirection.ORDER_DIRECTION_SELL,
                account_id=account_id,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=sell_client_order_id,
                confirm_margin_trade=False,
            )

            sell_started_ns = time.perf_counter_ns()
            sell_response = await services.orders.post_order_async(sell_request)
            sell_post_order_async_samples_ms.append(ns_to_ms(time.perf_counter_ns() - sell_started_ns))
            sell_request_identifier = sell_response.order_request_id or sell_client_order_id

            sell_registration_ms, sell_registration_event = await wait_for_order_registration(
                services,
                inbox,
                account_id=account_id,
                request_identifier=sell_request_identifier,
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=sell_started_ns,
            )
            last_events.append({"phase": "sell_post", **sell_registration_event})

            sell_fill_ms, sell_fill_event = await wait_for_filled_order_state(
                services,
                inbox,
                account_id=account_id,
                request_identifier=sell_request_identifier,
                order_identifier=(
                    sell_registration_event.get("order_id")
                    if sell_registration_event.get("order_id") != sell_request_identifier
                    else None
                ),
                timeout_seconds=ORDER_EVENT_TIMEOUT_SECONDS,
                since_ns=sell_started_ns,
            )
            sell_to_fill_samples_ms.append(sell_fill_ms)
            last_events.append({"phase": "sell_fill", **sell_fill_event})

            if sell_fill_event["execution_report_status"] != "EXECUTION_REPORT_STATUS_FILL":
                errors.append(
                    f"Sell market order did not fill: {sell_fill_event['execution_report_status']}"
                )
                continue

            roundtrip_cycle_samples_ms.append(
                ns_to_ms(time.perf_counter_ns() - roundtrip_started_ns)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(sanitize_error(exc))

    return {
        "buy_post_order_async_ms": build_series(
            buy_post_order_async_samples_ms,
            errors=errors,
        ),
        "buy_to_fill_ms": build_series(
            buy_to_fill_samples_ms,
            errors=errors,
            details={"last_events": last_events[-8:]},
        ),
        "sell_post_order_async_ms": build_series(
            sell_post_order_async_samples_ms,
            errors=errors,
        ),
        "sell_to_fill_ms": build_series(
            sell_to_fill_samples_ms,
            errors=errors,
            details={"last_events": last_events[-8:]},
        ),
        "roundtrip_cycle_ms": build_series(
            roundtrip_cycle_samples_ms,
            errors=errors,
            details={"last_events": last_events[-8:]},
        ),
    }


async def run_checks(config: CheckConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "started_at_utc": now_utc(),
        "target": f"{config.target}:{config.port}",
        "safe_defaults": {
            "order_tests_enabled": config.enable_order_tests,
            "outside_session_supported": True,
        },
        "checks": {},
        "context": {},
        "assessment": {},
    }
    report["context"]["tls"] = configure_grpc_root_certificates()

    report["checks"]["dns_resolve_ms"] = dns_probe(config.target, config.port, config.iterations)

    tcp_series, tls_series = tcp_tls_probe(
        config.target,
        config.port,
        config.iterations,
        config.connect_timeout_seconds,
    )
    report["checks"]["tcp_connect_ms"] = tcp_series
    report["checks"]["tls_handshake_ms"] = tls_series
    report["checks"]["event_loop_lag_ms"] = await event_loop_lag_probe(config.iterations)

    grpc_target = f"{config.target}:{config.port}"

    async with AsyncClient(config.token, target=grpc_target) as services:
        grpc_get_accounts, accounts_response = await measure_async_calls(
            config.iterations,
            services.users.get_accounts,
        )
        report["checks"]["grpc_get_accounts_ms"] = grpc_get_accounts

        if accounts_response is None:
            report["checks"]["grpc_get_trading_status_ms"] = build_series(
                [],
                skipped_reason="Authenticated API is unavailable",
            )
            report["checks"]["grpc_get_last_prices_ms"] = build_series(
                [],
                skipped_reason="Authenticated API is unavailable",
            )
            report["checks"]["grpc_get_order_book_ms"] = build_series(
                [],
                skipped_reason="Authenticated API is unavailable",
            )
            report["checks"]["market_data_stream"] = {
                "first_response_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
                "first_orderbook_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
            }
            report["checks"]["order_state_stream_first_response_ms"] = build_series(
                [],
                skipped_reason="Authenticated API is unavailable",
            )
            report["checks"]["order_cycle"] = {
                "post_order_async_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
                "cancel_order_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
                "post_to_order_state_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
                "cancel_to_order_state_ms": build_series(
                    [],
                    skipped_reason="Authenticated API is unavailable",
                ),
            }
            report["checks"]["market_roundtrip"] = build_skipped_market_roundtrip(
                "Authenticated API is unavailable"
            )
            report["assessment"]["can_reach_authenticated_api"] = False
            report["assessment"]["next_step"] = "Fix token or API access before judging scalping viability."
            report["finished_at_utc"] = now_utc()
            return report

        account_context = await resolve_account(services, config.account_id)
        report["context"]["accounts"] = account_context
        report["assessment"]["can_reach_authenticated_api"] = True

        instrument_context = await resolve_instrument(services, config)
        report["context"]["instrument"] = instrument_context

        selected_account_id = account_context.get("selected_account_id")
        instrument_id = instrument_context.get("instrument_id") if instrument_context else None

        if instrument_id:
            get_trading_status_series, trading_status_response = await measure_async_calls(
                config.iterations,
                lambda: services.market_data.get_trading_status(instrument_id=instrument_id),
            )
            report["checks"]["grpc_get_trading_status_ms"] = get_trading_status_series

            get_last_prices_series, last_prices_response = await measure_async_calls(
                config.iterations,
                lambda: services.market_data.get_last_prices(instrument_id=[instrument_id]),
            )
            report["checks"]["grpc_get_last_prices_ms"] = get_last_prices_series

            get_order_book_series, order_book_response = await measure_async_calls(
                config.iterations,
                lambda: services.market_data.get_order_book(instrument_id=instrument_id, depth=1),
            )
            report["checks"]["grpc_get_order_book_ms"] = get_order_book_series

            if trading_status_response is not None:
                report["context"]["instrument"]["live_trading_status"] = trading_status_response.trading_status.name
                report["context"]["instrument"]["limit_order_available_flag"] = (
                    trading_status_response.limit_order_available_flag
                )
                report["context"]["instrument"]["market_order_available_flag"] = (
                    trading_status_response.market_order_available_flag
                )
                report["context"]["instrument"]["api_trade_available_flag"] = (
                    trading_status_response.api_trade_available_flag
                )

            if last_prices_response is not None and last_prices_response.last_prices:
                first_price = last_prices_response.last_prices[0]
                report["context"]["instrument"]["last_price"] = str(
                    quotation_to_decimal(first_price.price)
                )

            if order_book_response is not None:
                best_bid = quotation_to_decimal(order_book_response.bids[0].price) if order_book_response.bids else None
                best_ask = quotation_to_decimal(order_book_response.asks[0].price) if order_book_response.asks else None
                report["context"]["instrument"]["best_bid"] = str(best_bid) if best_bid is not None else None
                report["context"]["instrument"]["best_ask"] = str(best_ask) if best_ask is not None else None

            report["checks"]["market_data_stream"] = await market_data_stream_probe(
                services,
                instrument_id=instrument_id,
                depth=1,
                iterations=config.stream_iterations,
                timeout_seconds=config.stream_timeout_seconds,
            )
        else:
            report["checks"]["grpc_get_trading_status_ms"] = build_series(
                [],
                skipped_reason="Instrument could not be resolved",
            )
            report["checks"]["grpc_get_last_prices_ms"] = build_series(
                [],
                skipped_reason="Instrument could not be resolved",
            )
            report["checks"]["grpc_get_order_book_ms"] = build_series(
                [],
                skipped_reason="Instrument could not be resolved",
            )
            report["checks"]["market_data_stream"] = {
                "first_response_ms": build_series([], skipped_reason="Instrument could not be resolved"),
                "first_orderbook_ms": build_series([], skipped_reason="Instrument could not be resolved"),
            }

        if selected_account_id:
            inbox, monitor_task, monitor_started_ns = await start_order_state_monitor(
                services,
                account_id=selected_account_id,
            )
            try:
                try:
                    first_event_ts_ns, first_event = await inbox.wait_for(
                        lambda _: True,
                        timeout_seconds=config.stream_timeout_seconds,
                    )
                    report["checks"]["order_state_stream_first_response_ms"] = build_series(
                        [ns_to_ms(first_event_ts_ns - monitor_started_ns)],
                        details={"first_event_type": classify_order_state_event(first_event)},
                    )
                except TimeoutError as exc:
                    report["checks"]["order_state_stream_first_response_ms"] = build_series(
                        [],
                        errors=[sanitize_error(exc)],
                    )

                live_status = (
                    report["context"].get("instrument", {}).get("live_trading_status")
                    if report["context"].get("instrument")
                    else None
                )

                if config.enable_order_tests and instrument_id and is_normal_trading(live_status):
                    if config.enable_market_roundtrip_test:
                        report["checks"]["order_cycle"] = {
                            "post_order_async_ms": build_series(
                                [],
                                skipped_reason="Limit-cancel cycle skipped because market roundtrip mode is enabled",
                            ),
                            "cancel_order_ms": build_series(
                                [],
                                skipped_reason="Limit-cancel cycle skipped because market roundtrip mode is enabled",
                            ),
                            "post_to_order_state_ms": build_series(
                                [],
                                skipped_reason="Limit-cancel cycle skipped because market roundtrip mode is enabled",
                            ),
                            "cancel_to_order_state_ms": build_series(
                                [],
                                skipped_reason="Limit-cancel cycle skipped because market roundtrip mode is enabled",
                            ),
                        }
                        report["checks"]["market_roundtrip"] = await market_roundtrip_probe(
                            services,
                            account_id=selected_account_id,
                            instrument=report["context"]["instrument"],
                            inbox=inbox,
                            order_iterations=config.order_iterations,
                            order_quantity_lots=config.order_quantity_lots,
                        )
                    else:
                        report["checks"]["order_cycle"] = await order_cycle_probe(
                            services,
                            account_id=selected_account_id,
                            instrument=report["context"]["instrument"],
                            inbox=inbox,
                            order_iterations=config.order_iterations,
                            order_quantity_lots=config.order_quantity_lots,
                            order_offset_steps=config.order_offset_steps,
                        )
                        report["checks"]["market_roundtrip"] = build_skipped_market_roundtrip(
                            "Enable with --enable-market-roundtrip-test to run market buy/sell roundtrip",
                        )
                elif config.enable_order_tests and not is_normal_trading(live_status):
                    report["checks"]["order_cycle"] = {
                        "post_order_async_ms": build_series(
                            [],
                            skipped_reason=(
                                f"Order tests skipped because trading status is {live_status or 'unknown'}, "
                                "not NORMAL_TRADING"
                            ),
                        ),
                        "cancel_order_ms": build_series(
                            [],
                            skipped_reason="Order tests skipped outside NORMAL_TRADING",
                        ),
                        "post_to_order_state_ms": build_series(
                            [],
                            skipped_reason="Order tests skipped outside NORMAL_TRADING",
                        ),
                        "cancel_to_order_state_ms": build_series(
                            [],
                            skipped_reason="Order tests skipped outside NORMAL_TRADING",
                        ),
                    }
                    report["checks"]["market_roundtrip"] = build_skipped_market_roundtrip(
                        "Order tests skipped outside NORMAL_TRADING"
                    )
                else:
                    report["checks"]["order_cycle"] = {
                        "post_order_async_ms": build_series(
                            [],
                            skipped_reason="Enable with --enable-order-tests during live session",
                        ),
                        "cancel_order_ms": build_series(
                            [],
                            skipped_reason="Enable with --enable-order-tests during live session",
                        ),
                        "post_to_order_state_ms": build_series(
                            [],
                            skipped_reason="Enable with --enable-order-tests during live session",
                        ),
                        "cancel_to_order_state_ms": build_series(
                            [],
                            skipped_reason="Enable with --enable-order-tests during live session",
                        ),
                    }
                    report["checks"]["market_roundtrip"] = build_skipped_market_roundtrip(
                        "Enable with --enable-order-tests and --enable-market-roundtrip-test during live session"
                    )
            finally:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
        else:
            report["checks"]["order_state_stream_first_response_ms"] = build_series(
                [],
                skipped_reason="No account available",
            )
            report["checks"]["order_cycle"] = {
                "post_order_async_ms": build_series([], skipped_reason="No account available"),
                "cancel_order_ms": build_series([], skipped_reason="No account available"),
                "post_to_order_state_ms": build_series([], skipped_reason="No account available"),
                "cancel_to_order_state_ms": build_series([], skipped_reason="No account available"),
            }
            report["checks"]["market_roundtrip"] = build_skipped_market_roundtrip(
                "No account available"
            )

    live_status = report["context"].get("instrument", {}).get("live_trading_status")
    normal_trading = is_normal_trading(live_status)
    report["assessment"]["can_test_without_session"] = True
    report["assessment"]["live_session_required_for_final_scalping_verdict"] = True
    report["assessment"]["current_trading_status"] = live_status
    report["assessment"]["current_run_is_market_representative"] = normal_trading
    report["assessment"]["next_step"] = (
        "Repeat the same probe during NORMAL_TRADING and enable order tests to judge scalping viability."
        if not normal_trading
        else "Review p95 for order placement/cancel path and market data stream before building the bot."
    )
    report["finished_at_utc"] = now_utc()
    return report


def render_summary(report: dict[str, Any]) -> str:
    checks = report["checks"]
    lines = [
        f"Target: {report['target']}",
        f"Authenticated API reachable: {report['assessment'].get('can_reach_authenticated_api')}",
        f"Trading status: {report['assessment'].get('current_trading_status')}",
        f"Representative for live scalping: {report['assessment'].get('current_run_is_market_representative')}",
        "",
        "Key stats:",
        f"- DNS p95: {checks['dns_resolve_ms']['stats']['p95_ms']} ms",
        f"- TCP p95: {checks['tcp_connect_ms']['stats']['p95_ms']} ms",
        f"- TLS p95: {checks['tls_handshake_ms']['stats']['p95_ms']} ms",
        f"- GetAccounts p95: {checks['grpc_get_accounts_ms']['stats']['p95_ms']} ms",
        f"- TradingStatus p95: {checks['grpc_get_trading_status_ms']['stats']['p95_ms']} ms",
        f"- LastPrices p95: {checks['grpc_get_last_prices_ms']['stats']['p95_ms']} ms",
        f"- OrderBook p95: {checks['grpc_get_order_book_ms']['stats']['p95_ms']} ms",
        f"- MarketData first response p95: {checks['market_data_stream']['first_response_ms']['stats']['p95_ms']} ms",
        f"- MarketData first orderbook p95: {checks['market_data_stream']['first_orderbook_ms']['stats']['p95_ms']} ms",
        f"- OrderState first response p95: {checks['order_state_stream_first_response_ms']['stats']['p95_ms']} ms",
        "",
        f"Next step: {report['assessment'].get('next_step')}",
    ]

    order_cycle = checks.get("order_cycle", {})
    post_stats = order_cycle.get("post_order_async_ms", {}).get("stats", {})
    if post_stats.get("count"):
        lines.extend(
            [
                "",
                "Order cycle:",
                f"- PostOrderAsync p95: {post_stats.get('p95_ms')} ms",
                f"- CancelOrder p95: {order_cycle['cancel_order_ms']['stats']['p95_ms']} ms",
                f"- Post->OrderState p95: {order_cycle['post_to_order_state_ms']['stats']['p95_ms']} ms",
                f"- Cancel->OrderState p95: {order_cycle['cancel_to_order_state_ms']['stats']['p95_ms']} ms",
            ]
        )

    market_roundtrip = checks.get("market_roundtrip", {})
    buy_post_stats = market_roundtrip.get("buy_post_order_async_ms", {}).get("stats", {})
    if buy_post_stats.get("count"):
        lines.extend(
            [
                "",
                "Market roundtrip:",
                f"- Buy PostOrderAsync p95: {market_roundtrip['buy_post_order_async_ms']['stats']['p95_ms']} ms",
                f"- Buy->Fill p95: {market_roundtrip['buy_to_fill_ms']['stats']['p95_ms']} ms",
                f"- Sell PostOrderAsync p95: {market_roundtrip['sell_post_order_async_ms']['stats']['p95_ms']} ms",
                f"- Sell->Fill p95: {market_roundtrip['sell_to_fill_ms']['stats']['p95_ms']} ms",
                f"- Full roundtrip p95: {market_roundtrip['roundtrip_cycle_ms']['stats']['p95_ms']} ms",
            ]
        )

    return "\n".join(lines)


def write_report(report: dict[str, Any], path: Path | None) -> Path:
    final_path = path
    if final_path is None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final_path = REPORT_DIR / f"tbank-latency-{timestamp}.json"
    else:
        final_path.parent.mkdir(parents=True, exist_ok=True)

    final_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return final_path


async def async_main(argv: list[str]) -> int:
    load_dotenv(Path(".env"))
    args = parse_args(argv)
    config = build_config(args)
    report = await run_checks(config)
    print(render_summary(report))

    if config.write_report:
        report_path = write_report(report, config.report_path)
        print(f"\nReport written to: {report_path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    return asyncio.run(async_main(argv))
