"""TownGas Meter integration — read gas meter via Bearer Token + refreshToken."""

from __future__ import annotations

import logging
import time
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ERR_TOKEN_INVALID,
    AuthError,
    TownGasApi,
    is_ok_code,
    result_code,
    result_message,
    strip_html,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_DEFAULT_PRICE,
    CONF_ORG_CODE,
    CONF_ORG_NAME,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SUBS_ID,
    CONF_TOKEN_CREATE_TIME,
    CONF_TOKEN_EXPIRES_IN,
    CONF_TOKEN_REFRESH_INTERVAL,
    DEFAULT_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

CONFIG_ENTRY_VERSION = 2

type TownGasConfigEntry = ConfigEntry

_CST = zoneinfo.ZoneInfo("Asia/Shanghai")


def _in_maintenance_window() -> bool:
    """True during TownGas daily maintenance (23:30–00:30 CST)."""
    now = datetime.now(tz=_CST)
    return (now.hour == 23 and now.minute >= 30) or (now.hour == 0 and now.minute < 30)


async def _triage_error_10001(api: TownGasApi, subs_id: str, api_msg: str) -> None:
    """区分 10001 的两种真实原因，给出可执行的处置。

    - 账号信息接口能读到户号 → 令牌没问题，是户号不属于该账号 → UpdateFailed
    - 账号信息接口也读不到     → 登录态/分公司不匹配     → 触发重新授权
    """
    try:
        account = await api.fetch_account()
    except AuthError as err:
        raise ConfigEntryAuthFailed(f"港华 10001 且刷新令牌失败: {err}") from err
    except Exception:  # noqa: BLE001 — 诊断失败不应掩盖原始错误
        _LOGGER.debug("10001 诊断：账号信息接口异常", exc_info=True)
        account = {}

    subs = account.get("subs") or []
    if subs:
        available = "、".join(
            item.get("subs_code") or item.get("subs_id", "")[:8] for item in subs
        )
        _LOGGER.error(
            "令牌有效，但户号 %s 不在当前登录账号名下；可用户号：%s", subs_id, available,
        )
        raise UpdateFailed(
            f"户号不属于当前登录账号（港华返回 10001）。"
            f"当前账号名下有：{available}。"
            f"请在「重新授权」里重新选择户号，或删除该条目后重新添加。"
        )

    _LOGGER.warning(
        "港华 10001 且账号信息接口同样不可用（%s）→ 判定为登录态失效，触发重新授权", api_msg,
    )
    raise ConfigEntryAuthFailed(
        "港华返回 10001（登录态失效或分公司/令牌不匹配），需要重新微信授权"
    )


