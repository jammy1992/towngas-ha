"""Sensor platform for TownGas Meter — reading, balance, daily usage & cost."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import CONF_DEFAULT_PRICE, CONF_SUBS_CODE, CONF_SUBS_ID, DEFAULT_PRICE, DOMAIN
from .daily import DailyUsageTracker

_LOGGER = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    """Best-effort numeric conversion."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datas(coordinator: DataUpdateCoordinator) -> dict[str, Any]:
    """Return the `datas` object of the last payload, or an empty dict."""
    payload = coordinator.data
    if not isinstance(payload, dict):
        return {}
    datas = payload.get("datas")
    return datas if isinstance(datas, dict) else {}


class TownGasBaseSensor(CoordinatorEntity, SensorEntity):
    """Shared device registration for every entity of one 户号."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        subs_id: str,
        subs_code: str,
        branch: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._subs_id = subs_id
        self._subs_code = subs_code
        self._branch = branch

    @property
    def device_info(self) -> dict[str, Any]:
        model = "NB-IoT 燃气表"
        if self._branch:
            model = f"NB-IoT 燃气表（{self._branch}）"
        return {
            "identifiers": {(DOMAIN, self._subs_id)},
            "name": f"港华燃气表 {self._subs_code}",
            "manufacturer": "港华燃气",
            "model": model,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator = data["coordinator"]
    schedule_info: dict = data["schedule_info"]
    subs_id = entry.data[CONF_SUBS_ID]
    subs_code = entry.data.get(CONF_SUBS_CODE, subs_id[:8])
    branch: str = data.get("branch", "")
    default_price = float(entry.options.get(CONF_DEFAULT_PRICE, DEFAULT_PRICE))

    tracker = DailyUsageTracker(hass, entry.entry_id, default_price=default_price)
    await tracker.async_load()
    if coordinator.data:
        await tracker.async_handle(_datas(coordinator))
    # 供 diagnostics 读取日报内部状态
    data["tracker"] = tracker

    def _on_coordinator_update() -> None:
        """Feed every successful payload into the daily tracker first."""
        if not coordinator.last_update_success:
            return
        datas = _datas(coordinator)
        if not datas:
            return
        hass.async_create_task(tracker.async_handle(datas))

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))

    async_add_entities(
        [
            TownGasMeterSensor(coordinator, entry, subs_id, subs_code, schedule_info, branch),
            TownGasBalanceSensor(coordinator, subs_id, subs_code, branch),
            TownGasDailyUsageSensor(coordinator, tracker, subs_id, subs_code, branch),
            TownGasDailyCostSensor(coordinator, tracker, subs_id, subs_code, branch),
        ],
        True,
    )


class TownGasMeterSensor(TownGasBaseSensor):
    """Gas meter total reading in m³."""

    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:meter-gas"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        subs_id: str,
        subs_code: str,
        schedule_info: dict,
        branch: str = "",
    ) -> None:
        super().__init__(coordinator, subs_id, subs_code, branch)
        self._schedule_info = schedule_info
        self._attr_unique_id = f"towngas_{subs_id}_reading"
        self._attr_translation_key = "gas_meter_reading"

    def _get_curr_reading(self) -> str | None:
        """从 readingRptList[0].currReading 读取，月初 fallback 到 gasFee.currReading."""
        datas = _datas(self.coordinator)
        reports = datas.get("readingRptList")
        if isinstance(reports, list) and reports:
            first = reports[0]
            if isinstance(first, dict) and first.get("currReading") is not None:
                return first.get("currReading")
        gas_fee = datas.get("gasFee")
        if isinstance(gas_fee, dict):
            return gas_fee.get("currReading")
        return None

    @property
    def native_value(self) -> float | None:
        return _to_float(self._get_curr_reading())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        datas = _datas(self.coordinator)
        attrs: dict[str, Any] = {}
        for key in (
            "subsCode", "subsName", "subsAddr", "chargeType",
            "totalFee", "errorCode", "errorMsg", "savingSum",
        ):
            val = datas.get(key)
            if val is not None:
                attrs[key] = val
        reports = datas.get("readingRptList")
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            rpt = reports[0]
            for k in ("recordDate", "resId", "amount"):
                if rpt.get(k) is not None:
                    attrs[k] = rpt[k]
        gas_fee = datas.get("gasFee")
        if isinstance(gas_fee, dict):
            for k in ("lastReading", "totalAmount", "totalFee", "recordDate",
                      "orgShortName", "orgCode"):
                if gas_fee.get(k) is not None:
                    attrs.setdefault(k, gas_fee[k])
            fee_list = gas_fee.get("gasFeeList")
            if fee_list and isinstance(fee_list, list) and isinstance(fee_list[0], dict):
                bill = fee_list[0]
                for k in ("yrMonth", "amount", "price", "chrgSum", "unpaidFee",
                          "paidSum", "lateFeeDate", "lastReading", "currReading"):
                    if bill.get(k) is not None:
                        attrs.setdefault(f"bill_{k}", bill[k])

        now = dt_util.utcnow()
        for key in ("next_data_refresh", "next_token_refresh"):
            ts = self._schedule_info.get(key)
            if ts is not None:
                delta = int((ts - now).total_seconds())
                attrs[key] = ts.isoformat()
                attrs[f"{key}_in"] = f"{max(delta, 0)}s"

        return attrs


class TownGasBalanceSensor(TownGasBaseSensor):
    """Account balance (savingSum) in CNY."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:wallet"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        subs_id: str,
        subs_code: str,
        branch: str = "",
    ) -> None:
        super().__init__(coordinator, subs_id, subs_code, branch)
        self._attr_unique_id = f"towngas_{subs_id}_balance"
        self._attr_translation_key = "account_balance"

    @property
    def native_value(self) -> float | None:
        datas = _datas(self.coordinator)
        for key in ("savingSum", "balance", "acctBalance", "remainFee"):
            value = _to_float(datas.get(key))
            if value is not None:
                return value
        gas_fee = datas.get("gasFee")
        if isinstance(gas_fee, dict):
            for key in ("savingSum", "balance"):
                value = _to_float(gas_fee.get(key))
                if value is not None:
                    return value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        datas = _datas(self.coordinator)
        attrs: dict[str, Any] = {}
        for key in ("savingSum", "balance", "acctBalance", "remainFee"):
            if _to_float(datas.get(key)) is not None:
                attrs["取值字段"] = key
                break
        for key in ("chargeType", "totalFee"):
            if datas.get(key) not in (None, ""):
                attrs[key] = datas[key]
        reports = datas.get("readingRptList")
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            if reports[0].get("recordDate"):
                attrs["recordDate"] = reports[0]["recordDate"]
        return attrs


