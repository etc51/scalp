from __future__ import annotations

from datetime import datetime

from .config import ScalperConfig


def moment_in_entry_window(config: ScalperConfig, moment: datetime) -> tuple[bool, str]:
    local_now = moment.astimezone(config.timezone)
    if local_now.weekday() not in config.entry_weekdays:
        return False, "entry_weekday_closed"
    current_time = local_now.time().replace(tzinfo=None)
    if current_time < config.entry_start_time:
        return False, "entry_before_window"
    if current_time > config.entry_end_time:
        return False, "entry_after_window"
    return True, "ok"
