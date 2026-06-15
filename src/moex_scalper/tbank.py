from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import (
    InstrumentIdType,
    MarketDataRequest,
    OrderBookInstrument,
    PingDelaySettings,
    SubscribeOrderBookRequest,
    SubscriptionAction,
)

from .config import ScalperConfig
from .domain import InstrumentSpec, MarketSnapshot
from tbank_latency_check.checker import configure_grpc_root_certificates


NANOS_IN_SECOND = Decimal("1000000000")
LOGGER = logging.getLogger("moex_scalper.tbank")
POLL_FALLBACK_RPC_TIMEOUT_SECONDS = 3.0


def quotation_to_decimal(value: object) -> Decimal:
    units = getattr(value, "units", 0) or 0
    nano = getattr(value, "nano", 0) or 0
    return Decimal(units) + (Decimal(nano) / NANOS_IN_SECOND)


def open_client(config: ScalperConfig) -> AsyncClient:
    configure_grpc_root_certificates()
    return AsyncClient(config.token, target=config.target)


async def resolve_instruments(services: object, config: ScalperConfig) -> list[InstrumentSpec]:
    instruments: list[InstrumentSpec] = []
    for ticker in config.watchlist:
        response = await services.instruments.share_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
            class_code=config.class_code,
            id=ticker,
        )
        share = response.instrument
        instruments.append(
            InstrumentSpec(
                instrument_id=share.uid,
                ticker=share.ticker,
                class_code=share.class_code,
                figi=share.figi,
                lot_size=share.lot,
                min_price_increment=quotation_to_decimal(share.min_price_increment),
                currency=share.currency,
                name=share.name,
            )
        )
    return instruments


async def validate_account(services: object, account_id: str) -> dict[str, str]:
    response = await services.users.get_accounts()
    for account in response.accounts:
        if account.id == account_id:
            return {
                "id": account.id,
                "name": account.name,
                "type": account.type.name,
                "status": account.status.name,
            }
    raise ValueError(f"Account {account_id} not found in GetAccounts.")


async def market_data_request_iterator(
    instrument_ids: list[str],
    *,
    depth: int,
    stop_event: asyncio.Event,
) -> AsyncIterator[MarketDataRequest]:
    yield MarketDataRequest(
        subscribe_order_book_request=SubscribeOrderBookRequest(
            subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
            instruments=[
                OrderBookInstrument(instrument_id=instrument_id, depth=depth)
                for instrument_id in instrument_ids
            ],
        )
    )
    yield MarketDataRequest(ping_settings=PingDelaySettings(ping_delay_ms=1000))
    await stop_event.wait()


async def stream_orderbooks(
    services: object,
    instruments: list[InstrumentSpec],
    *,
    depth: int,
    stop_event: asyncio.Event,
    idle_timeout_seconds: float,
    reconnect_delay_seconds: float,
    poll_fallback_enabled: bool,
    poll_fallback_interval_seconds: float,
) -> AsyncIterator[MarketSnapshot]:
    spec_by_uid = {instrument.instrument_id: instrument for instrument in instruments}
    instrument_ids = [instrument.instrument_id for instrument in instruments]
    loop = asyncio.get_running_loop()
    last_fallback_poll_monotonic: float | None = None
    while not stop_event.is_set():
        request_iterator = market_data_request_iterator(
            instrument_ids,
            depth=depth,
            stop_event=stop_event,
        )
        stream = services.market_data_stream.market_data_stream(request_iterator)
        reconnect_reason = "stream_closed"
        try:
            while not stop_event.is_set():
                try:
                    event = await asyncio.wait_for(anext(stream), timeout=idle_timeout_seconds)
                except StopAsyncIteration:
                    reconnect_reason = "stream_closed"
                    break
                except asyncio.TimeoutError:
                    reconnect_reason = f"idle_timeout_{idle_timeout_seconds:.0f}s"
                    break
                orderbook = event.orderbook
                if orderbook is None:
                    continue
                spec = spec_by_uid.get(orderbook.instrument_uid)
                if spec is None or not orderbook.bids or not orderbook.asks:
                    continue
                bid = orderbook.bids[0]
                ask = orderbook.asks[0]
                yield MarketSnapshot(
                    instrument=spec,
                    bid_price=quotation_to_decimal(bid.price),
                    ask_price=quotation_to_decimal(ask.price),
                    bid_quantity=int(bid.quantity),
                    ask_quantity=int(ask.quantity),
                    at=orderbook.time,
                )
        except Exception as exc:  # noqa: BLE001
            reconnect_reason = f"{type(exc).__name__}: {exc}"
        finally:
            await _safe_aclose(stream)
            await _safe_aclose(request_iterator)

        if stop_event.is_set():
            break
        if poll_fallback_enabled and _should_run_poll_fallback(
            now_monotonic=loop.time(),
            last_poll_monotonic=last_fallback_poll_monotonic,
            min_interval_seconds=poll_fallback_interval_seconds,
        ):
            last_fallback_poll_monotonic = loop.time()
            fallback_count = 0
            async for snapshot in poll_orderbooks_once(
                services,
                instruments,
                depth=depth,
            ):
                fallback_count += 1
                yield snapshot
            if fallback_count > 0:
                LOGGER.warning(
                    "Market data fallback poll served snapshots=%s reason=%s",
                    fallback_count,
                    reconnect_reason,
                )

        LOGGER.warning(
            "Market data stream reconnecting reason=%s delay=%.1fs",
            reconnect_reason,
            reconnect_delay_seconds,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=reconnect_delay_seconds)
        except asyncio.TimeoutError:
            continue


async def _safe_aclose(stream: object) -> None:
    closer = getattr(stream, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001
        return


async def poll_orderbooks_once(
    services: object,
    instruments: list[InstrumentSpec],
    *,
    depth: int,
    request_timeout_seconds: float = POLL_FALLBACK_RPC_TIMEOUT_SECONDS,
) -> AsyncIterator[MarketSnapshot]:
    for instrument in instruments:
        try:
            orderbook = await asyncio.wait_for(
                services.market_data.get_order_book(
                    instrument_id=instrument.instrument_id,
                    depth=depth,
                ),
                timeout=request_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Market data fallback poll failed ticker=%s error=%s",
                instrument.ticker,
                exc,
            )
            continue
        snapshot = orderbook_response_to_snapshot(orderbook, instrument=instrument)
        if snapshot is not None:
            yield snapshot


def orderbook_response_to_snapshot(
    orderbook: object,
    *,
    instrument: InstrumentSpec,
) -> MarketSnapshot | None:
    bids = getattr(orderbook, "bids", None) or []
    asks = getattr(orderbook, "asks", None) or []
    if not bids or not asks:
        return None
    bid = bids[0]
    ask = asks[0]
    return MarketSnapshot(
        instrument=instrument,
        bid_price=quotation_to_decimal(bid.price),
        ask_price=quotation_to_decimal(ask.price),
        bid_quantity=int(bid.quantity),
        ask_quantity=int(ask.quantity),
        at=getattr(orderbook, "time", None) or datetime.now(timezone.utc),
    )


def _should_run_poll_fallback(
    *,
    now_monotonic: float,
    last_poll_monotonic: float | None,
    min_interval_seconds: float,
) -> bool:
    if last_poll_monotonic is None:
        return True
    return (now_monotonic - last_poll_monotonic) >= min_interval_seconds
