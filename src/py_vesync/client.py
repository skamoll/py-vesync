"""VeSync Cloud API v2 sync and async clients based on observed mobile app traffic."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

REGION_ENDPOINTS: dict[str, tuple[str, str]] = {
    "EU": ("https://accountapi.vesync.eu", "https://smartapi.vesync.eu"),
    "US": ("https://accountapi.vesync.com", "https://smartapi.vesync.com"),
}

APP_ID = "eldodkfj"
CLIENT_TYPE = "vesyncApp"
CLIENT_VERSION = "VeSync 6.0.10"
USER_AGENT = "VeSync/VeSync 6.0.10(Python;py-vesync)"
STOPPED_COOK_STATUSES = frozenset({"standby", "cookend", "cookstop", "off", "end", "finished", "idle", ""})
AUTH_ERROR_CODES = frozenset({-11000, -11200, -11201, -11202, -11212, -11260})


def normalize_program_key(name: str | None) -> str | None:
    """Normalize a VeSync program name (e.g. 'French fries') to a HA translation key ('french_fries')."""
    if not name:
        return None
    cleaned = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned).strip("_").lower()
    return cleaned or None


def is_airfryer_running(status: dict[str, Any]) -> bool:
    """Return True if an air fryer status payload indicates active cooking or preheating."""
    cook_status = str(status.get("cookStatus") or "").strip().lower()
    remaining = int(status.get("totalTimeRemaining") or 0)
    preheat_remaining = int(status.get("preheatLastTime") or 0)
    return (cook_status not in STOPPED_COOK_STATUSES) or remaining > 0 or preheat_remaining > 0


class VeSyncAPIError(RuntimeError):
    """Raised when the VeSync cloud API returns a non-zero business code or HTTP error."""

    def __init__(self, code: int | str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(f"VeSync API error {code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


class VeSyncAuthError(VeSyncAPIError):
    """Raised on authentication failure or expired/invalid session tokens."""


@dataclass(slots=True)
class VeSyncDevice:
    """Representation of a VeSync device with metadata from getHomeInfo and configurationsV2."""

    cid: str
    name: str
    device_type: str
    config_module: str
    connection_status: str
    device_status: str
    device_region: str
    room_name: str
    image_url: str | None = None
    default_image_url: str | None = None
    device_category: str | None = None
    connection_type: str | None = None
    current_firmware: str | None = None
    latest_firmware: str | None = None
    wifi_name: str | None = None
    rssi: int | None = None
    is_owner: bool | None = None
    mac_id: str | None = None
    uuid: str | None = None
    config: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Enrich device fields from a configurationsV2 payload."""
        self.config = cfg
        self.image_url = cfg.get("deviceImg") or self.image_url
        self.default_image_url = cfg.get("defaultDeviceImg") or self.default_image_url
        self.current_firmware = cfg.get("currentFirmVersion") or self.current_firmware
        self.latest_firmware = cfg.get("latestFirmVersion") or self.latest_firmware
        self.wifi_name = cfg.get("wifiName") or self.wifi_name
        self.rssi = cfg.get("rssi") if cfg.get("rssi") is not None else self.rssi
        self.mac_id = cfg.get("macID") or self.mac_id
        self.connection_type = cfg.get("connectionType") or self.connection_type
        if cfg.get("ownerShip") is not None:
            self.is_owner = bool(cfg["ownerShip"])


