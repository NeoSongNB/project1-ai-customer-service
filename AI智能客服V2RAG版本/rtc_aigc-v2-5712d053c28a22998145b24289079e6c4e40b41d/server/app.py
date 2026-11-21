# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""本地 AIGC Demo 服务端（FastAPI）。

整体职责
--------
1. 给前端发 RTC 进房凭证（AppId / RoomId / UserId / Token）
2. 代理 StartVoiceChat / StopVoiceChat 到火山 RTC OpenAPI
3. 在 CustomLLM（rag）场景下，接收 RTC 云端回调，先查知识库再调方舟流式补全

主链路（浏览器 ↔ 本服务 ↔ 火山）
--------------------------------
前端 POST /getScenes
  → 本服务创建 Session，为每个场景生成独立 RoomId/UserId/TaskId，并签发 Token
前端 joinRoom 后，POST /proxy?Action=StartVoiceChat
  → 本服务签名调用 rtc.volcengineapi.com StartVoiceChat
若场景 LLMConfig.Mode=CustomLLM（如 scenes/rag.json）：
  RTC 云端 ASR 得到用户话术后，POST {SERVER_URL}/api/chat_callback
  → 本服务：知识库检索 → 方舟流式回答 → SSE 回给 RTC → TTS 播给用户
前端挂断时 POST /proxy?Action=StopVoiceChat
  → 本服务签名调用 StopVoiceChat

