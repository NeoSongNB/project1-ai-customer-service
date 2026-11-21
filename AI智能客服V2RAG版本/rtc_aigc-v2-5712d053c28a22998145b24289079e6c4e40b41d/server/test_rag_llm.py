# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from httpx import ASGITransport, AsyncClient

from app import (
    START_ACTION,
    build_start_request,
    create_app,
    create_runtime_scenes,
)
from llm_service import LLMService
from rag_service import RagService
from server_url import resolve_server_url
from test_app import APP_ID, APP_KEY, runtime_config, scene_definitions


def rag_scene_definition() -> Dict[str, Any]:
    return {
        "SceneConfig": {"name": "RAG"},
        "VoiceChat": {
            "Config": {
                "ASRConfig": {"Provider": "volcano", "ProviderParams": {}},
                "TTSConfig": {"Provider": "volcano", "ProviderParams": {}},
                "LLMConfig": {
                    "Mode": "CustomLLM",
                    "Url": "${SERVER_URL}/api/chat_callback",
                    "APIKey": "${CUSTOM_LLM_API_KEY}",
                    "HistoryLength": 10,
                },
            },
            "AgentConfig": {"UserId": "rag_agent"},
        },
    }


def test_resolve_server_url_prefers_env():
    assert (
        resolve_server_url({"SERVER_URL": "https://demo.example.com/"})
        == "https://demo.example.com"
    )


def test_resolve_server_url_allows_localhost_fallback():
    assert (
        resolve_server_url({}, "http://127.0.0.1:3001/")
        == "http://127.0.0.1:3001"
    )


def test_resolve_server_url_rejects_public_fallback():
    with pytest.raises(ValueError, match="SERVER_URL"):
        resolve_server_url({}, "http://8.8.8.8:3001/")


def test_rag_service_missing_config_returns_empty(monkeypatch):
    service = RagService({})
    monkeypatch.setattr(
        "rag_service.httpx.AsyncClient",
        lambda: (_ for _ in ()).throw(AssertionError("must not call upstream")),
    )
    assert asyncio.run(service.retrieve("q")) == ""


def test_rag_service_extracts_contents(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "result_list": [
                        {"content": "alpha"},
                        {"content": "beta"},
                        {"content": ""},
                    ]
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("rag_service.httpx.AsyncClient", lambda: Client())
    service = RagService(
        {
            "VOLCENGINE_ACCESS_KEY_ID": "ak",
            "VOLCENGINE_SECRET_ACCESS_KEY": "sk",
            "VOLC_ACCOUNT_ID": "acc",
            "KB_COLLECTION_NAME": "col",
            "KB_PROJECT_NAME": "default",
        }
    )
    assert asyncio.run(service.retrieve("q")) == "alpha\n\nbeta"


def test_llm_service_builds_system_with_rag(monkeypatch):
    create_calls: List[Dict[str, Any]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return iter([SimpleNamespace(id="1")])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    def ark_factory(**kwargs):
        assert kwargs["api_key"] == "ark-key"
        return fake_client

    import sys
    import types

    fake_pkg = types.ModuleType("volcenginesdkarkruntime")
    fake_pkg.Ark = ark_factory
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", fake_pkg)

    service = LLMService(
        {
            "ARK_API_KEY": "ark-key",
            "ARK_ENDPOINT_ID": "ep-1",
            "RAG_SYSTEM_PROMPT": "persona",
        }
    )
    history = [{"role": "user", "content": "hello"}]
    chunks = list(service.chat_stream(history, "kb-context"))
    assert len(chunks) == 1
    assert create_calls[0]["model"] == "ep-1"
    assert create_calls[0]["stream"] is True
    assert create_calls[0]["messages"][0]["role"] == "system"
    assert "persona" in create_calls[0]["messages"][0]["content"]
    assert "kb-context" in create_calls[0]["messages"][0]["content"]
    assert create_calls[0]["messages"][1:] == history


def test_build_start_request_injects_custom_llm_url():
    definitions = {"rag": rag_scene_definition()}
    config = runtime_config(
        env={
            "SERVER_URL": "https://tunnel.example.com",
            "CUSTOM_LLM_API_KEY": "secret-key",
        }
    )
    scenes = create_runtime_scenes(definitions, config)
    request = build_start_request(scenes["rag"], config)
    llm = request["Config"]["LLMConfig"]
    assert llm["Mode"] == "CustomLLM"
    assert llm["Url"] == "https://tunnel.example.com/api/chat_callback"
    assert llm["APIKey"] == "secret-key"
    assert llm["Method"] == "POST"
    assert llm["ApiType"] == "https"


@pytest.mark.asyncio
async def test_chat_callback_requires_bearer_when_configured():
    class FakeRag:
        async def retrieve(self, query: str) -> str:
            return "ctx"

    class FakeLlm:
        def chat_stream(self, messages, rag_context=""):
            chunk = SimpleNamespace(
                model_dump_json=lambda: '{"id":"c1","choices":[]}'
            )
            yield chunk

    app = create_app(
        {
            "config": runtime_config(
                env={"CUSTOM_LLM_API_KEY": "expected-key"}
            ),
            "scene_definitions": scene_definitions(),
            "invoke_voice_chat": async_ok,
            "rag_service": FakeRag(),
            "llm_service": FakeLlm(),
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/api/chat_callback",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert denied.status_code == 401

        ok = await client.post(
            "/api/chat_callback",
            headers={"Authorization": "Bearer expected-key"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert ok.status_code == 200
        assert "text/event-stream" in ok.headers["content-type"]
        body = ok.text
        assert "data: {\"id\":\"c1\",\"choices\":[]}" in body
        assert "data: [DONE]" in body


@pytest.mark.asyncio
async def test_proxy_start_rag_scene_uses_custom_llm_url():
    calls: List[Dict[str, Any]] = []

    async def invoke(action: str, body: Dict[str, Any]):
        calls.append({"action": action, "body": body})
        return {"ResponseMetadata": {"Action": action}, "Result": "ok"}

    definitions = {**scene_definitions(), "rag": rag_scene_definition()}
    app = create_app(
        {
            "config": runtime_config(
                env={
                    "SERVER_URL": "https://public.example.com",
                    "CUSTOM_LLM_API_KEY": "k",
                }
            ),
            "scene_definitions": definitions,
            "invoke_voice_chat": invoke,
        }
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        session = (await client.post("/getScenes", json={})).json()
        session_id = session["Result"]["SessionID"]
        scene_ids = {item["scene"]["id"] for item in session["Result"]["scenes"]}
        assert "default" in scene_ids
        assert "rag" in scene_ids

        started = await client.post(
            f"/proxy?Action={START_ACTION}",
            json={"SessionID": session_id, "SceneID": "rag"},
        )
        assert started.json()["Result"] == "ok"
        llm = calls[0]["body"]["Config"]["LLMConfig"]
        assert llm["Url"] == "https://public.example.com/api/chat_callback"
        assert llm["Mode"] == "CustomLLM"


async def async_ok(action: str, body: Dict[str, Any]):
    return {"ResponseMetadata": {"Action": action}, "Result": "ok"}