class TownGasDailyUsageSensor(TownGasBaseSensor):
    """Usage of the last completed day, derived by diffing two readings."""

    _attr_icon = "mdi:gas-burner"
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        tracker: DailyUsageTracker,
        subs_id: str,
        subs_code: str,
        branch: str = "",
    ) -> None:
        super().__init__(coordinator, subs_id, subs_code, branch)
        self._tracker = tracker
        self._attr_unique_id = f"towngas_{subs_id}_daily_usage"
        self._attr_translation_key = "daily_usage"

    @property
    def native_value(self) -> float | None:
        return self._tracker.usage

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "开始日期": self._tracker.usage_start,
            "结束日期": self._tracker.usage_end,
            "覆盖天数": self._tracker.days,
            "上期读数": self._tracker.prev_reading,
            "本期读数": self._tracker.curr_reading,
            "单价": self._tracker.price,
            "状态": self._tracker.status,
            "快照天数": self._tracker.snapshot_days,
            "最新快照日期": self._tracker.latest_date,
            "最新读数时间": self._tracker.latest_observed,
            "接口抄表日": self._tracker.latest_record_date,
        }


class TownGasDailyCostSensor(TownGasBaseSensor):
    """Cost of the last completed day = daily usage × unit price."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "CNY"
    _attr_icon = "mdi:cash"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        tracker: DailyUsageTracker,
        subs_id: str,
        subs_code: str,
        branch: str = "",
    ) -> None:
        super().__init__(coordinator, subs_id, subs_code, branch)
        self._tracker = tracker
        self._attr_unique_id = f"towngas_{subs_id}_daily_cost"
        self._attr_translation_key = "daily_cost"

    @property
    def native_value(self) -> float | None:
        usage = self._tracker.usage
        if usage is None:
            return None
        return round(usage * self._tracker.price, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "用量": self._tracker.usage,
            "单价": self._tracker.price,
            "单价来源": "接口" if self._tracker.price_source == "api" else "默认值",
            "开始日期": self._tracker.usage_start,
            "结束日期": self._tracker.usage_end,
            "状态": self._tracker.status,
        }
