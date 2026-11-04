"""Config flow for TownGas Meter.

流程（v2.1.1 起「户号自动识别」）：
    user        —— 选择分公司（下拉，内置 176 家）/ 或手填 clientId
    auth        —— 显示微信授权二维码 + 填写 authCode
    pick_subs   —— 账号下有多户时选一个（通常只有 1 户，自动跳过）
    manual_subs —— 自动识别失败时的兜底：手填 subsId / subsCode

重新授权：
    reauth_confirm —— 已知道 clientId 时直接跳到 reauth_token；否则先选分公司
    reauth_token   —— 重新扫码后粘贴 authCode，同样自动识别户号
    pick_subs      —— 原户号不在该账号名下时，从账号里的户号中重新选
"""

from __future__ import annotations

import base64
from io import BytesIO
import json
import logging
from pathlib import Path
from typing import Any

import qrcode
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ERR_TOKEN_INVALID, ClientIdMissing, TownGasApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_CODE,
    CONF_CLIENT_ID,
    CONF_DEFAULT_PRICE,
    CONF_ORG_CODE,
    CONF_ORG_NAME,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SUBS_CODE,
    CONF_SUBS_ID,
    CONF_TOKEN_CREATE_TIME,
    CONF_TOKEN_EXPIRES_IN,
    CONF_TOKEN_REFRESH_INTERVAL,
    DEFAULT_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_REFRESH_INTERVAL,
    DOMAIN,
    build_client_id,
)

_LOGGER = logging.getLogger(__name__)

_BRANCH_FILE = Path(__file__).with_name("branches.json")


def _load_branches() -> tuple[list[SelectOptionDict], dict[str, str]]:
    """读取内置分公司清单，返回 (下拉选项, orgCode→名称)。"""
    try:
        raw = json.loads(_BRANCH_FILE.read_text(encoding="utf-8"))
        branches = raw.get("branches", [])
    except (OSError, ValueError):
        _LOGGER.exception("无法读取 branches.json，分公司列表将为空")
        return [], {}

    options: list[SelectOptionDict] = []
    names: dict[str, str] = {}
    for item in branches:
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if not code or not name:
            continue
        label = name
        desc = str(item.get("desc", "")).strip()
        short = str(item.get("short", "")).strip()
        if short and short != name:
            label = f"{name}（{short}）"
        elif desc and desc not in name:
            label = f"{name}（{desc[:16]}）"
        options.append(SelectOptionDict(value=code, label=label))
        names[code] = short or name
    return options, names


BRANCH_OPTIONS, BRANCH_NAMES = _load_branches()


