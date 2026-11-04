"""TownGas VCC API client — async, no external deps beyond aiohttp.

与上游唯一区别：clientId 由调用方传入（按分公司），不再写死。
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import time
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .const import (
    API_PATH,
    CLIENT_ID_PREFIX,
    DEFAULT_CLIENT_ID,
    DEFAULT_HOST,
    OAUTH_PATH,
    SIGN_SALT,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_TOKEN_EXPIRY_BUFFER_SECS = 60  # refresh proactively this many seconds before expiry


class ClientIdMissing(Exception):
    """Raised when no OAuth clientId is available for building the auth URL."""


# ----------------------------------------------------------------------
#  Response helpers — 容错解析（港华不同分公司的返回结构略有差异）
# ----------------------------------------------------------------------

# 依次尝试的「当前登录账号下的户号列表」接口；命中一个即停止。
ACCOUNT_PATHS = (
    "/usersubs/getLoginUserInfo",
    "/usersubs/getSubsList",
    "/usersubs/getUserSubs",
)

_SUBS_ID_KEYS = ("subsid", "subsno", "subs_id")
_SUBS_CODE_KEYS = ("subscode", "custcode", "custno", "subs_code")
_SUBS_NAME_KEYS = ("subsname", "custname", "username", "subs_name")
_SUBS_ADDR_KEYS = ("subsaddr", "address", "addr")

# 10001 = 港华网关的通用失败码（页面提示「系统正在升级中......」）
# 既可能是登录态失效，也可能是「这个户号不属于当前登录的分公司」。
ERR_TOKEN_INVALID = "10001"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _norm_dict(d: dict) -> dict[str, Any]:
    """键名归一化：去下划线 + 转小写，便于跨分公司容错。"""
    return {str(k).replace("_", "").lower(): v for k, v in d.items()}


def _walk_dicts(node: Any):
    """深度遍历所有 dict（含嵌套 list）。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def _first_value(norm: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = norm.get(key)
        if value not in (None, "", 0):
            return str(value).strip()
    return ""


def extract_subs(payload: Any) -> list[dict[str, str]]:
    """从任意层级的返回体里抠出户号列表：[{subs_id, subs_code, name, addr}]。"""
    merged: dict[str, dict[str, str]] = {}
    for item in _walk_dicts(payload):
        norm = _norm_dict(item)
        subs_id = _first_value(norm, _SUBS_ID_KEYS)
        if not subs_id or len(subs_id) < 6:
            continue
        entry = merged.setdefault(
            subs_id,
            {"subs_id": subs_id, "subs_code": "", "name": "", "addr": ""},
        )
        for field, keys in (
            ("subs_code", _SUBS_CODE_KEYS),
            ("name", _SUBS_NAME_KEYS),
            ("addr", _SUBS_ADDR_KEYS),
        ):
            if not entry[field]:
                entry[field] = _first_value(norm, keys)
    return [v for v in merged.values() if v["subs_code"] or v["name"] or v["addr"]]


def result_code(payload: Any) -> str:
    """提取返回体里的业务错误码（0/空 视为成功）。"""
    if not isinstance(payload, dict):
        return ""
    datas = payload.get("datas")
    if isinstance(datas, dict):
        for key in ("errorCode", "resultCode"):
            value = datas.get(key)
            if value not in (None, ""):
                return str(value)
    for key in ("resultCode", "result_code", "errorCode"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def result_message(payload: Any) -> str:
    """提取返回体里的业务错误文案。"""
    if not isinstance(payload, dict):
        return ""
    datas = payload.get("datas")
    if isinstance(datas, dict):
        for key in ("errorMsg", "resultMsg"):
            value = datas.get(key)
            if value:
                return str(value)
    for key in ("resultMsg", "result_msg", "errorMsg", "msg"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def is_ok_code(code: str) -> bool:
    return str(code or "").strip() in ("", "0", "200", "0000")


def strip_html(text: str) -> str:
    """港华的错误文案里带 <p style=...>，去掉标签便于显示。"""
    import re

    return re.sub(r"<[^>]+>", "", text or "").strip()


def _sign(params: dict[str, Any]) -> str:
    """Sort keys → concat key+value → append salt → MD5 upper."""
    keys = sorted(
        k for k, v in params.items()
        if k != "sign" and v is not None and v != ""
    )
    raw = "".join(f"{k}{params[k]}" for k in keys)
    return _md5(raw + SIGN_SALT).upper()


class TownGasApi:
    """Lightweight async API wrapper for TownGas VCC."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_create_time: float = 0,
        token_expires_in: int = 7200,
        client_id: str | None = None,
    ) -> None:
        self._session = session
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_create_time = _as_float(token_create_time)
        self.token_expires_in = _as_int(token_expires_in, 7200)
        self.client_id = client_id or DEFAULT_CLIENT_ID

    @property
    def bearer_valid(self) -> bool:
        if not self.access_token or not self.token_create_time:
            return False
        return self.token_create_time + self.token_expires_in - _TOKEN_EXPIRY_BUFFER_SECS > time.time()

    @property
    def bearer_remain(self) -> int:
        if not self.bearer_valid:
            return 0
        return int(self.token_create_time + self.token_expires_in - time.time())

    # ------------------------------------------------------------------
    #  OAuth helpers
    # ------------------------------------------------------------------

    async def get_oauth_url(self) -> str:
        """Return the WeChat OAuth redirect URL for scanning.

        Returns "" when the branch rejects the clientId (港华 returns a JSON
        error body instead of a 302 to open.weixin.qq.com).
        """
        client_id = self.client_id
        if not client_id:
            raise ClientIdMissing(
                "未配置 clientId：请先选择分公司，或手动填写微信 OAuth 的 clientId"
            )
        redirect = quote(f"https://{DEFAULT_HOST}/h5-gas/", safe="")
        url = (
            f"https://{DEFAULT_HOST}{OAUTH_PATH}/oauth/authorize2/union"
            f"?clientid={client_id}&redirectUri={redirect}"
        )
        async with self._session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
            ssl=_SSL_CTX,
        ) as resp:
            location = resp.headers.get("Location", "")
            if not location:
                body = await resp.text()
                _LOGGER.warning(
                    "clientId=%s 未获得微信跳转（status=%s, body=%s）",
                    client_id, resp.status, (body or "")[:200],
                )
            return location

    async def exchange_token(self, auth_code: str) -> dict[str, Any]:
        """Exchange authCode for access_token + refresh_token."""
        url = (
            f"https://{DEFAULT_HOST}{OAUTH_PATH}/oauth/authorize2/accessToekn"
            f"?authCode={auth_code}"
        )
        async with self._session.post(
            url, headers={"User-Agent": USER_AGENT}, ssl=_SSL_CTX,
        ) as resp:
            data = await resp.json(content_type=None)

        _LOGGER.debug("exchange_token raw response keys=%s data=%s", list(data.keys()), data)
        if data.get("access_token"):
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self.token_expires_in = _as_int(data.get("expires_in", 7200), 7200)
            self.token_create_time = time.time()
        return data

    async def refresh_access_token(self) -> bool:
        """Use refresh_token to obtain a new access_token."""
        if not self.refresh_token:
            return False

        ts = int(time.time() * 1000)
        sign = _sign({"timestamp": ts, "refreshToken": self.refresh_token})
        url = (
            f"https://{DEFAULT_HOST}{OAUTH_PATH}/oauth/authorize2/refreshToken"
            f"?timestamp={ts}"
            f"&refreshToken={self.refresh_token}"
            f"&sign={sign}"
        )
        try:
            async with self._session.post(
                url, headers={"User-Agent": USER_AGENT}, ssl=_SSL_CTX,
            ) as resp:
                data = await resp.json(content_type=None)

            _LOGGER.debug("refresh_access_token raw response keys=%s data=%s", list(data.keys()), data)
            if data.get("access_token"):
                self.access_token = data["access_token"]
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.token_expires_in = _as_int(data.get("expires_in", 7200), 7200)
                self.token_create_time = time.time()
                _LOGGER.debug("Token refreshed, expires_in=%s", self.token_expires_in)
                return True

            # API returned an error code → definitive auth failure (not transient)
            if data.get("resultCode") or data.get("result_code"):
                raise AuthError(f"refresh_token 已失效: {data}")

            _LOGGER.warning("Token refresh unexpected response: %s", data)
            return False
        except AuthError:
            raise
        except Exception:
            _LOGGER.exception("Token refresh error")
            return False

    async def ensure_token(self) -> bool:
        """Make sure we have a valid bearer token, refreshing if needed."""
        if self.bearer_valid:
            return True
        return await self.refresh_access_token()

    # ------------------------------------------------------------------
    #  Business API
    # ------------------------------------------------------------------

    async def _cbs_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Signed GET on /nv1/vcc-cbs/*."""
        if not await self.ensure_token():
            raise AuthError("无可用 access_token，且 refresh 失败")

        def _build() -> tuple[str, dict[str, str]]:
            """Build a freshly-signed URL + headers using the current token."""
            ts = int(time.time() * 1000)
            all_params: dict[str, Any] = {**(params or {}), "timestamp": ts}
            all_params["sign"] = _sign(all_params)
            built_url = f"https://{DEFAULT_HOST}{API_PATH}{path}?{urlencode(all_params)}"
            built_headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self.access_token}",
            }
            return built_url, built_headers

        url, headers = _build()
        async with self._session.get(url, headers=headers, ssl=_SSL_CTX) as resp:
            body = await resp.text()
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            _LOGGER.debug(
                "CBS GET %s status=%s resp_keys=%s",
                path, resp.status, list(data.keys()) if isinstance(data, dict) else "non-dict",
            )
            if resp.status == 401:
                _LOGGER.info("CBS 401，尝试 refresh token 后重试")
                ok = await self.refresh_access_token()
                if not ok:
                    raise AuthError("access_token 过期且 refresh 失败")
                # Rebuild URL + headers with fresh timestamp/sign and new token
                retry_url, retry_headers = _build()
                async with self._session.get(retry_url, headers=retry_headers, ssl=_SSL_CTX) as retry:
                    return await retry.json(content_type=None)
            if resp.status != 200:
                _LOGGER.warning(
                    "CBS GET %s 非 200: status=%s body=%s",
                    path, resp.status, (body or "")[:400],
                )
            return data

    async def pre_check(self, subs_id: str) -> dict[str, Any]:
        """GET /charge/preCheck — returns currReading etc."""
        result = await self._cbs_get("/charge/preCheck", {"subsId": subs_id})
        datas = result.get("datas") if isinstance(result, dict) else {}
        lst = datas.get("readingRptList") if isinstance(datas, dict) else None
        curr = None
        if lst and isinstance(lst, list) and lst:
            first = lst[0]
            if isinstance(first, dict):
                curr = first.get("currReading")
        err_code = datas.get("errorCode") if isinstance(datas, dict) else None
        err_msg = datas.get("errorMsg") if isinstance(datas, dict) else None
        code = result_code(result)
        _LOGGER.debug(
            "preCheck subs_id=%s currReading=%s code=%s errorCode=%s errorMsg=%s",
            subs_id, curr, code, err_code, err_msg,
        )
        if not is_ok_code(code):
            _LOGGER.warning(
                "preCheck 失败 code=%s msg=%s body=%s",
                code, strip_html(result_message(result)), str(result)[:500],
            )
        return result

    async def get_login_user_info(self) -> dict[str, Any]:
        """GET /usersubs/getLoginUserInfo."""
        return await self._cbs_get("/usersubs/getLoginUserInfo")

    async def fetch_account(self) -> dict[str, Any]:
        """用当前 token 问「我是谁、我名下有哪些户号」。

        返回 {"subs": [...], "code": "10001"/"", "msg": "...", "path": "命中的接口"}。
        code 非空且不是成功码时，说明连账号信息都拿不到（通常是登录态失效或
        令牌与分公司不匹配）；subs 非空则说明令牌可用，户号可以自动识别。
        """
        first_code = ""
        first_msg = ""
        for path in ACCOUNT_PATHS:
            try:
                result = await self._cbs_get(path)
            except AuthError:
                raise
            except Exception:  # noqa: BLE001 — 探测接口失败不应中断流程
                _LOGGER.debug("账号信息接口 %s 请求异常", path, exc_info=True)
                continue

            code = result_code(result)
            msg = strip_html(result_message(result))
            if not is_ok_code(code):
                _LOGGER.debug("账号信息接口 %s 返回错误 %s: %s", path, code, msg)
                if not first_code:
                    first_code, first_msg = code, msg
                continue

            found = extract_subs(result)
            _LOGGER.debug("账号信息接口 %s 识别到户号 %s 个", path, len(found))
            if found:
                return {"subs": found, "code": "", "msg": "", "path": path}

        return {"subs": [], "code": first_code, "msg": first_msg, "path": ""}

    async def verify_subs(self, subs_id: str) -> tuple[bool, str, str]:
        """试抓一次户号数据，返回 (是否可用, 错误码, 错误文案)。"""
        try:
            result = await self.pre_check(subs_id)
        except AuthError:
            raise
        except Exception as err:  # noqa: BLE001
            return False, "", str(err)
        code = result_code(result)
        msg = strip_html(result_message(result))
        datas = result.get("datas") if isinstance(result, dict) else {}
        lst = datas.get("readingRptList") if isinstance(datas, dict) else None
        has_reading = bool(
            lst and isinstance(lst, list) and isinstance(lst[0], dict)
            and lst[0].get("currReading") is not None
        )
        if not is_ok_code(code):
            return False, code, msg
        # 没报错但也没有读数 → 视为该户号在此登录态下不可用
        return has_reading, "" if has_reading else "empty", msg


class AuthError(Exception):
    """Raised when authentication is not possible."""
