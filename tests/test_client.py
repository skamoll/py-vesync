import hashlib
import json

import httpx
import pytest

from py_vesync import (
    AirFryerMonitor,
    AsyncVeSyncClient,
    CookSessionTracker,
    VeSyncAPIError,
    VeSyncAuthError,
    VeSyncClient,
    is_airfryer_running,
    normalize_program_key,
)


def test_login_cross_region_and_devices():
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append((str(request.url), body))
        url = str(request.url)
        if url.endswith("/authByPWDOrOTM"):
            assert body["password"] == hashlib.md5(b"secret").hexdigest()
            return httpx.Response(200, json={"code": 0, "result": {"accountID": "123", "authorizeCode": "code1"}})
        if "smartapi.vesync.eu" in url and url.endswith("/loginByAuthorizeCode4Vesync"):
            return httpx.Response(200, json={"code": -11260022, "msg": "cross region", "result": {"currentRegion": "US", "bizToken": "biz1"}})
        if "smartapi.vesync.com" in url and url.endswith("/loginByAuthorizeCode4Vesync"):
            assert body["bizToken"] == "biz1"
            return httpx.Response(200, json={"code": 0, "result": {"token": "tok1", "accountID": "123"}})
        if url.endswith("/getHomeList"):
            return httpx.Response(200, json={"code": 0, "result": {"homeList": [{"homeId": 42}]}})
        if url.endswith("/getHomeInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "roomInfoList": [
                            {
                                "roomName": "Cocina",
                                "deviceList": [
                                    {
                                        "cid": "cid1",
                                        "deviceName": "AirFryer",
                                        "deviceType": "CAF-P583S-KEU",
                                        "configModule": "VS_WFON_AFR_CAF-P583S-KEU_ONL_EU",
                                        "connectionStatus": "online",
                                        "deviceStatus": "off",
                                        "deviceRegion": "EU",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if url.endswith("/bypassV2"):
            return httpx.Response(200, json={"code": 0, "result": {"code": 0, "result": {"cookStatus": "standby", "currentTemp": 24}}})
        raise AssertionError(f"Unexpected URL {url}")

    transport = httpx.MockTransport(handler)
    with VeSyncClient(region="EU", transport=transport) as client:
        client.login("user@example.com", "secret")
        assert client.region == "US"
        assert client.token == "tok1"
        devs = client.get_devices()
        assert len(devs) == 1
        assert devs[0].name == "AirFryer"
        st = client.get_airfryer_status(devs[0].cid, devs[0].config_module)
        assert st["cookStatus"] == "standby"


def test_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -11000, "msg": "bad credentials"})

    with VeSyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VeSyncAuthError):
            client.login("a@b.com", "wrong")


@pytest.mark.anyio
async def test_async_client_and_session_restore():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/authByPWDOrOTM"):
            return httpx.Response(200, json={"code": 0, "result": {"accountID": "acc1", "authorizeCode": "code1"}})
        if url.endswith("/loginByAuthorizeCode4Vesync"):
            return httpx.Response(200, json={"code": 0, "result": {"token": "tok_async", "accountID": "acc1"}})
        if url.endswith("/bypassV2"):
            return httpx.Response(200, json={"code": 0, "result": {"code": 0, "result": {"cookStatus": "standby", "currentTemp": 25}}})
        raise AssertionError(url)

    async with AsyncVeSyncClient(region="EU", transport=httpx.MockTransport(handler)) as client:
        await client.login("u@e.com", "pw")
        sess = client.export_session()
        assert sess["token"] == "tok_async"

    async with AsyncVeSyncClient(transport=httpx.MockTransport(handler)) as client2:
        client2.restore_session(sess)
        st = await client2.get_airfryer_status("cid1", "mod1")
        assert st["currentTemp"] == 25


def test_cook_session_tracker_elapsed():
    now = [50.0]
    tracker = CookSessionTracker("cid1", clock=lambda: now[0])
    assert tracker.current_elapsed_seconds == 0.0
    running, interval, done = tracker.update({"cookStatus": "cooking", "currentTemp": 190, "totalTimeRemaining": 200})
    assert running is True and interval == 5.0 and done is None
    now[0] = 62.5
    assert tracker.current_elapsed_seconds == 12.5


def test_normalize_program_key_and_devices_full():
    assert normalize_program_key("French fries") == "french_fries"
    assert normalize_program_key("AirFry") == "air_fry"
    assert normalize_program_key("Roast") == "roast"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/getHomeList"):
            return httpx.Response(200, json={"code": 0, "result": {"homeList": [{"homeId": 1}]}})
        if url.endswith("/getHomeInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "roomInfoList": [
                            {
                                "roomName": "Cocina",
                                "deviceList": [
                                    {
                                        "cid": "cid1",
                                        "deviceName": "AirFryer",
                                        "deviceType": "CAF-P583S-KEU",
                                        "configModule": "MOD1",
                                        "connectionStatus": "online",
                                        "deviceStatus": "off",
                                        "deviceRegion": "EU",
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if url.endswith("/configurationsV2"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "deviceImg": "https://img/a.png",
                        "defaultDeviceImg": "https://img/b.png",
                        "currentFirmVersion": "2.1.07",
                        "latestFirmVersion": "2.1.08",
                        "wifiName": "HOME",
                        "rssi": -71,
                        "macID": "aa:bb:cc",
                    },
                },
            )
        raise AssertionError(url)

    with VeSyncClient(transport=httpx.MockTransport(handler)) as client:
        client.token = "tok"
        client.account_id = "acc"
        devs = client.get_devices_full()
        assert len(devs) == 1
        assert devs[0].image_url == "https://img/a.png"
        assert devs[0].current_firmware == "2.1.07"
        assert devs[0].latest_firmware == "2.1.08"
        assert devs[0].rssi == -71
        assert devs[0].wifi_name == "HOME"


def test_is_airfryer_running_and_history_pagination():
    assert is_airfryer_running({"cookStatus": "standby", "totalTimeRemaining": 0}) is False
    assert is_airfryer_running({"cookStatus": "cooking", "totalTimeRemaining": 300}) is True
    assert is_airfryer_running({"cookStatus": "standby", "preheatLastTime": 120}) is True

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("index") == "":
            return httpx.Response(200, json={"code": 0, "result": {"index": "cur2", "cookHistoryList": [{"recipeName": "Roast"}]}})
        return httpx.Response(200, json={"code": 0, "result": {"index": "", "cookHistoryList": [{"recipeName": "Frozen"}]}})

    with VeSyncClient(transport=httpx.MockTransport(handler)) as client:
        client.token = "tok"
        client.account_id = "acc"
        items = client.get_all_cooking_history("cid1", "mod1", page_size=1)
        assert [x["recipeName"] for x in items] == ["Roast", "Frozen"]


def test_adaptive_monitor_intervals_and_session():
    states = iter([
        {"cookStatus": "standby", "currentTemp": 24, "totalTimeRemaining": 0},
        {"cookStatus": "cooking", "cookMode": "Roast", "currentTemp": 180, "totalTimeRemaining": 300, "tempUnit": "c"},
        {"cookStatus": "cooking", "cookMode": "Roast", "currentTemp": 205, "totalTimeRemaining": 295, "tempUnit": "c"},
        {"cookStatus": "standby", "currentTemp": 190, "totalTimeRemaining": 0, "tempUnit": "c"},
    ])
    now = [100.0]

    def fake_clock() -> float:
        return now[0]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/bypassV2"):
            return httpx.Response(200, json={"code": 0, "result": {"code": 0, "result": next(states)}})
        if url.endswith("/getCookingHistoryV2"):
            return httpx.Response(200, json={"code": 0, "result": {"cookHistoryList": [{"recipeName": "Roast", "workParam": {"workTime": 360, "workTemp": 205}}]}})
        raise AssertionError(url)

    with VeSyncClient(transport=httpx.MockTransport(handler)) as client:
        client.token = "tok"
        client.account_id = "acc"
        mon = AirFryerMonitor(client, "cid1", "mod1", standby_interval=10.0, active_interval=5.0, clock=fake_clock)

        _, interval, done = mon.poll_once()
        assert interval == 10.0 and done is None

        now[0] = 110.0
        _, interval, done = mon.poll_once()
        assert interval == 5.0 and done is None and mon.active_session is not None

        now[0] = 115.0
        _, interval, done = mon.poll_once()
        assert interval == 5.0 and done is None and mon.active_session.peak_temp == 205

        now[0] = 120.0
        _, interval, done = mon.poll_once()
        assert interval == 10.0 and done is not None
        assert done.actual_seconds == 10.0
        assert done.program_name == "Roast"
        assert done.programmed_seconds == 360