def _branch_schema(default_org: str | None = None) -> vol.Schema:
    """分公司下拉 + 可选手填 clientId。"""
    fields: dict[Any, Any] = {}
    if BRANCH_OPTIONS:
        selector = SelectSelector(
            SelectSelectorConfig(
                options=BRANCH_OPTIONS,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        key = (
            vol.Required(CONF_ORG_CODE, default=default_org)
            if default_org
            else vol.Required(CONF_ORG_CODE)
        )
        fields[key] = selector
    fields[vol.Optional(CONF_CLIENT_ID, default="")] = str
    return vol.Schema(fields)


def _subs_label(item: dict[str, str]) -> str:
    """户号下拉的显示文案：户号 + 姓名/地址 + subsId 尾号。"""
    code = item.get("subs_code") or ""
    name = item.get("name") or ""
    addr = item.get("addr") or ""
    sid = item.get("subs_id") or ""
    parts = [p for p in (code, name, addr) if p]
    label = " · ".join(parts) or "未命名户号"
    return f"{label}（…{sid[-6:]}）"


class TownGasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TownGas Meter."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """Return the options flow handler."""
        return TownGasOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Init flow."""
        super().__init__()
        self._oauth_url: str = ""
        self._client_id: str = ""
        self._org_code: str = ""
        self._org_name: str = ""
        self._entry: Any = None
        self._errors: dict[str, str] = {}
        self._account_subs: list[dict[str, str]] = []
        self._account_note: str = ""
        self._reauth_mode: bool = False
        # 扫码成功后先攒着，等户号确定了一起落库
        self._pending: dict[str, Any] = {}

    # ------------------------------------------------------------------
    #  helpers
    # ------------------------------------------------------------------

    def _placeholders(self, **extra: str) -> dict[str, str]:
        branch = self._org_name
        if not branch and self._entry is not None:
            branch = self._entry.data.get(CONF_ORG_NAME) or ""
        placeholders = {
            "oauth_url": self._oauth_url,
            "oauth_qr_markdown": self._oauth_qr_markdown(),
            "branch": branch or "未选择（沿用上次的分公司）",
            "client_id": self._client_id,
            "detected": self._detected_text(),
            "account_note": self._account_note,
            "subs_count": str(len(self._account_subs)),
        }
        placeholders.update(extra)
        return placeholders

    def _detected_text(self) -> str:
        if not self._account_subs:
            return "未读取到（可手动填写）"
        return "、".join(
            (item.get("subs_code") or item.get("subs_id", "")[:8]) for item in self._account_subs
        )

    def _oauth_qr_markdown(self) -> str:
        """Generate markdown image from OAuth URL QRCode."""
        if not self._oauth_url:
            return ""
        qr = qrcode.QRCode(border=2, box_size=5)
        qr.add_data(self._oauth_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"![oauth_qr](data:image/png;base64,{b64})"

    async def _prepare_oauth(self, client_id: str, org_code: str = "") -> bool:
        """用给定 clientId 取微信授权链接，失败时把错误写进 self._errors。"""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        api = TownGasApi(session, client_id=client_id)
        self._client_id = client_id
        self._org_code = org_code
        self._org_name = BRANCH_NAMES.get(org_code, "")
        self._oauth_url = ""
        try:
            self._oauth_url = await api.get_oauth_url()
        except ClientIdMissing:
            self._errors = {"base": "client_id_required"}
            return False
        except Exception:
            _LOGGER.exception("获取微信授权链接失败")
            self._errors = {"base": "oauth_url_failed"}
            return False

        if not self._oauth_url:
            self._errors = {"base": "client_id_invalid"}
            return False
        self._errors = {}
        return True

    def _known_client_id(self) -> str:
        data = self._entry.data if self._entry else {}
        return (
            data.get(CONF_CLIENT_ID)
            or build_client_id(data.get(CONF_ORG_CODE, ""))
            or ""
        )

    @staticmethod
    def _extract_auth_code(raw: str) -> str:
        code = (raw or "").strip()
        for sep in ("authCode=", "code="):
            if sep in code:
                code = code.split(sep, 1)[1].split("&", 1)[0]
                break
        return code

    # ------------------------------------------------------------------
    #  户号自动识别（扫码后）
    # ------------------------------------------------------------------

    async def _identify_account(self, api: TownGasApi) -> None:
        """用新 token 读取账号名下的户号；失败不抛异常，只记录原因。"""
        self._account_subs = []
        self._account_note = ""
        try:
            account = await api.fetch_account()
        except Exception as err:  # noqa: BLE001 — 识别失败要能退回手填
            _LOGGER.warning("读取账号户号失败: %s", err)
            self._account_note = f"读取账号信息失败：{err}"
            return

        self._account_subs = account.get("subs") or []
        code = str(account.get("code") or "")
        if self._account_subs:
            _LOGGER.info(
                "账号下识别到 %s 个户号：%s",
                len(self._account_subs), self._detected_text(),
            )
            self._account_note = f"已从账号中读取到 {len(self._account_subs)} 个户号"
        elif code:
            self._account_note = (
                f"读取账号信息返回错误 {code}：{account.get('msg') or '（无说明）'}"
            )
        else:
            self._account_note = "账号信息接口没有返回户号，需要手动填写"

    def _subs_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=item["subs_id"], label=_subs_label(item))
            for item in self._account_subs
        ]

    def _subs_by_id(self, subs_id: str) -> dict[str, str] | None:
        for item in self._account_subs:
            if item.get("subs_id") == subs_id:
                return item
        return None

    # ------------------------------------------------------------------
    #  落库
    # ------------------------------------------------------------------

    def _entry_data(self, subs_id: str, subs_code: str) -> dict[str, Any]:
        data = {
            CONF_SUBS_ID: subs_id,
            CONF_SUBS_CODE: subs_code,
            CONF_CLIENT_ID: self._client_id,
            CONF_ORG_CODE: self._org_code,
            CONF_ORG_NAME: self._org_name,
        }
        data.update(self._pending)
        return data

    def _entry_options(self) -> dict[str, Any]:
        return {
            CONF_SCAN_INTERVAL: self._pending.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            CONF_TOKEN_REFRESH_INTERVAL: self._pending.get(
                CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL
            ),
            CONF_DEFAULT_PRICE: self._pending.get(CONF_DEFAULT_PRICE, DEFAULT_PRICE),
        }

    def _finish_with_subs(self, subs_id: str, subs_code: str) -> ConfigFlowResult:
        title = f"港华燃气 {subs_code}"
        if self._reauth_mode and self._entry is not None:
            return self.async_update_reload_and_abort(
                self._entry,
                data={**self._entry.data, **self._entry_data(subs_id, subs_code)},
                title=title,
                reason="reauth_successful",
            )
        return self.async_create_entry(
            title=title,
            data=self._entry_data(subs_id, subs_code),
            options=self._entry_options(),
        )

    # ------------------------------------------------------------------
    #  first install
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 1: 选择所属分公司（决定微信 OAuth 的 clientId）。"""
        self._reauth_mode = False
        if user_input is not None:
            manual = str(user_input.get(CONF_CLIENT_ID, "")).strip()
            org_code = str(user_input.get(CONF_ORG_CODE, "")).strip()
            client_id = manual or build_client_id(org_code)
            if not client_id:
                self._errors = {"base": "client_id_required"}
            elif await self._prepare_oauth(client_id, org_code):
                return await self.async_step_auth()

        errors = self._errors
        self._errors = {}
        return self.async_show_form(
            step_id="user",
            description_placeholders={
                "branch_count": str(len(BRANCH_OPTIONS)),
                "prefix": "pe92a8wechat",
            },
            data_schema=_branch_schema(user_input.get(CONF_ORG_CODE) if user_input else None),
            errors=errors,
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 2: 扫码后粘贴 authCode —— 户号由集成自动识别。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_code = self._extract_auth_code(user_input[CONF_AUTH_CODE])

            session = async_get_clientsession(self.hass, verify_ssl=False)
            api = TownGasApi(session, client_id=self._client_id)
            data: dict[str, Any] = {}
            try:
                data = await api.exchange_token(auth_code)
            except Exception:
                _LOGGER.exception("Token exchange failed")
                errors["base"] = "exchange_failed"

            if data.get("access_token"):
                self._pending = {
                    CONF_ACCESS_TOKEN: api.access_token,
                    CONF_REFRESH_TOKEN: api.refresh_token,
                    CONF_TOKEN_EXPIRES_IN: api.token_expires_in,
                    CONF_TOKEN_CREATE_TIME: api.token_create_time,
                    CONF_SCAN_INTERVAL: int(
                        user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                    ),
                    CONF_TOKEN_REFRESH_INTERVAL: int(
                        user_input.get(CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL)
                    ),
                    CONF_DEFAULT_PRICE: float(
                        user_input.get(CONF_DEFAULT_PRICE, DEFAULT_PRICE)
                    ),
                }
                await self._identify_account(api)

                if len(self._account_subs) == 1:
                    only = self._account_subs[0]
                    await self.async_set_unique_id(only["subs_id"])
                    self._abort_if_unique_id_configured()
                    return self._finish_with_subs(only["subs_id"], only.get("subs_code", ""))

                if len(self._account_subs) > 1:
                    return await self.async_step_pick_subs()

                # 没读到户号 → 退回手填
                return await self.async_step_manual_subs()

            if not errors:
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="auth",
            description_placeholders=self._placeholders(),
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_CODE): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(int, vol.Range(min=60, max=86400)),
                    vol.Optional(
                        CONF_TOKEN_REFRESH_INTERVAL, default=DEFAULT_TOKEN_REFRESH_INTERVAL
                    ): vol.All(int, vol.Range(min=300, max=7140)),
                    vol.Optional(
                        CONF_DEFAULT_PRICE, default=DEFAULT_PRICE
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=99)),
                }
            ),
            errors=errors,
        )

    async def async_step_pick_subs(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step 3: 账号下有多个户号时，让用户选一个。"""
        if user_input is not None:
            subs_id = str(user_input[CONF_SUBS_ID]).strip()
            item = self._subs_by_id(subs_id)
            if item is None:
                return self.async_show_form(
                    step_id="pick_subs",
                    description_placeholders=self._placeholders(),
                    data_schema=self._pick_schema(),
                    errors={"base": "subs_mismatch"},
                )
            if not self._reauth_mode:
                await self.async_set_unique_id(item["subs_id"])
                self._abort_if_unique_id_configured()
            return self._finish_with_subs(item["subs_id"], item.get("subs_code", ""))

        return self.async_show_form(
            step_id="pick_subs",
            description_placeholders=self._placeholders(),
            data_schema=self._pick_schema(),
        )

    def _pick_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_SUBS_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=self._subs_options(),
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )

    async def async_step_manual_subs(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """兜底：自动识别不到户号时手填 subsId / subsCode。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            subs_id = str(user_input[CONF_SUBS_ID]).strip()
            subs_code = str(user_input[CONF_SUBS_CODE]).strip()
            if not subs_id or not subs_code:
                errors["base"] = "subs_required"
            else:
                session = async_get_clientsession(self.hass, verify_ssl=False)
                api = TownGasApi(
                    session,
                    access_token=self._pending.get(CONF_ACCESS_TOKEN),
                    refresh_token=self._pending.get(CONF_REFRESH_TOKEN),
                    token_create_time=self._pending.get(CONF_TOKEN_CREATE_TIME, 0),
                    token_expires_in=self._pending.get(CONF_TOKEN_EXPIRES_IN, 7200),
                    client_id=self._client_id,
                )
                ok, code, msg = await api.verify_subs(subs_id)
                if ok:
                    if not self._reauth_mode:
                        await self.async_set_unique_id(subs_id)
                        self._abort_if_unique_id_configured()
                    return self._finish_with_subs(subs_id, subs_code)
                if str(code) == ERR_TOKEN_INVALID:
                    errors["base"] = "org_mismatch"
                elif code == "empty":
                    errors["base"] = "subs_no_data"
                else:
                    _LOGGER.warning("手填户号校验失败 code=%s msg=%s", code, msg)
                    errors["base"] = "subs_invalid"

        return self.async_show_form(
            step_id="manual_subs",
            description_placeholders=self._placeholders(),
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SUBS_ID): str,
                    vol.Required(CONF_SUBS_CODE): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    #  re-authentication flow (triggered by ConfigEntryAuthFailed)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Start re-auth when the stored tokens are permanently invalid."""
        self._entry = self._get_reauth_entry()
        self._reauth_mode = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """重新授权：已知道 clientId 就直接给二维码，否则先让用户选分公司。"""
        self._reauth_mode = True
        current_branch = (
            self._entry.data.get(CONF_ORG_NAME) or self._entry.data.get(CONF_ORG_CODE) or "未记录"
        )
        if user_input is not None:
            manual = str(user_input.get(CONF_CLIENT_ID, "")).strip()
            org_code = str(user_input.get(CONF_ORG_CODE, "")).strip()
            client_id = manual or build_client_id(org_code)
            if not client_id:
                self._errors = {"base": "client_id_required"}
            elif await self._prepare_oauth(client_id, org_code):
                return await self.async_step_reauth_token()

            errors = self._errors
            self._errors = {}
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={
                    "branch_count": str(len(BRANCH_OPTIONS)),
                    "current_branch": current_branch,
                },
                data_schema=_branch_schema(org_code or None),
                errors=errors,
            )

        known = self._known_client_id()
        if known and await self._prepare_oauth(known, self._entry.data.get(CONF_ORG_CODE, "")):
            # 直接进入粘贴 authCode 的表单（二维码在那个表单的说明里）
            return await self.async_step_reauth_token()

        errors = self._errors
        self._errors = {}
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={
                "branch_count": str(len(BRANCH_OPTIONS)),
                "current_branch": current_branch,
            },
            data_schema=_branch_schema(self._entry.data.get(CONF_ORG_CODE) if self._entry else None),
            errors=errors,
        )

    async def async_step_reauth_token(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """重新扫码后兑换新 token，并顺便核对户号是否还在账号名下。"""
        self._reauth_mode = True
        errors: dict[str, str] = {}

        if user_input is not None:
            auth_code = self._extract_auth_code(user_input[CONF_AUTH_CODE])

            session = async_get_clientsession(self.hass, verify_ssl=False)
            api = TownGasApi(session, client_id=self._client_id)
            data: dict[str, Any] = {}
            try:
                data = await api.exchange_token(auth_code)
            except Exception:
                _LOGGER.exception("Reauth: token exchange failed")
                errors["base"] = "exchange_failed"

            if data.get("access_token"):
                self._pending = {
                    CONF_ACCESS_TOKEN: api.access_token,
                    CONF_REFRESH_TOKEN: api.refresh_token,
                    CONF_TOKEN_EXPIRES_IN: api.token_expires_in,
                    CONF_TOKEN_CREATE_TIME: api.token_create_time,
                }
                old_subs_id = self._entry.data.get(CONF_SUBS_ID, "")
                old_subs_code = self._entry.data.get(CONF_SUBS_CODE, "")

                await self._identify_account(api)

                if not self._account_subs:
                    # 读不到户号列表也不该卡住重新授权：原样写回令牌
                    return self.async_update_reload_and_abort(
                        self._entry,
                        data={**self._entry.data, **self._pending},
                        reason="reauth_successful",
                    )

                item = self._subs_by_id(old_subs_id)
                if item is not None:
                    return self._finish_with_subs(
                        old_subs_id, item.get("subs_code") or old_subs_code
                    )

                if len(self._account_subs) == 1:
                    only = self._account_subs[0]
                    _LOGGER.warning(
                        "原户号 %s 不在该账号名下，自动切换为 %s",
                        old_subs_id, only["subs_id"],
                    )
                    return self._finish_with_subs(only["subs_id"], only.get("subs_code", ""))

                # 多个候选，且原户号不在其中 → 让用户选
                return await self.async_step_pick_subs()

            if not errors:
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth_token",
            description_placeholders=self._placeholders(),
            data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
            errors=errors,
        )


