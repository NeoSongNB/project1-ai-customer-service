# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from app import (
    START_ACTION,
    STOP_ACTION,
    build_start_request,
    create_app,
    create_open_api_invoker,
    create_runtime_scenes,
    load_runtime_config,
)

APP_ID = "012345678901234567890123"
APP_KEY = "01234567890123456789012345678901"


def scene_definitions() -> Dict[str, Any]:
    return {
        "default": {
            "SceneConfig": {"name": "Default"},
            "VoiceChat": {
                "Config": {
                    "ASRConfig": {"Provider": "volcano", "ProviderParams": {}},
                    "LLMConfig": {"Mode": "ArkV3"},
                    "TTSConfig": {"Provider": "volcano", "ProviderParams": {}},
                },
                "AgentConfig": {
                    "UserId": "voice_agent",
                    "EnableConversationStateCallback": False,
                },
            },
        },
        "alternate": {
            "SceneConfig": {"name": "Alternate"},
            "VoiceChat": {
                "Config": {
                    "ASRConfig": {"Provider": "volcano", "ProviderParams": {}},
                    "LLMConfig": {"Mode": "ArkV3"},
                    "TTSConfig": {"Provider": "volcano", "ProviderParams": {}},
                },
                "AgentConfig": {"UserId": "alternate_agent"},
            },
        },
    }


def runtime_config(**overrides: Any) -> Any:
    base = {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "business_id": None,
        "host": "127.0.0.1",
        "port": 3001,
        "env": {},
        "credential": {"accessKeyId": "ak", "secretKey": "sk"},
    }
    base.update(overrides)
    from app import RuntimeConfig

    return RuntimeConfig(**base)


def test_load_runtime_config_requires_complete_credentials():
    base = {"RTC_APP_ID": APP_ID, "RTC_APP_KEY": APP_KEY}
    env = {
        **base,
        "VOLCENGINE_ACCESS_KEY_ID": " ak ",
        "VOLCENGINE_SECRET_ACCESS_KEY": " sk ",
    }
    config = load_runtime_config(env)
    assert config.credential == {"accessKeyId": "ak", "secretKey": "sk"}
    with pytest.raises(ValueError, match="VOLCENGINE_ACCESS_KEY_ID"):
        load_runtime_config(base)
    for partial in (
        {"VOLCENGINE_ACCESS_KEY_ID": "ak"},
        {"VOLCENGINE_SECRET_ACCESS_KEY": "sk"},
    ):
        with pytest.raises(ValueError, match="必须同时配置"):
            load_runtime_config({**base, **partial})


def test_load_runtime_config_reads_optional_business_id():
    env = {
        "RTC_APP_ID": APP_ID,
        "RTC_APP_KEY": APP_KEY,
        "VOLCENGINE_ACCESS_KEY_ID": "ak",
        "VOLCENGINE_SECRET_ACCESS_KEY": "sk",
    }
    assert (
        load_runtime_config({**env, "RTC_BUSINESS_ID": "demo_business"}).business_id
        == "demo_business"
    )
    assert load_runtime_config(env).business_id is None


def test_runtime_propagates_optional_business_id():
    config = runtime_config(business_id="demo_business")
    scenes = create_runtime_scenes(scene_definitions(), config)
    assert scenes["default"].rtc["BusinessId"] == "demo_business"
    assert build_start_request(scenes["default"], config)["BusinessId"] == "demo_business"

    config_without = runtime_config()
    scenes_without = create_runtime_scenes(scene_definitions(), config_without)
    assert "BusinessId" not in build_start_request(scenes_without["default"], config_without)


def test_scene_runtime_injects_session_identities_without_mutating_scene_file():
    definitions = scene_definitions()
    scenes = create_runtime_scenes(definitions, runtime_config())
    request = build_start_request(scenes["default"], runtime_config())

    assert request["AppId"] == APP_ID
    assert request["RoomId"] == scenes["default"].rtc["RoomId"]
    assert request["TaskId"] == scenes["default"].task_id
    assert request["AgentConfig"]["UserId"] == "voice_agent"
    assert scenes["default"].scene["botName"] == "voice_agent"
    assert request["AgentConfig"]["TargetUserId"] == [scenes["default"].rtc["UserId"]]
    assert request["AgentConfig"]["EnableConversationStateCallback"] is True
    assert (
        definitions["default"]["VoiceChat"]["AgentConfig"]["EnableConversationStateCallback"]
        is False
    )
    assert "AppId" not in definitions["default"]["VoiceChat"]
    assert "TargetUserId" not in definitions["default"]["VoiceChat"]["AgentConfig"]
    assert APP_KEY not in str(scenes["default"].rtc)


def test_scene_requires_persisted_agent_user_id():
    definitions = scene_definitions()
    del definitions["default"]["VoiceChat"]["AgentConfig"]["UserId"]
    with pytest.raises(ValueError, match=r"VoiceChat\.AgentConfig\.UserId 不能为空"):
        create_runtime_scenes(definitions, runtime_config())


