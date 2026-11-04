"""Diagnostics for the TownGas integration.

「设置 → 设备与服务 → 港华燃气 → 三个点 → 下载诊断信息」会调用这里。
输出里令牌做了脱敏，可以放心贴出来排错。
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DOMAIN

_SECRET_KEYS = (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN)


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """令牌只留前后 4 位，方便确认「有没有」而不泄露内容。"""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in _SECRET_KEYS and isinstance(value, str) and value:
            out[key] = f"***{value[:4]}…{value[-4:]} (len={len(value)})***"
        else:
            out[key] = value
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    tracker = runtime.get("tracker")

    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": _redact(dict(entry.data)),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "last_update_success_time": str(
                getattr(coordinator, "last_update_success_time", "")
            ),
            "payload": getattr(coordinator, "data", None),
        },
    }

    if tracker is not None:
        state = getattr(tracker, "_state", {}) or {}
        diagnostics["daily"] = {
            "status": getattr(tracker, "status", None),
            "usage": getattr(tracker, "usage", None),
            "price": getattr(tracker, "price", None),
            "price_source": getattr(tracker, "price_source", None),
            "snapshots": state.get("snapshots", {}),
        }

    return diagnostics
