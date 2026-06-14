from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
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
) -> AsyncIterator[MarketSnapshot]:
    spec_by_uid = {instrument.instrument_id: instrument for instrument in instruments}
    request_iterator = market_data_request_iterator(
        [instrument.instrument_id for instrument in instruments],
        depth=depth,
        stop_event=stop_event,
    )
    async for event in services.market_data_stream.market_data_stream(request_iterator):
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
