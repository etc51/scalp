from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import InstrumentSpec, MarketSnapshot


class MarketSnapshotRecorder:
    def __init__(self, runtime_dir: Path, timezone_info: Any) -> None:
        self.runtime_dir = runtime_dir
        self.timezone_info = timezone_info
        self.market_dir = runtime_dir / "market"
        self.market_dir.mkdir(parents=True, exist_ok=True)

    def append(self, snapshot: MarketSnapshot) -> None:
        path = self.snapshot_file_for(snapshot.at)
        payload = {
            "at": snapshot.at.isoformat(),
            "ticker": snapshot.instrument.ticker,
            "instrument_id": snapshot.instrument.instrument_id,
            "class_code": snapshot.instrument.class_code,
            "figi": snapshot.instrument.figi,
            "lot_size": snapshot.instrument.lot_size,
            "min_price_increment": str(snapshot.instrument.min_price_increment),
            "currency": snapshot.instrument.currency,
            "name": snapshot.instrument.name,
            "bid_price": str(snapshot.bid_price),
            "ask_price": str(snapshot.ask_price),
            "bid_quantity": snapshot.bid_quantity,
            "ask_quantity": snapshot.ask_quantity,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def snapshot_file_for(self, moment: datetime) -> Path:
        local_day = moment.astimezone(self.timezone_info).date().isoformat()
        return self.market_dir / f"{local_day}.jsonl"


def snapshot_path_for_date(runtime_dir: Path, date_key: str) -> Path:
    return runtime_dir / "market" / f"{date_key}.jsonl"


def load_snapshots(path: Path) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    if not path.exists():
        return snapshots
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                instrument = InstrumentSpec(
                    instrument_id=str(item["instrument_id"]),
                    ticker=str(item["ticker"]),
                    class_code=str(item["class_code"]),
                    figi=str(item["figi"]),
                    lot_size=int(item["lot_size"]),
                    min_price_increment=Decimal(str(item["min_price_increment"])),
                    currency=str(item["currency"]),
                    name=str(item["name"]),
                )
                snapshots.append(
                    MarketSnapshot(
                        instrument=instrument,
                        bid_price=Decimal(str(item["bid_price"])),
                        ask_price=Decimal(str(item["ask_price"])),
                        bid_quantity=int(item["bid_quantity"]),
                        ask_quantity=int(item["ask_quantity"]),
                        at=datetime.fromisoformat(str(item["at"])),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
                continue
    return snapshots


def load_snapshots_from_paths(paths: list[Path]) -> list[MarketSnapshot]:
    combined: list[MarketSnapshot] = []
    for path in paths:
        combined.extend(load_snapshots(path))
    combined.sort(key=lambda item: item.at)
    return combined
