"""Track daily gas usage by diffing daily meter snapshots.

Why snapshots and not "diff by recordDate jump":

The TownGas API returns a *reading date* and a *cumulative reading*, but the two
are not updated at the same time. In practice the date advances at midnight while
the reading for that day only lands a few hours later. An earlier version of this
module settled usage the moment the date changed, which produced a delta of 0 at
midnight and then silently swallowed the real increment as a "same-day revision"
— the daily sensor stayed at 0 forever.

This version instead keeps one snapshot per **local observation day** (the newest
reading seen that day) and settles usage between the two most recent *completed*
days. Late-arriving readings simply raise today's snapshot; nothing is settled
before the day is over, so the lag no longer matters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 2
STORAGE_KEY = "hztowngas_daily"
DEFAULT_PRICE = 3.45
MAX_SNAPSHOTS = 31
DATE_FMT = "%Y-%m-%d"


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


def _day_gap(start: str, end: str) -> int:
    """Whole days between two YYYY-MM-DD strings, minimum 1."""
    try:
        d1 = datetime.strptime(start, DATE_FMT)
        d2 = datetime.strptime(end, DATE_FMT)
    except ValueError:
        return 1
    return max((d2 - d1).days, 1)


def extract_reading(datas: dict[str, Any]) -> tuple[float | None, str | None]:
    """Pull the latest cumulative reading + the date the API attaches to it."""
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
    """Persist one reading snapshot per day and settle usage on completed days."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, default_price: float = DEFAULT_PRICE
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )
        self._default_price: float = (
            float(default_price) if default_price and default_price > 0 else DEFAULT_PRICE
        )
        self._state: dict[str, Any] = {"snapshots": {}}

        self.usage: float | None = None
        self.usage_start: str | None = None
        self.usage_end: str | None = None
        self.days: int = 1
        self.prev_reading: float | None = None
        self.curr_reading: float | None = None
        self.price: float = self._default_price
        self.price_source: str = "default"

        # Diagnostics surfaced as entity attributes.
        self.snapshot_days: int = 0
        self.latest_date: str | None = None
        self.latest_observed: str | None = None
        self.latest_record_date: str | None = None
        self.status: str = "等待数据"

    # ------------------------------------------------------------------ load
    async def async_load(self) -> None:
        """Restore persisted snapshots (and migrate the v1 baseline)."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return

        snapshots = stored.get("snapshots")
        if isinstance(snapshots, dict):
            self._state["snapshots"] = {
                str(k): v for k, v in snapshots.items() if isinstance(v, dict)
            }
        else:
            # v1 state: {"date": ..., "reading": ..., "usage": ...}
            legacy_date = stored.get("date")
            legacy_reading = _to_float(stored.get("reading"))
            if legacy_date and legacy_reading is not None:
                self._state["snapshots"] = {
                    str(legacy_date): {
                        "reading": legacy_reading,
                        "record_date": str(legacy_date),
                        "observed_at": str(stored.get("observed_at") or ""),
                    }
                }
                _LOGGER.info(
                    "迁移旧的日用量基线：%s @ %.3f m³", legacy_date, legacy_reading
                )

        self.usage = _to_float(stored.get("usage"))
        self.usage_start = stored.get("usage_start")
        self.usage_end = stored.get("usage_end")
        saved_days = stored.get("days")
        self.days = int(saved_days) if isinstance(saved_days, int) and saved_days > 0 else 1
        self.prev_reading = _to_float(stored.get("prev_reading"))
        self._refresh_diagnostics()
        _LOGGER.debug(
            "每日用量跟踪器已恢复：%s 个快照，最新 %s", self.snapshot_days, self.latest_date
        )

    # ----------------------------------------------------------------- ingest
    async def async_handle(self, datas: dict[str, Any]) -> None:
        """Ingest a fresh API payload and re-settle the last completed day."""
        reading, record_date = extract_reading(datas)
        if reading is None:
            return

        api_price = extract_price(datas)
        self.price = api_price if api_price else self._default_price
        self.price_source = "api" if api_price else "default"
        self.curr_reading = reading

        now = dt_util.now()
        today = now.strftime(DATE_FMT)
        snapshots: dict[str, dict[str, Any]] = self._state.setdefault("snapshots", {})

        dirty = False
        snap = snapshots.get(today)
        current = _to_float(snap.get("reading")) if isinstance(snap, dict) else None
        if current is None or reading > current:
            snapshots[today] = {
                "reading": reading,
                "record_date": record_date,
                "observed_at": now.isoformat(timespec="seconds"),
            }
            dirty = True

        self._prune(today)
        self._settle(today)
        self._refresh_diagnostics()

        if dirty:
            await self._save()

    # ---------------------------------------------------------------- helpers
    def _prune(self, today: str) -> None:
        """Keep the snapshot map small (and drop future-dated junk)."""
        snapshots: dict[str, dict[str, Any]] = self._state.setdefault("snapshots", {})
        cutoff = (datetime.strptime(today, DATE_FMT) - timedelta(days=MAX_SNAPSHOTS)).strftime(
            DATE_FMT
        )
        for key in [k for k in snapshots if k < cutoff or k > today]:
            snapshots.pop(key, None)

    def _settle(self, today: str) -> None:
        """Usage of the most recent *completed* day = newest closed snapshot − previous."""
        snapshots: dict[str, dict[str, Any]] = self._state.get("snapshots", {})
        closed = sorted(d for d in snapshots if d < today)
        if len(closed) < 2:
            if closed:
                self.status = f"等待第二个完整日（已有快照：{closed[-1]}）"
            else:
                self.status = "等待数据"
            return

        d_end, d_start = closed[-1], closed[-2]
        r_end = _to_float(snapshots[d_end].get("reading"))
        r_start = _to_float(snapshots[d_start].get("reading"))
        if r_end is None or r_start is None:
            self.status = "快照数据不完整"
            return

        delta = round(r_end - r_start, 3)
        if delta < 0:
            # Meter swapped or reading reset — do not publish a negative value.
            _LOGGER.warning(
                "读数回退 %s %.3f → %s %.3f，暂不产出日用量", d_start, r_start, d_end, r_end
            )
            self.usage = None
            self.usage_start = d_start
            self.usage_end = d_end
            self.prev_reading = r_start
            self.days = _day_gap(d_start, d_end)
            self.status = "读数回退，等待新基线"
            return

        self.usage = delta
        self.usage_start = d_start
        self.usage_end = d_end
        self.prev_reading = r_start
        self.days = _day_gap(d_start, d_end)
        self.status = "正常" if self.days == 1 else f"跨 {self.days} 天结算"

        self._state.update(
            {
                "usage": delta,
                "usage_start": d_start,
                "usage_end": d_end,
                "prev_reading": r_start,
                "days": self.days,
            }
        )
        _LOGGER.debug(
            "结算 %s：%s %.3f → %.3f，用量 %.3f m³", d_end, d_start, r_start, r_end, delta
        )

    def _refresh_diagnostics(self) -> None:
        snapshots: dict[str, dict[str, Any]] = self._state.get("snapshots", {})
        self.snapshot_days = len(snapshots)
        if snapshots:
            latest = max(snapshots)
            self.latest_date = latest
            snap = snapshots[latest] or {}
            self.latest_observed = snap.get("observed_at")
            self.latest_record_date = snap.get("record_date")
        else:
            self.latest_date = self.latest_observed = self.latest_record_date = None

    async def _save(self) -> None:
        await self._store.async_save(self._state)