@pytest.mark.asyncio
async def test_openapi_invoker_signs_start_and_stop_with_rtc_api_constants():
    requests: List[Dict[str, Any]] = []
    config = load_runtime_config(
        {
            "RTC_APP_ID": APP_ID,
            "RTC_APP_KEY": APP_KEY,
            "VOLCENGINE_ACCESS_KEY_ID": "ak",
            "VOLCENGINE_SECRET_ACCESS_KEY": "sk",
        }
    )

    async def fetch_impl(url: str, options: Dict[str, Any]):
        requests.append({"url": url, "options": options})
        return {"ResponseMetadata": {}, "Result": "ok"}

    invoke = create_open_api_invoker(config, fetch_impl)
    body = {"AppId": APP_ID, "RoomId": "room", "TaskId": "task"}
    for action in (START_ACTION, STOP_ACTION):
        await invoke(action, body)
        request = requests[-1]
        parsed = urlparse(request["url"])
        query = parse_qs(parsed.query)
        assert query["Action"] == [action]
        assert query["Version"] == ["2025-06-01"]
        assert f"{parsed.scheme}://{parsed.netloc}" == "https://rtc.volcengineapi.com"
        assert request["options"]["headers"]["Host"] == "rtc.volcengineapi.com"
        auth = request["options"]["headers"]["Authorization"]
        assert auth.startswith("HMAC-SHA256 ")
        assert re.search(r"Credential=ak/\d{8}/cn-north-1/rtc/request", auth)
        import json

        assert json.loads(request["options"]["body"]) == body


def test_runtime_preserves_avatar_presentation_and_voicechat_configuration():
    definitions = scene_definitions()
    avatar = {"Enabled": True, "BackgroundUrl": "https://example.com/avatar.png"}
    definitions["default"]["VoiceChat"]["Config"]["AvatarConfig"] = avatar
    config = runtime_config()
    scenes = create_runtime_scenes(definitions, config)
    assert scenes["default"].scene["isAvatarScene"] is True
    assert scenes["default"].scene["avatarBgUrl"] == avatar["BackgroundUrl"]
    assert build_start_request(scenes["default"], config)["Config"]["AvatarConfig"] == avatar


@pytest.mark.asyncio
async def test_http_surface_isolates_sessions_and_allows_concurrent_pages():
    calls: List[Dict[str, Any]] = []

    async def invoke_voice_chat(action: str, body: Dict[str, Any]):
        calls.append({"action": action, "body": body})
        return {"ResponseMetadata": {"Action": action}, "Result": "ok"}

    app = create_app(
        {
            "config": runtime_config(),
            "scene_definitions": scene_definitions(),
            "invoke_voice_chat": invoke_voice_chat,
        }
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health_response = await client.get(
            "/health", headers={"Origin": "http://local-web.example"}
        )
        assert health_response.headers.get("access-control-allow-origin") == "*"
        assert health_response.json() == {"ok": True}
        assert (await client.get("/sts")).status_code == 404

        async def create_session():
            response = await client.post("/getScenes", json={})
            return response.json()

        async def invoke(action: str, session_id: str, scene_id: str = "default"):
            response = await client.post(
                f"/proxy?Action={action}",
                json={"SessionID": session_id, "SceneID": scene_id},
            )
            return response.json()

        first_page, second_page, third_page = await asyncio_gather_sessions(create_session)
        for response in (first_page, second_page, third_page):
            assert response["Result"]["scenes"][0]["scene"]["id"] == "default"
            assert "AppKey" not in response["Result"]["scenes"][0]["rtc"]

        assert len({r["Result"]["SessionID"] for r in (first_page, second_page, third_page)}) == 3
        assert (
            len({r["Result"]["scenes"][0]["rtc"]["RoomId"] for r in (first_page, second_page, third_page)})
            == 3
        )

        starts = [
            await invoke(START_ACTION, first_page["Result"]["SessionID"]),
            await invoke(START_ACTION, second_page["Result"]["SessionID"]),
            await invoke(START_ACTION, third_page["Result"]["SessionID"]),
        ]
        assert all(item["Result"] == "ok" for item in starts)

        stop_response = await client.post(
            f"/proxy?Action={STOP_ACTION}",
            data={
                "SessionID": first_page["Result"]["SessionID"],
                "SceneID": "default",
            },
        )
        assert stop_response.json()["Result"] == "ok"
        assert (
            await invoke(START_ACTION, first_page["Result"]["SessionID"], "alternate")
        )["Result"] == "ok"

        start_calls = [item for item in calls if item["action"] == START_ACTION]
        assert len(start_calls) == 4
        assert len({item["body"]["RoomId"] for item in start_calls}) == 4
        assert len({item["body"]["TaskId"] for item in start_calls}) == 4
        assert {item["body"]["AgentConfig"]["UserId"] for item in start_calls} == {
            "voice_agent",
            "alternate_agent",
        }
        assert all(len(item["body"]["AgentConfig"]["TargetUserId"]) == 1 for item in start_calls)
        assert all(
            item["body"]["AgentConfig"]["EnableConversationStateCallback"] is True
            for item in start_calls
        )

        rejected = await client.post(
            "/proxy?Action=DescribeInstances",
            json={
                "SessionID": third_page["Result"]["SessionID"],
                "SceneID": "default",
            },
        )
        assert rejected.json()["ResponseMetadata"]["Error"]

        await app.state.stop_active_agents()
        assert len([item for item in calls if item["action"] == STOP_ACTION]) == 4


async def asyncio_gather_sessions(create_session):
    import asyncio

    return await asyncio.gather(create_session(), create_session(), create_session())