配置来源：项目根目录 .env.local（不是 server/.env.local）
场景定义：server/scenes/*.json，文件名（不含后缀）即 SceneID
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from llm_service import LLMService
from openapi import dump_json_body, sign_headers
from rag_service import RagService
from rtc_token import AccessToken, Privileges
from server_url import resolve_server_url
from util import assert_ok, deep_clone, expand_env_references, omit_none, read_scene_files

# ---------------------------------------------------------------------------
# VoiceChat OpenAPI 常量（版本/地址/签名 region 写死在代码里，不走环境变量）
# ---------------------------------------------------------------------------
START_ACTION = "StartVoiceChat"
STOP_ACTION = "StopVoiceChat"
ALLOWED_ACTIONS = frozenset({START_ACTION, STOP_ACTION})
RTC_API_VERSION = "2025-06-01"
RTC_ENDPOINT = "https://rtc.volcengineapi.com"
RTC_REGION = "cn-north-1"
RTC_SERVICE = "rtc"
SERVER_DIR = Path(__file__).resolve().parent
ROOT_ENV = SERVER_DIR.parent / ".env.local"


# ---------------------------------------------------------------------------
# 运行时数据结构（存在内存里；进程重启后全部失效，前端需重新 getScenes）
# ---------------------------------------------------------------------------
@dataclass
class RuntimeConfig:
    """从环境变量解析出的服务端配置。"""

    app_id: str
    app_key: str
    business_id: Optional[str]
    host: str
    port: int
    env: Mapping[str, str]  # 原始环境，场景 JSON 里的 ${ENV_NAME} 从这里展开
    credential: Dict[str, str]  # OpenAPI 签名用的 AK/SK


@dataclass
class RuntimeScene:
    """某一个场景在某个 Session 下的运行时状态。

    status 状态机：
      idle → starting → active → stopping → idle
    pending：进行中的 Start/Stop 协程；并发重复请求会 await 同一个 pending，避免重复打云端。
    """

    scene: Dict[str, Any]  # 返回给前端展示用（名称、是否视觉场景等）
    rtc: Dict[str, Any]  # 返回给前端进房用（AppId/RoomId/UserId/Token）
    voice_chat: Dict[str, Any]  # 场景 JSON 里的 VoiceChat 模板（Start 时再展开环境变量）
    room_id: str
    user_id: str
    task_id: str
    status: str = "idle"
    pending: Optional[asyncio.Task] = None


@dataclass
class Session:
    """一次前端页面会话。每个 getScenes 创建一个新 Session。"""

    id: str
    scenes: Dict[str, RuntimeScene]
    active_scene_id: Optional[str] = None  # 同一 Session 同时只允许一个场景在通话


InvokeVoiceChat = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


def required(value: Optional[str], name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def load_runtime_config(env: Optional[Mapping[str, str]] = None) -> RuntimeConfig:
    """读取必填凭证；缺任一必填项时服务无法启动。"""
    source = env if env is not None else os.environ
    access_key_id = (source.get("VOLCENGINE_ACCESS_KEY_ID") or "").strip()
    secret_key = (source.get("VOLCENGINE_SECRET_ACCESS_KEY") or "").strip()
    # AK/SK 必须成对出现，不允许只配其中一个
    if bool(access_key_id) != bool(secret_key):
        raise ValueError(
            "VOLCENGINE_ACCESS_KEY_ID 和 VOLCENGINE_SECRET_ACCESS_KEY 必须同时配置"
        )

    port_raw = source.get("PORT") or "3001"
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("PORT 必须为正整数") from exc
    if port <= 0:
        raise ValueError("PORT 必须为正整数")

    business_id = (source.get("RTC_BUSINESS_ID") or "").strip() or None
    return RuntimeConfig(
        app_id=required(source.get("RTC_APP_ID"), "RTC_APP_ID"),
        app_key=required(source.get("RTC_APP_KEY"), "RTC_APP_KEY"),
        business_id=business_id,
        host=(source.get("HOST") or "").strip() or "127.0.0.1",
        port=port,
        env=source,
        credential={
            "accessKeyId": required(access_key_id, "VOLCENGINE_ACCESS_KEY_ID"),
            "secretKey": required(secret_key, "VOLCENGINE_SECRET_ACCESS_KEY"),
        },
    )


def validate_scene(scene_id: str, value: Any) -> None:
    """校验场景 JSON 最小必填字段；AgentConfig.UserId 是智能体在 RTC 房间里的用户名。"""
    if not isinstance(value, dict):
        raise ValueError(f"{scene_id}: scene 必须为对象")
    if not isinstance(value.get("SceneConfig"), dict):
        raise ValueError(f"{scene_id}: SceneConfig 不能为空")
    voice_chat = value.get("VoiceChat") or {}
    if not voice_chat.get("Config") or not voice_chat.get("AgentConfig"):
        raise ValueError(f"{scene_id}: VoiceChat.Config 和 VoiceChat.AgentConfig 不能为空")
    user_id = voice_chat["AgentConfig"].get("UserId")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError(f"{scene_id}: VoiceChat.AgentConfig.UserId 不能为空")


def create_rtc_token(config: RuntimeConfig, room_id: str, user_id: str) -> str:
    """按官方 AccessToken 规则签发正式 Token；必须与进房时的 RoomId/UserId 一致。"""
    key = AccessToken(config.app_id, config.app_key, room_id, user_id)
    key.add_privilege(Privileges.PrivSubscribeStream, 0)
    key.add_privilege(Privileges.PrivPublishStream, 0)
    key.expire_time(int(__import__("time").time()) + 24 * 3600)
    return key.serialize()


def create_runtime_scenes(
    scene_definitions: Mapping[str, Any], config: RuntimeConfig
) -> Dict[str, RuntimeScene]:
    """把 scenes/*.json 变成运行时对象：每个场景一套独立房间/用户/任务/Token。

    AppId/RoomId/TaskId/TargetUserId 由服务端覆盖或生成，场景文件里不用手写。
    前端展示字段（isVision / isAvatarScene 等）从 VoiceChat.Config 推导。
    """
    scenes: Dict[str, RuntimeScene] = {}
    for scene_id, definition in scene_definitions.items():
        validate_scene(scene_id, definition)
        room_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        voice_chat = deep_clone(definition["VoiceChat"])
        avatar_config = (voice_chat.get("Config") or {}).get("AvatarConfig") or {}
        vision_config = ((voice_chat.get("Config") or {}).get("LLMConfig") or {}).get(
            "VisionConfig"
        ) or {}
        snapshot_config = vision_config.get("SnapshotConfig") or {}
        scene = {
            "id": scene_id,
            **deep_clone(definition["SceneConfig"]),
            "botName": voice_chat["AgentConfig"]["UserId"],
            "isAvatarScene": bool(avatar_config.get("Enabled")),
            "avatarBgUrl": avatar_config.get("BackgroundUrl"),
            "isInterruptMode": voice_chat["Config"].get("InterruptMode") == 0,
            "isVision": bool(vision_config.get("Enable")),
            "isScreenMode": snapshot_config.get("StreamType") == 1,
        }
        rtc = omit_none(
            {
                "AppId": config.app_id,
                "BusinessId": config.business_id,
                "RoomId": room_id,
                "UserId": user_id,
                "Token": create_rtc_token(config, room_id, user_id),
            }
        )
        scenes[scene_id] = RuntimeScene(
            scene=scene,
            rtc=rtc,
            voice_chat=voice_chat,
            room_id=room_id,
            user_id=user_id,
            task_id=task_id,
        )
    return scenes


def apply_custom_llm_url(
    voice_chat: Dict[str, Any],
    config: RuntimeConfig,
    *,
    request_base_url: Optional[str] = None,
) -> None:
    """CustomLLM 场景：把 LLMConfig.Url 改写成 RTC 云端能访问的绝对回调地址。

    优先用 .env 的 SERVER_URL（ngrok 等公网 Origin）；
    未配置时仅允许 localhost / 私网回退地址。
    CUSTOM_LLM_API_KEY 会写入 LLMConfig.APIKey，RTC 回调时带 Bearer。
    """
    llm_config = (voice_chat.get("Config") or {}).get("LLMConfig") or {}
    if llm_config.get("Mode") != "CustomLLM":
        return
    server_url = resolve_server_url(config.env, request_base_url)
    llm_config["Url"] = f"{server_url}/api/chat_callback"
    llm_config["Method"] = "POST"
    llm_config["ApiType"] = "https" if server_url.startswith("https") else "http"
    api_key = (config.env.get("CUSTOM_LLM_API_KEY") or "").strip()
    if api_key:
        llm_config["APIKey"] = api_key
    elif llm_config.get("APIKey") in (None, "", "${CUSTOM_LLM_API_KEY}"):
        # 没配密钥就不要把占位符或空字符串发给云端
        llm_config.pop("APIKey", None)
    voice_chat.setdefault("Config", {})["LLMConfig"] = llm_config


def build_start_request(
    runtime_scene: RuntimeScene,
    config: RuntimeConfig,
    *,
    request_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """组装 StartVoiceChat 请求体。

    - 场景 JSON 里的 ${ENV_NAME} 在这里展开
    - AppId / RoomId / TaskId / TargetUserId 由服务端覆盖
    - 始终打开 EnableConversationStateCallback，前端靠它判断 AI 是否就绪
    """
    # 可选 RAG/CustomLLM 键可能不在 env 里；先补空字符串，避免 ${ENV} 展开时报「缺少环境变量」
    expand_env = dict(config.env)
    for key in (
        "SERVER_URL",
        "CUSTOM_LLM_API_KEY",
        "RAG_SYSTEM_PROMPT",
        "KB_PROJECT_NAME",
        "ARK_API_KEY",
        "ARK_ENDPOINT_ID",
        "VOLC_ACCOUNT_ID",
        "KB_COLLECTION_NAME",
    ):
        expand_env.setdefault(key, "")
    voice_chat = expand_env_references(deep_clone(runtime_scene.voice_chat), expand_env)
    apply_custom_llm_url(voice_chat, config, request_base_url=request_base_url)
    agent_config = {
        **voice_chat.get("AgentConfig", {}),
        "TargetUserId": [runtime_scene.user_id],
        "EnableConversationStateCallback": True,
    }
    request = {
        **voice_chat,
        "AppId": config.app_id,
        "RoomId": runtime_scene.room_id,
        "TaskId": runtime_scene.task_id,
        "AgentConfig": agent_config,
    }
    if config.business_id is not None:
        request["BusinessId"] = config.business_id
    return request


def build_stop_request(runtime_scene: RuntimeScene, config: RuntimeConfig) -> Dict[str, Any]:
    """StopVoiceChat 只需 AppId + RoomId + TaskId。"""
    return {
        "AppId": config.app_id,
        "RoomId": runtime_scene.room_id,
        "TaskId": runtime_scene.task_id,
    }


def response_succeeded(response: Optional[Mapping[str, Any]]) -> bool:
    """火山 OpenAPI 常返回 HTTP 200，业务成败看 ResponseMetadata.Error。"""
    return not (response or {}).get("ResponseMetadata", {}).get("Error")


def local_success(action: str) -> Dict[str, Any]:
    """本地幂等成功响应：例如已经 active 再 Start、已经 idle 再 Stop。"""
    return {
        "ResponseMetadata": {
            "Action": action,
            "RequestId": f"local-{uuid.uuid4()}",
        },
        "Result": "ok",
    }


def create_open_api_invoker(
    config: RuntimeConfig,
    fetch_impl: Optional[Callable[..., Awaitable[Any]]] = None,
) -> InvokeVoiceChat:
    """返回一个可注入的 OpenAPI 调用函数（测试可 mock fetch_impl）。"""

    async def default_fetch(url: str, options: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                options["method"],
                url,
                headers=options["headers"],
                content=options["body"],
            )
            return response.json()

    fetch = fetch_impl or default_fetch

    async def invoke(action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        query = {"Action": action, "Version": RTC_API_VERSION}
        host = "rtc.volcengineapi.com"
        body_str = dump_json_body(body)
        # 签名内容必须与实际发出的 body 字节完全一致
        headers = sign_headers(
            method="POST",
            host=host,
            path="/",
            query=query,
            body=body_str,
            access_key_id=config.credential["accessKeyId"],
            secret_key=config.credential["secretKey"],
            region=RTC_REGION,
            service=RTC_SERVICE,
        )
        url = f"{RTC_ENDPOINT}/?{urlencode(query)}"
        return await fetch(
            url,
            {
                "method": "POST",
                "headers": headers,
                "body": body_str,
            },
        )

    return invoke


def create_app(options: Optional[Mapping[str, Any]] = None) -> FastAPI:
    """工厂：创建 FastAPI 应用。测试可注入 config / scene_definitions / invoke_voice_chat。"""
    options = dict(options or {})
    config: RuntimeConfig = options.get("config") or load_runtime_config(options.get("env"))
    definitions = options.get("scene_definitions")
    if definitions is None:
        definitions = read_scene_files(SERVER_DIR / "scenes")
    invoke_voice_chat: InvokeVoiceChat = options.get("invoke_voice_chat") or create_open_api_invoker(
        config, options.get("fetch_impl")
    )
    rag_service: RagService = options.get("rag_service") or RagService(config.env)
    llm_service: LLMService = options.get("llm_service") or LLMService(config.env)
    sessions: Dict[str, Session] = {}
    # StartVoiceChat 时用来解析 CustomLLM 回调 Origin（来自当前 HTTP 请求的 base_url）
    start_request_base_url: Dict[str, Optional[str]] = {"value": None}

    def create_session() -> Session:
        session = Session(
            id=str(uuid.uuid4()),
            scenes=create_runtime_scenes(definitions, config),
        )
        sessions[session.id] = session
        return session

    def release_session_call(session: Session, runtime_scene: RuntimeScene) -> None:
        runtime_scene.status = "idle"
        if session.active_scene_id == runtime_scene.scene["id"]:
            session.active_scene_id = None

    async def start_session_scene(session: Session, runtime_scene: RuntimeScene) -> Dict[str, Any]:
        """启动某个场景的 VoiceChat；处理并发合并与状态机。"""
        assert_ok(
            not session.active_scene_id or session.active_scene_id == runtime_scene.scene["id"],
            "同一 session 只允许一个 scene 进行通话",
        )
        # 已有进行中的 Start：直接等同一个 pending，避免重复创建任务
        if runtime_scene.status == "starting":
            assert runtime_scene.pending is not None
            return await runtime_scene.pending
        # 正在 Stop：等 Stop 完成后再尝试 Start
        if runtime_scene.status == "stopping":
            assert runtime_scene.pending is not None
            await runtime_scene.pending
            return await start_session_scene(session, runtime_scene)
        if runtime_scene.status == "active":
            return local_success(START_ACTION)

        session.active_scene_id = runtime_scene.scene["id"]
        runtime_scene.status = "starting"

        async def _run() -> Dict[str, Any]:
            return await invoke_voice_chat(
                START_ACTION,
                build_start_request(
                    runtime_scene,
                    config,
                    request_base_url=start_request_base_url["value"],
                ),
            )

        runtime_scene.pending = asyncio.create_task(_run())
        try:
            response = await runtime_scene.pending
            if response_succeeded(response):
                runtime_scene.status = "active"
            else:
                release_session_call(session, runtime_scene)
            return response
        except Exception:
            release_session_call(session, runtime_scene)
            raise
        finally:
            runtime_scene.pending = None

    async def stop_session_scene(session: Session, runtime_scene: RuntimeScene) -> Dict[str, Any]:
        """停止 VoiceChat；idle/正在 stopping 视为幂等成功。"""
        if runtime_scene.status == "idle":
            return local_success(STOP_ACTION)
        if runtime_scene.status == "starting":
            try:
                assert runtime_scene.pending is not None
                await runtime_scene.pending
            except Exception:
                return local_success(STOP_ACTION)
            return await stop_session_scene(session, runtime_scene)
        if runtime_scene.status == "stopping":
            assert runtime_scene.pending is not None
            await runtime_scene.pending
            return local_success(STOP_ACTION)

        previous_status = runtime_scene.status
        runtime_scene.status = "stopping"

        async def _run() -> Dict[str, Any]:
            return await invoke_voice_chat(
                STOP_ACTION, build_stop_request(runtime_scene, config)
            )

        runtime_scene.pending = asyncio.create_task(_run())
        try:
            response = await runtime_scene.pending
            if response_succeeded(response):
                release_session_call(session, runtime_scene)
            else:
                # 云端 Stop 失败：恢复原状态，方便前端重试
                runtime_scene.status = previous_status
            return response
        except Exception:
            runtime_scene.status = previous_status
            raise
        finally:
            runtime_scene.pending = None

    async def stop_active_agents() -> None:
        """进程退出时尽量停掉所有仍在通话的任务，避免云端任务残留。"""
        active = []
        for session in sessions.values():
            for scene in session.scenes.values():
                if scene.status != "idle":
                    active.append((session, scene))
        results = []
        for session, scene in active:
            if scene.status in {"starting", "stopping"} and scene.pending is not None:
                try:
                    await scene.pending
                except Exception:
                    pass
            if scene.status == "active":
                results.append(stop_session_scene(session, scene))
        if results:
            await asyncio.gather(*results, return_exceptions=True)
        for session, scene in active:
            if scene.status != "idle":
                release_session_call(session, scene)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await stop_active_agents()

    app = FastAPI(lifespan=lifespan)
    # 仅用于本地 Demo；生产环境不要 allow_origins=["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> Dict[str, bool]:
        """进程存活探测；不检查云端凭证或场景是否可用。"""
        return {"ok": True}

    @app.post("/getScenes")
    async def get_scenes() -> Dict[str, Any]:
        """前端进页时调用：创建新 Session，返回各场景展示信息 + RTC 进房参数。"""
        try:
            session = create_session()
            return {
                "ResponseMetadata": {"Action": "getScenes"},
                "Result": {
                    "SessionID": session.id,
                    "scenes": [
                        {"scene": item.scene, "rtc": item.rtc}
                        for item in session.scenes.values()
                    ],
                },
            }
        except Exception as exc:
            return {
                "ResponseMetadata": {
                    "Action": "getScenes",
                    "Error": {"Code": -1, "Message": str(exc)},
                }
            }

    @app.post("/proxy")
    async def proxy(
        request: Request, Action: Optional[str] = Query(default=None)
    ) -> JSONResponse:
        """代理 StartVoiceChat / StopVoiceChat。

        请求体：{"SessionID": "...", "SceneID": "default"|"rag"|...}
        Stop 也接受表单编码（页面卸载时用 sendBeacon）。
        """
        try:
            assert_ok(
                Action in ALLOWED_ACTIONS,
                "Action 仅支持 StartVoiceChat 或 StopVoiceChat",
            )
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body = await request.json()
            else:
                form = await request.form()
                body = {key: form.get(key) for key in form.keys()}
            session_id = body.get("SessionID")
            scene_id = body.get("SceneID")
            assert_ok(session_id, "SessionID 不能为空")
            assert_ok(scene_id, "SceneID 不能为空")
            session = sessions.get(session_id)
            assert_ok(session, f"{session_id} 不存在")
            runtime_scene = session.scenes.get(scene_id)
            assert_ok(runtime_scene, f"{scene_id} 不存在")
            # 供 CustomLLM 在未配置 SERVER_URL 时做 localhost/私网回退
            start_request_base_url["value"] = str(request.base_url).rstrip("/")
            result = (
                await start_session_scene(session, runtime_scene)
                if Action == START_ACTION
                else await stop_session_scene(session, runtime_scene)
            )
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse(
                {
                    "ResponseMetadata": {
                        "Action": "proxy",
                        "Error": {"Code": -1, "Message": str(exc)},
                    }
                }
            )

    def _verify_custom_llm_api_key(request: Request) -> None:
        """若配置了 CUSTOM_LLM_API_KEY，则要求 Authorization: Bearer <key>。"""
        expected = (config.env.get("CUSTOM_LLM_API_KEY") or "").strip()
        if not expected:
            return  # 未配置则跳过校验（仅适合本地；公网不建议）
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = authorization.strip()
        if token != expected:
            raise HTTPException(status_code=401, detail="Invalid CustomLLM APIKey")

    @app.post("/api/chat_callback")
    async def chat_callback(request: Request):
        """RTC CustomLLM 回调入口（仅 rag 等 CustomLLM 场景会打到这里）。

        入参：OpenAI 风格 {"messages":[{"role":"user","content":"..."}, ...]}
        出参：text/event-stream，chunk JSON + 结尾 data: [DONE]
        内部：retrieve(最后一句用户话) → 拼 system → 方舟 stream → SSE
        """
        _verify_custom_llm_api_key(request)
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"text": ""})

        messages = data.get("messages", [])
        if not messages or messages[-1].get("role") != "user":
            return JSONResponse({"text": ""})

        async def generate_sse():
            rag_content = await rag_service.retrieve(messages[-1].get("content", ""))

            def _collect_chunks():
                # 方舟 SDK 是同步流式；放到线程里，避免阻塞事件循环
                out = []
                for chunk in llm_service.chat_stream(messages, rag_content):
                    if chunk:
                        out.append(chunk)
                return out

            try:
                chunks = await asyncio.to_thread(_collect_chunks)
            except Exception as exc:  # noqa: BLE001
                print(f"LLM 流式异常: {exc}")
                chunks = []
            for chunk in chunks:
                payload = (
                    chunk.model_dump_json()
                    if hasattr(chunk, "model_dump_json")
                    else dump_json_body(chunk)
                )
                yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/debug/rag")
    async def debug_rag(query: str = Query(default="")):
        """联调知识库检索（不经过 LLM）。例：/debug/rag?query=测试"""
        if not query:
            return {"error": "请提供 query 参数"}
        context = await rag_service.retrieve(query)
        return {
            "query": query,
            "retrieved_context": context,
            "length": len(context) if context else 0,
            "status": "success" if context else "no_results_or_error",
        }

    app.state.sessions = sessions
    app.state.stop_active_agents = stop_active_agents
    app.state.config = config
    app.state.rag_service = rag_service
    app.state.llm_service = llm_service
    return app


def create_default_app() -> FastAPI:
    """uvicorn factory 入口：加载根目录 .env.local 后创建应用。"""
    load_dotenv(ROOT_ENV)
    return create_app()


def start_server(options: Optional[Mapping[str, Any]] = None) -> None:
    """本地启动：默认开启热重载（RELOAD 未设置时为 1）。"""
    import uvicorn

    options = dict(options or {})
    if "config" not in options:
        load_dotenv(ROOT_ENV)
    config = options.get("config") or load_runtime_config(options.get("env"))
    reload_enabled = os.environ.get("RELOAD", "1") == "1"
    uvicorn.run(
        "app:create_default_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=reload_enabled,
        reload_includes=["*.py", "*.json"] if reload_enabled else None,
        reload_dirs=[str(SERVER_DIR)] if reload_enabled else None,
    )


if __name__ == "__main__":
    start_server()