async def async_setup_entry(hass: HomeAssistant, entry: TownGasConfigEntry) -> bool:
    """Set up TownGas Meter from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=False)

    api = TownGasApi(
        session=session,
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_create_time=entry.data.get(CONF_TOKEN_CREATE_TIME, 0),
        token_expires_in=entry.data.get(CONF_TOKEN_EXPIRES_IN, 7200),
        client_id=entry.data.get(CONF_CLIENT_ID) or None,
    )

    subs_id = entry.data[CONF_SUBS_ID]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    token_refresh_interval = entry.options.get(
        CONF_TOKEN_REFRESH_INTERVAL,
        entry.data.get(CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL),
    )

    async def _update() -> dict:
        _LOGGER.debug(
            "coordinator 开始更新 — token_remain=%ss bearer_valid=%s",
            api.bearer_remain,
            api.bearer_valid,
        )
        # Skip data fetch during daily maintenance window (23:30–00:30 CST).
        # Token refresh runs on its own timer and is unaffected.
        if _in_maintenance_window():
            _LOGGER.debug("处于维护期（CST 23:30-00:30），跳过本次数据刷新")
            if coordinator.data is not None:
                return coordinator.data
            raise UpdateFailed("维护期内无历史数据，等待维护结束后自动恢复")

        # The independent _token_refresh_job timer handles proactive refresh.
        # This is a safety net: if the token is already expired at update time
        # (e.g. HA just restarted after a long sleep), refresh immediately.
        if not api.bearer_valid:
            _LOGGER.debug("刷新 token (remain=%ss)", api.bearer_remain)
            try:
                if not await api.refresh_access_token():
                    _LOGGER.warning("token 刷新失败（网络问题），继续使用当前 token")
            except AuthError as err:
                _LOGGER.warning("token 刷新确认失败，需要重新授权: %s", err)
                raise ConfigEntryAuthFailed(str(err)) from err
        try:
            result = await api.pre_check(subs_id)
        except AuthError as err:
            _LOGGER.warning("Towngas 认证失败，需要重新授权: %s", err)
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            _LOGGER.exception("Towngas API 请求失败: %s", err)
            raise UpdateFailed(f"API 请求失败: {err}") from err

        # Detect API-level errors and incomplete responses (e.g. maintenance
        # window 23:30-00:30). Raise UpdateFailed so coordinator keeps last
        # good data and sensor stays at its last known value instead of
        # flipping to "unknown".
        if isinstance(result, dict):
            datas = result.get("datas") if isinstance(result.get("datas"), dict) else {}
            err_code = result_code(result)
            err_msg = strip_html(result_message(result))
            if not is_ok_code(err_code):
                _LOGGER.warning("API 返回错误 %s: %s", err_code, err_msg)
                if str(err_code) == ERR_TOKEN_INVALID:
                    # 10001 是港华网关的「万能失败码」，页面提示往往是
                    # 「系统正在升级中......」。真实原因只有两种：
                    #   1) 登录态失效 / 令牌与分公司不匹配 → 需要重新授权
                    #   2) 令牌有效，但这个户号不在账号名下 → 换户号即可
                    # 用「账号信息接口能不能读到户号」来区分。
                    await _triage_error_10001(api, subs_id, err_msg)
                raise UpdateFailed(f"API 错误 {err_code}: {err_msg}")
            # Also raise when datas is present but has no readingRptList —
            # the API sometimes returns an empty/partial response at the edge
            # of the maintenance window without an explicit errorCode.
            if isinstance(datas, dict):
                lst = datas.get("readingRptList")
                gas_fee = datas.get("gasFee")
                has_reading = (
                    (lst and isinstance(lst, list) and isinstance(lst[0], dict) and lst[0].get("currReading") is not None)
                    or (isinstance(gas_fee, dict) and gas_fee.get("currReading") is not None)
                )
                if not has_reading:
                    _LOGGER.warning("API 返回数据无有效读数（readingRptList 和 gasFee 均无 currReading），保留上次读数")
                    raise UpdateFailed("API 数据不完整：无有效读数")

        has_datas = isinstance(result, dict) and "datas" in result
        has_curr = False
        if has_datas:
            datas = result["datas"]
            if isinstance(datas, dict):
                lst = datas.get("readingRptList")
                if lst and isinstance(lst, list) and isinstance(lst[0], dict):
                    has_curr = "currReading" in lst[0]
        _LOGGER.debug(
            "coordinator 更新 result_keys=%s has_datas=%s has_currReading=%s",
            list(result.keys()) if isinstance(result, dict) else None,
            has_datas, has_curr,
        )
        _persist_tokens(hass, entry, api)
        return result

    # schedule_info is a mutable dict shared with sensor/button entities so they
    # can surface next-refresh times without needing a coordinator update cycle.
    schedule_info: dict[str, Any] = {
        "next_data_refresh": None,
        "next_token_refresh": None,
    }

    def _schedule_token_refresh(delay: float) -> None:
        """Record the next token-refresh time and arm the one-shot timer."""
        schedule_info["next_token_refresh"] = datetime.fromtimestamp(
            time.time() + delay, tz=timezone.utc
        )
        entry.async_on_unload(async_call_later(hass, delay, _token_refresh_job))

    async def _token_refresh_job(_now=None) -> None:
        """Independent timer-based token refresh — decoupled from scan_interval."""
        try:
            if await api.refresh_access_token():
                _persist_tokens(hass, entry, api)
                _LOGGER.debug("定时 token 刷新成功, remain=%ss", api.bearer_remain)
            else:
                _LOGGER.warning("定时 token 刷新失败（网络问题），将在下次尝试")
        except AuthError as err:
            _LOGGER.warning("定时 token 刷新永久失败，触发重新授权: %s", err)
            entry.async_start_reauth(hass)
            schedule_info["next_token_refresh"] = None
            return  # Don't reschedule
        _schedule_token_refresh(token_refresh_interval)

    # Schedule first token refresh. If the remaining bearer lifetime is shorter
    # than the configured interval (e.g. HA restarted with a near-expired token),
    # fire early enough to refresh before the token expires.
    _first_delay = min(token_refresh_interval, max(api.bearer_remain - 60, 60))
    _schedule_token_refresh(_first_delay)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"towngas_{subs_id[:8]}",
        update_method=_update,
        update_interval=timedelta(seconds=scan_interval),
    )

    await coordinator.async_config_entry_first_refresh()

    # Populate next_data_refresh after first successful fetch, then keep it
    # updated via a coordinator listener on subsequent updates.
    def _on_coordinator_update() -> None:
        if coordinator.last_update_success:
            schedule_info["next_data_refresh"] = datetime.fromtimestamp(
                time.time() + scan_interval, tz=timezone.utc
            )

    _on_coordinator_update()
    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "schedule_info": schedule_info,
        "branch": entry.data.get(CONF_ORG_NAME) or entry.data.get(CONF_ORG_CODE) or "",
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_options_updated(
        _hass: HomeAssistant, _entry: TownGasConfigEntry,
    ) -> None:
        """Reload the entry only when actual options (not token data) change.

        HA fires update_listeners for BOTH data and options changes via
        async_update_entry. _persist_tokens only updates data (tokens), so
        comparing current options against the values captured at setup time
        lets us skip the reload when only tokens changed — preventing a
        reload loop that creates duplicate token-refresh timers.
        """
        if (
            _entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) == scan_interval
            and _entry.options.get(CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL) == token_refresh_interval
        ):
            return
        await _hass.config_entries.async_reload(_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: TownGasConfigEntry) -> bool:
    """Migrate v1 entries (clientId 写死在代码里) to v2 (clientId 存在条目里).

    v1.x 的令牌与 clientId 无关（refreshToken 接口只需要 refresh_token + sign），
    所以升级时不需要重新扫码：这里只补齐字段，clientId 留空，
    等真正需要重新授权时再让用户选择分公司即可。
    """
    _LOGGER.debug(
        "迁移配置条目 %s：version %s → %s",
        getattr(entry, "entry_id", "?"), entry.version, CONFIG_ENTRY_VERSION,
    )

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error("配置条目版本 %s 高于当前集成支持的 %s，请升级集成", entry.version, CONFIG_ENTRY_VERSION)
        return False

    if entry.version == 1:
        new_data = {**entry.data}
        new_data.setdefault(CONF_CLIENT_ID, "")
        new_data.setdefault(CONF_ORG_CODE, "")
        new_data.setdefault(CONF_ORG_NAME, "")
        new_options = {**entry.options}
        new_options.setdefault(CONF_DEFAULT_PRICE, DEFAULT_PRICE)
        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=CONFIG_ENTRY_VERSION
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TownGasConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok


def _persist_tokens(
    hass: HomeAssistant, entry: TownGasConfigEntry, api: TownGasApi,
) -> None:
    """Write refreshed tokens back to config entry so they survive restarts."""
    new_data = {**entry.data}
    changed = False
    for key, val in (
        (CONF_ACCESS_TOKEN, api.access_token),
        (CONF_REFRESH_TOKEN, api.refresh_token),
        (CONF_TOKEN_CREATE_TIME, api.token_create_time),
        (CONF_TOKEN_EXPIRES_IN, api.token_expires_in),
    ):
        if new_data.get(key) != val:
            new_data[key] = val
            changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=new_data)
