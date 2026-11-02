"""Track daily gas usage by diffing consecutive meter readings.

The TownGas API only ever returns the *latest* reading, so "yesterday's usage"
has to be derived: we persist the last observed (date, reading) pair and, when
the reading date advances, the difference becomes the usage of the day that
just closed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "hztowngas_daily"
DEFAULT_PRICE = 3.45


def _to_float(value: Any) -> float | None:
    """Best-effort numeric conversion."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_date(value: Any) -> str | None:
    """Normalise recordDate to YYYY-MM-DD when possible."""
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _day_gap(start: str | None, end: str | None) -> int:
    """Whole days between two YYYY-MM-DD strings, minimum 1."""
    if not start or not end:
        return 1
    try:
        d1 = datetime.strptime(start, "%Y-%m-%d")
        d2 = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return 1
    return max((d2 - d1).days, 1)


def extract_reading(datas: dict[str, Any]) -> tuple[float | None, str | None]:
    """Pull the latest cumulative reading + its record date."""
    reports = datas.get("readingRptList")
    if isinstance(reports, list) and reports and isinstance(reports[0], dict):
        report = reports[0]
        reading = _to_float(report.get("currReading"))
        if reading is not None:
            return reading, _norm_date(report.get("recordDate"))

    gas_fee = datas.get("gasFee")
    if isinstance(gas_fee, dict):
        reading = _to_float(gas_fee.get("currReading"))
        if reading is not None:
            return reading, _norm_date(gas_fee.get("recordDate"))

    return None, None


def extract_price(datas: dict[str, Any]) -> float | None:
    """Pull the unit price (元/m³) from the latest bill when available."""
    gas_fee = datas.get("gasFee")
    if isinstance(gas_fee, dict):
        bills = gas_fee.get("gasFeeList")
        if isinstance(bills, list):
            for bill in bills:
                if not isinstance(bill, dict):
                    continue
                price = _to_float(bill.get("price"))
                if price and price > 0:
                    return price

    price = _to_float(datas.get("price"))
    if price and price > 0:
        return price

    return None


class DailyUsageTracker:
    """Persist the previous reading and derive per-day usage."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, default_price: float = DEFAULT_PRICE
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )
        self._default_price: float = (
            float(default_price) if default_price and default_price > 0 else DEFAULT_PRICE
        )
        self._state: dict[str, Any] = {}

        self.usage: float | None = None
        self.usage_start: str | None = None
        self.usage_end: str | None = None
        self.days: int = 1
        self.prev_reading: float | None = None
        self.curr_reading: float | None = None
        self.price: float = self._default_price
        self.price_source: str = "default"

    async def async_load(self) -> None:
        """Restore the persisted baseline."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._state = stored
            self.usage = _to_float(stored.get("usage"))
            self.usage_start = stored.get("usage_start")
            self.usage_end = stored.get("usage_end")
            saved_days = stored.get("days")
            self.days = int(saved_days) if isinstance(saved_days, int) and saved_days > 0 else 1
            self.prev_reading = _to_float(stored.get("prev_reading"))
        _LOGGER.debug("DailyUsageTracker restored: %s", self._state)

    async def async_handle(self, datas: dict[str, Any]) -> None:
        """Ingest a fresh API payload and update the daily counters."""
        reading, record_date = extract_reading(datas)
        if reading is None:
            return
        if not record_date:
            # Some branches omit recordDate; fall back to today (CST) so the
            # same-day / new-day logic still works.
            record_date = dt_util.now().strftime("%Y-%m-%d")

        api_price = extract_price(datas)
        self.price = api_price if api_price else self._default_price
        self.price_source = "api" if api_price else "default"
        self.curr_reading = reading

        baseline_date = self._state.get("date")
        baseline_reading = _to_float(self._state.get("reading"))

        if baseline_reading is None or not baseline_date:
            await self._reset(record_date, reading)
            return

        if record_date and record_date < baseline_date:
            # Reading went backwards (meter swap / API glitch) — restart baseline.
            _LOGGER.info(
                "读数日期回退 %s < %s，重置基线", record_date, baseline_date
            )
            await self._reset(record_date, reading)
            return

        if record_date == baseline_date:
            # Same day: the meter may revise the reading, but there is no new
            # completed day yet. Only keep the larger value as the baseline.
            if reading > baseline_reading:
                self._state["reading"] = reading
                await self._save()
            self.curr_reading = reading
            return

        if reading < baseline_reading:
            _LOGGER.warning(
                "读数回退 %.3f < %.3f，重置基线", reading, baseline_reading
            )
            await self._reset(record_date, reading)
            return

        # A new day's reading arrived → the delta belongs to the day that
        # just closed.
        delta = round(reading - baseline_reading, 3)
        self.usage = delta
        self.usage_start = baseline_date
        self.usage_end = record_date
        self.prev_reading = baseline_reading
        self.days = _day_gap(baseline_date, record_date)

        self._state.update(
            {
                "date": record_date,
                "reading": reading,
                "usage": delta,
                "usage_start": baseline_date,
                "usage_end": record_date,
                "prev_reading": baseline_reading,
                "days": self.days,
            }
        )
        await self._save()
        _LOGGER.info(
            "新抄表日 %s：上期 %.3f → 本期 %.3f，用量 %.3f m³（%s 天）",
            record_date, baseline_reading, reading, delta, self.days,
        )

    async def _reset(self, record_date: str | None, reading: float) -> None:
        """Start a fresh baseline without producing a usage value."""
        self.curr_reading = reading
        self._state = {
            "date": record_date,
            "reading": reading,
            "usage": self.usage,
            "usage_start": self.usage_start,
            "usage_end": self.usage_end,
            "prev_reading": self.prev_reading,
            "days": self.days,
        }
        await self._save()

    async def _save(self) -> None:
        await self._store.async_save(self._state)