class TownGasOptionsFlow(OptionsFlow):
    """Options flow — 刷新间隔与默认气价。"""

    def __init__(self, config_entry: Any) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    CONF_TOKEN_REFRESH_INTERVAL: user_input[CONF_TOKEN_REFRESH_INTERVAL],
                    CONF_DEFAULT_PRICE: user_input[CONF_DEFAULT_PRICE],
                },
            )

        current_scan = self._entry.options.get(
            CONF_SCAN_INTERVAL,
            self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        current_token = self._entry.options.get(
            CONF_TOKEN_REFRESH_INTERVAL,
            self._entry.data.get(CONF_TOKEN_REFRESH_INTERVAL, DEFAULT_TOKEN_REFRESH_INTERVAL),
        )
        current_price = self._entry.options.get(CONF_DEFAULT_PRICE, DEFAULT_PRICE)
        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "branch": self._entry.data.get(CONF_ORG_NAME)
                or self._entry.data.get(CONF_ORG_CODE)
                or "未知（可由「重新授权」重新选择）",
                "client_id": self._entry.data.get(CONF_CLIENT_ID) or "未记录",
                "subs_code": self._entry.data.get(CONF_SUBS_CODE) or "未记录",
            },
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current_scan): vol.All(
                        int, vol.Range(min=60, max=86400)
                    ),
                    vol.Optional(CONF_TOKEN_REFRESH_INTERVAL, default=current_token): vol.All(
                        int, vol.Range(min=300, max=7140)
                    ),
                    vol.Optional(CONF_DEFAULT_PRICE, default=current_price): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=99)
                    ),
                }
            ),
        )
