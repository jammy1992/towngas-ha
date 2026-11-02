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


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


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
        self.token_create_time = token_create_time
        self.token_expires_in = token_expires_in
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
            self.token_expires_in = data.get("expires_in", 7200)
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
                self.token_expires_in = data.get("expires_in", 7200)
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
        _LOGGER.debug(
            "preCheck subs_id=%s currReading=%s errorCode=%s errorMsg=%s",
            subs_id, curr, err_code, err_msg,
        )
        return result

    async def get_login_user_info(self) -> dict[str, Any]:
        """GET /usersubs/getLoginUserInfo."""
        return await self._cbs_get("/usersubs/getLoginUserInfo")


class AuthError(Exception):
    """Raised when authentication is not possible."""