class _VeSyncProtocolMixin:
    """Shared request-building and response-parsing helpers for sync and async clients."""

    def _init_protocol(
        self,
        *,
        region: str = "EU",
        country_code: str = "ES",
        time_zone: str = "Europe/Madrid",
        accept_language: str = "es",
        terminal_id: str | None = None,
    ) -> None:
        region_key = region.upper()
        if region_key not in REGION_ENDPOINTS:
            raise ValueError(f"Unsupported region {region!r}; expected one of {tuple(REGION_ENDPOINTS)}")
        self.region = region_key
        self.account_base, self.smart_base = REGION_ENDPOINTS[region_key]
        self.country_code = country_code
        self.time_zone = time_zone
        self.accept_language = accept_language
        self.terminal_id = terminal_id or uuid.uuid4().hex + "0"
        self.token: str = ""
        self.account_id: str = ""
        self._trace_seq = 0

    def export_session(self) -> dict[str, str]:
        """Export session state so consumers like Home Assistant can persist it."""
        return {
            "token": self.token,
            "account_id": self.account_id,
            "region": self.region,
            "terminal_id": self.terminal_id,
        }

    def restore_session(self, session: dict[str, str]) -> None:
        """Restore a previously exported session without re-logging in."""
        region = str(session.get("region") or self.region).upper()
        if region in REGION_ENDPOINTS:
            self.region = region
            self.account_base, self.smart_base = REGION_ENDPOINTS[region]
        self.token = str(session.get("token") or "")
        self.account_id = str(session.get("account_id") or "")
        if session.get("terminal_id"):
            self.terminal_id = str(session["terminal_id"])

    def _next_trace_id(self) -> str:
        self._trace_seq += 1
        return f"APP{int(time.time() * 1000)}-{self._trace_seq:05d}"

    def _base_body(self, method: str) -> dict[str, Any]:
        return {
            "acceptLanguage": self.accept_language,
            "accountID": self.account_id,
            "clientInfo": "py-vesync",
            "clientType": CLIENT_TYPE,
            "clientVersion": CLIENT_VERSION,
            "debugMode": False,
            "method": method,
            "osInfo": "Python",
            "terminalId": self.terminal_id,
            "timeZone": self.time_zone,
            "token": self.token,
            "traceId": self._next_trace_id(),
            "userCountryCode": self.country_code,
        }

    def _cloud_body(self, method: str) -> dict[str, Any]:
        return {
            "acceptLanguage": self.accept_language,
            "accountID": self.account_id,
            "appVersion": CLIENT_VERSION,
            "debugMode": False,
            "method": method,
            "phoneBrand": "py-vesync",
            "phoneOS": "Python",
            "timeZone": self.time_zone,
            "token": self.token,
            "traceId": str(int(time.time() * 1000)),
            "userCountryCode": self.country_code,
        }

    @staticmethod
    def _check_response(resp: httpx.Response, *, allow_codes: set[int] | None = None, auth_context: bool = False) -> dict[str, Any]:
        resp.raise_for_status()
        data = resp.json()
        code = data.get("code")
        if code != 0 and (allow_codes is None or code not in allow_codes):
            msg = str(data.get("msg", "unknown error"))
            if auth_context or code in AUTH_ERROR_CODES or "token" in msg.lower() or "password" in msg.lower():
                raise VeSyncAuthError(code, msg, data)
            raise VeSyncAPIError(code, msg, data)
        return data

    def _build_auth_payload(self, email: str, password: str) -> tuple[str, dict[str, Any]]:
        pwd_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        return (
            f"{self.account_base}/globalPlatform/api/accountAuth/v1/authByPWDOrOTM",
            {
                **self._base_body("authByPWDOrOTM"),
                "sourceAppID": APP_ID,
                "appID": APP_ID,
                "authProtocolType": "generic",
                "email": email,
                "password": pwd_md5,
            },
        )

    def _build_authorize_exchange(self, auth_resp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        auth_result = auth_resp.get("result") or {}
        authorize_code = auth_result["authorizeCode"]
        self.account_id = str(auth_result.get("accountID") or "")
        return (
            f"{self.smart_base}/user/api/accountManage/v1/loginByAuthorizeCode4Vesync",
            {
                **self._base_body("loginByAuthorizeCode4Vesync"),
                "authorizeCode": authorize_code,
                "emailSubscriptions": False,
            },
        )

    def _build_cross_region_retry(self, login_resp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        res = login_resp.get("result") or {}
        target_region = str(res.get("currentRegion") or "US").upper()
        if target_region in REGION_ENDPOINTS:
            _, self.smart_base = REGION_ENDPOINTS[target_region]
            self.region = target_region
        return (
            f"{self.smart_base}/user/api/accountManage/v1/loginByAuthorizeCode4Vesync",
            {
                **self._base_body("loginByAuthorizeCode4Vesync"),
                "bizToken": res["bizToken"],
                "emailSubscriptions": False,
                "regionChange": "",
            },
        )

    def _finalize_login(self, login_resp: dict[str, Any]) -> dict[str, Any]:
        result = login_resp.get("result") or {}
        self.token = str(result["token"])
        self.account_id = str(result.get("accountID") or self.account_id)
        return result

    @staticmethod
    def _parse_rooms_devices(rooms_by_home: list[list[dict[str, Any]]]) -> list[VeSyncDevice]:
        devices: list[VeSyncDevice] = []
        for rooms in rooms_by_home:
            for room in rooms:
                room_name = str(room.get("roomName") or "")
                for dev in room.get("deviceList") or []:
                    devices.append(
                        VeSyncDevice(
                            cid=str(dev["cid"]),
                            name=str(dev.get("deviceName") or ""),
                            device_type=str(dev.get("deviceType") or ""),
                            config_module=str(dev.get("configModule") or ""),
                            connection_status=str(dev.get("connectionStatus") or ""),
                            device_status=str(dev.get("deviceStatus") or ""),
                            device_region=str(dev.get("deviceRegion") or ""),
                            room_name=room_name,
                            image_url=dev.get("deviceImg"),
                            device_category=dev.get("type"),
                            connection_type=dev.get("connectionType"),
                            current_firmware=dev.get("currentFirmVersion"),
                            is_owner=dev.get("isOwner"),
                            mac_id=dev.get("macID"),
                            uuid=dev.get("uuid"),
                            raw=dev,
                        )
                    )
        return devices

    def _bypass_v2_body(self, cid: str, config_module: str, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            **self._cloud_body("bypassV2"),
            "cid": cid,
            "deviceId": cid,
            "configModule": config_module,
            "configModel": config_module,
            "subDeviceNo": 0,
            "subDeviceType": "",
            "payload": {
                "data": data or {},
                "method": method,
                "subDeviceType": "",
                "subDeviceNo": 0,
                "source": "APP",
            },
        }

    @staticmethod
    def _unwrap_bypass_v2(resp: dict[str, Any]) -> dict[str, Any]:
        inner = resp.get("result") or {}
        if isinstance(inner, dict) and inner.get("code", 0) != 0:
            raise VeSyncAPIError(inner["code"], str(inner.get("msg", "bypass error")), inner)
        return dict(inner.get("result") or {}) if isinstance(inner, dict) else {}


class VeSyncClient(_VeSyncProtocolMixin):
    """Lightweight synchronous client for VeSync Cloud API v2."""

    def __init__(
        self,
        *,
        region: str = "EU",
        country_code: str = "ES",
        time_zone: str = "Europe/Madrid",
        accept_language: str = "es",
        terminal_id: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._init_protocol(
            region=region,
            country_code=country_code,
            time_zone=time_zone,
            accept_language=accept_language,
            terminal_id=terminal_id,
        )
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json; charset=UTF-8",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> VeSyncClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _post(
        self,
        url: str,
        body: dict[str, Any],
        *,
        allow_codes: set[int] | None = None,
        auth_context: bool = False,
    ) -> dict[str, Any]:
        resp = self._http.post(url, json=body)
        return self._check_response(resp, allow_codes=allow_codes, auth_context=auth_context)

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate against VeSync globalPlatform and exchange authorizeCode for session token."""
        url, body = self._build_auth_payload(email, password)
        auth_resp = self._post(url, body, auth_context=True)
        url, body = self._build_authorize_exchange(auth_resp)
        login_resp = self._post(url, body, allow_codes={-11260022}, auth_context=True)
        if login_resp.get("code") == -11260022:
            url, body = self._build_cross_region_retry(login_resp)
            login_resp = self._post(url, body, auth_context=True)
        return self._finalize_login(login_resp)

    def get_homes(self) -> list[dict[str, Any]]:
        data = self._post(f"{self.smart_base}/cloud/v1/homeManaged/getHomeList", self._cloud_body("getHomeList"))
        return list((data.get("result") or {}).get("homeList") or [])

    def get_home_info(self, home_id: int) -> list[dict[str, Any]]:
        body = {**self._cloud_body("getHomeInfo"), "homeId": home_id}
        data = self._post(f"{self.smart_base}/cloud/v1/homeManaged/getHomeInfo", body)
        return list((data.get("result") or {}).get("roomInfoList") or [])

    def get_devices(self, home_id: int | None = None) -> list[VeSyncDevice]:
        home_ids = [home_id] if home_id is not None else [h["homeId"] for h in self.get_homes()]
        return self._parse_rooms_devices([self.get_home_info(hid) for hid in home_ids])

    def get_devices_full(self, home_id: int | None = None) -> list[VeSyncDevice]:
        """Return all devices enriched with configurationsV2 (firmware, image URLs, RSSI, wifi, etc.)."""
        devices = self.get_devices(home_id=home_id)
        for dev in devices:
            if dev.config_module:
                dev.apply_config(self.get_device_config(dev.cid, dev.config_module))
        return devices

    def get_device_config(self, cid: str, config_module: str) -> dict[str, Any]:
        body = {
            **self._cloud_body("configurationsV2"),
            "cid": cid,
            "deviceId": cid,
            "configModule": config_module,
            "configModel": config_module,
            "subDeviceNo": 0,
            "subDeviceType": "",
        }
        data = self._post(f"{self.smart_base}/cloud/v2/deviceManaged/configurationsV2", body)
        return dict(data.get("result") or {})

    def bypass_v2(self, cid: str, config_module: str, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._post(
            f"{self.smart_base}/cloud/v2/deviceManaged/bypassV2",
            self._bypass_v2_body(cid, config_module, method, data),
        )
        return self._unwrap_bypass_v2(resp)

    def get_airfryer_status(self, cid: str, config_module: str) -> dict[str, Any]:
        return self.bypass_v2(cid, config_module, "getAirfryerStatus")

    def get_airfryer_presets(self, cid: str, config_module: str) -> dict[str, Any]:
        return self.bypass_v2(cid, config_module, "getPresetRecipe")

    def get_cooking_history(self, cid: str, config_module: str, *, page_size: int = 10, index: str = "") -> dict[str, Any]:
        body = {
            **self._cloud_body("getCookingHistoryV2"),
            "configModel": config_module,
            "deviceCid": cid,
            "index": index,
            "pageSize": page_size,
        }
        data = self._post(f"{self.smart_base}/cloud/v2/recipe/getCookingHistoryV2", body)
        return dict(data.get("result") or {})

    def get_all_cooking_history(self, cid: str, config_module: str, *, page_size: int = 50, max_pages: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max_pages):
            page = self.get_cooking_history(cid, config_module, page_size=page_size, index=cursor)
            batch = list(page.get("cookHistoryList") or [])
            items.extend(batch)
            next_cursor = str(page.get("index") or "")
            if not batch or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return items


class AsyncVeSyncClient(_VeSyncProtocolMixin):
    """Async client for VeSync Cloud API v2, suited for Home Assistant integrations."""

    def __init__(
        self,
        *,
        region: str = "EU",
        country_code: str = "ES",
        time_zone: str = "Europe/Madrid",
        accept_language: str = "es",
        terminal_id: str | None = None,
        timeout: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._init_protocol(
            region=region,
            country_code=country_code,
            time_zone=time_zone,
            accept_language=accept_language,
            terminal_id=terminal_id,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json; charset=UTF-8",
            },
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncVeSyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _post(
        self,
        url: str,
        body: dict[str, Any],
        *,
        allow_codes: set[int] | None = None,
        auth_context: bool = False,
    ) -> dict[str, Any]:
        resp = await self._http.post(
            url,
            json=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=UTF-8"},
        )
        return self._check_response(resp, allow_codes=allow_codes, auth_context=auth_context)

    async def login(self, email: str, password: str) -> dict[str, Any]:
        url, body = self._build_auth_payload(email, password)
        auth_resp = await self._post(url, body, auth_context=True)
        url, body = self._build_authorize_exchange(auth_resp)
        login_resp = await self._post(url, body, allow_codes={-11260022}, auth_context=True)
        if login_resp.get("code") == -11260022:
            url, body = self._build_cross_region_retry(login_resp)
            login_resp = await self._post(url, body, auth_context=True)
        return self._finalize_login(login_resp)

    async def get_homes(self) -> list[dict[str, Any]]:
        data = await self._post(f"{self.smart_base}/cloud/v1/homeManaged/getHomeList", self._cloud_body("getHomeList"))
        return list((data.get("result") or {}).get("homeList") or [])

    async def get_home_info(self, home_id: int) -> list[dict[str, Any]]:
        body = {**self._cloud_body("getHomeInfo"), "homeId": home_id}
        data = await self._post(f"{self.smart_base}/cloud/v1/homeManaged/getHomeInfo", body)
        return list((data.get("result") or {}).get("roomInfoList") or [])

    async def get_devices(self, home_id: int | None = None) -> list[VeSyncDevice]:
        home_ids = [home_id] if home_id is not None else [h["homeId"] for h in await self.get_homes()]
        rooms_by_home = await asyncio.gather(*(self.get_home_info(hid) for hid in home_ids))
        return self._parse_rooms_devices(list(rooms_by_home))

    async def get_devices_full(self, home_id: int | None = None) -> list[VeSyncDevice]:
        """Return all devices enriched concurrently with configurationsV2 metadata."""
        devices = await self.get_devices(home_id=home_id)
        configurable = [dev for dev in devices if dev.config_module]
        configs = await asyncio.gather(*(self.get_device_config(d.cid, d.config_module) for d in configurable))
        for dev, cfg in zip(configurable, configs, strict=True):
            dev.apply_config(cfg)
        return devices

    async def get_device_config(self, cid: str, config_module: str) -> dict[str, Any]:
        body = {
            **self._cloud_body("configurationsV2"),
            "cid": cid,
            "deviceId": cid,
            "configModule": config_module,
            "configModel": config_module,
            "subDeviceNo": 0,
            "subDeviceType": "",
        }
        data = await self._post(f"{self.smart_base}/cloud/v2/deviceManaged/configurationsV2", body)
        return dict(data.get("result") or {})

    async def bypass_v2(self, cid: str, config_module: str, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._post(
            f"{self.smart_base}/cloud/v2/deviceManaged/bypassV2",
            self._bypass_v2_body(cid, config_module, method, data),
        )
        return self._unwrap_bypass_v2(resp)

    async def get_airfryer_status(self, cid: str, config_module: str) -> dict[str, Any]:
        return await self.bypass_v2(cid, config_module, "getAirfryerStatus")

    async def get_airfryer_presets(self, cid: str, config_module: str) -> dict[str, Any]:
        return await self.bypass_v2(cid, config_module, "getPresetRecipe")

    async def get_cooking_history(self, cid: str, config_module: str, *, page_size: int = 10, index: str = "") -> dict[str, Any]:
        body = {
            **self._cloud_body("getCookingHistoryV2"),
            "configModel": config_module,
            "deviceCid": cid,
            "index": index,
            "pageSize": page_size,
        }
        data = await self._post(f"{self.smart_base}/cloud/v2/recipe/getCookingHistoryV2", body)
        return dict(data.get("result") or {})

    async def get_all_cooking_history(self, cid: str, config_module: str, *, page_size: int = 50, max_pages: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max_pages):
            page = await self.get_cooking_history(cid, config_module, page_size=page_size, index=cursor)
            batch = list(page.get("cookHistoryList") or [])
            items.extend(batch)
            next_cursor = str(page.get("index") or "")
            if not batch or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return items
