# Python Server

## 启动命令

先按根目录 [README](../README.md#快速开始) 配置凭证和场景。以下命令均从项目根目录执行：

```shell
uv sync --directory server --frozen
yarn server
```

`yarn server` 和 `yarn dev` 都通过 `uv run --directory server` 启动，使用 `server/.venv` 里的 Python 3.12，不依赖当前 shell 的 `python3`、pyenv 或 conda。

开发时可设置 `RELOAD=1`（默认）以监听 `server/` 下 `.py` / `.json` 变化并自动重启。

服务端读取项目根目录 `.env.local`，不是 `server/.env.local`；已有进程环境变量优先于文件中的值。
默认监听 `127.0.0.1:3001`，可通过根目录 `.env.local` 中的 `HOST`、`PORT` 修改。
不使用热重载时，先设置环境变量 `RELOAD=0` 再启动。macOS / Linux 写成 `RELOAD=0 uv run --directory server python app.py`；Windows PowerShell 写成 `$env:RELOAD="0"; uv run --directory server python app.py`；cmd 写成 `set RELOAD=0&& uv run --directory server python app.py`。不要把 `RELOAD=1` 直接写在命令前面，Windows 会把它当成程序名。

## 使用须知

服务启动时会自动读取 `server/scenes` 下的 `.json` 文件作为可用的场景, 并通过接口 API 返回相关信息。

因此，您需要：

1. 在 `server/scenes` 目录下参考其它 JSON 的格式, 自定义创建一个 `xxxx.json` 文件，用于描述您的场景，其中 xxxx 为场景名称。
2. 确保您的 `.json` 文件符合模版定义（可参考 `default.json`），大小写敏感。
3. 修改场景后重启服务端并刷新页面。`RELOAD=1` 会监听 JS/JSON 变化自动重启，但根目录 `.env.local` 不在监听范围内。
4. 服务端为每个 session 动态生成 RTC RoomId、UserId、Token 与 VoiceChat TaskId，不写回 JSON；AgentConfig.UserId 必须由场景提供。

## 接口约定

- `GET /health` 返回 `{"ok":true}`，不检查云端凭证或服务权限。
- `POST /getScenes`：请求体为 `{}`，每次调用创建新的 session；返回 `{ ResponseMetadata, Result: { SessionID, scenes: [{ scene, rtc }] } }`。
- `POST /proxy?Action=StartVoiceChat` 和 `POST /proxy?Action=StopVoiceChat`：JSON 请求体为 `{ "SessionID": "...", "SceneID": "default" }`；`SceneID` 对应场景文件名（如 `rag`）。停止请求也接受表单编码，供页面卸载时发送。
- `POST /api/chat_callback`：RTC CustomLLM 回调（OpenAI 风格 `messages` → SSE）。若配置了 `CUSTOM_LLM_API_KEY`，需携带 `Authorization: Bearer <key>`。
- `GET /debug/rag?query=`：联调知识库检索（返回检索文本，不经 LLM）。
- 代理接口返回 OpenAPI 的 `ResponseMetadata` / `Result` 结构；调用方需检查 `ResponseMetadata.Error`，不能仅凭 HTTP 状态判断业务成功。
- 不同 session 使用独立的房间、用户和 VoiceChat 任务；同一 session 同时仅允许一个场景通话。
- session 保存在服务端内存中，重启后失效，前端需刷新页面重新获取。
- 服务端覆盖 VoiceChat 的 AppId、RoomId、TaskId、AgentConfig.TargetUserId，并启用 EnableConversationStateCallback；这些运行时字段无需写入场景。
- CustomLLM 场景会在 Start 时把 `LLMConfig.Url` 解析为 `{SERVER_URL}/api/chat_callback`（无 `SERVER_URL` 时仅允许 localhost/RFC1918 回退）。

## 相关参数获取

- 根目录 `.env.local`
  - `RTC_APP_ID`、`RTC_APP_KEY` 为服务端 RTC 凭证。
  - 第三方 secret 也只保存在此文件，由场景 JSON 通过 `${ENV_NAME}` 引用。
  - `VOLCENGINE_ACCESS_KEY_ID`、`VOLCENGINE_SECRET_ACCESS_KEY` 必须成对配置，用于签名调用 OpenAPI，并复用于知识库签名。
  - 可选 `RTC_BUSINESS_ID` 同时传给 RTC SDK 和 VoiceChat。
  - RAG 场景另需 `SERVER_URL`、`ARK_API_KEY`、`ARK_ENDPOINT_ID`、`VOLC_ACCOUNT_ID`、`KB_COLLECTION_NAME`、`CUSTOM_LLM_API_KEY`（详见根目录 `.env.example`）。
- VoiceChat
  - 参考 [VoiceChat 参数说明](https://docs.volcengine.com/docs/6348/2123348)。
  - 可在 [我的智能体](https://console.volcengine.com/conversational-ai/agentManage) 中创建、配置并保存智能体，再从管理页的 `代码示例` 获取 `StartVoiceChat` 配置。
    将请求体中的 `Config`、`AgentConfig` 分别填入场景 JSON 的 `VoiceChat.Config`、`VoiceChat.AgentConfig`；
    AppId、RoomId、TaskId、TargetUserId 由本 Demo 服务端设置，密钥改用环境变量引用。
  - `scenes/rag.json` 使用 `Mode: CustomLLM`；知识库检索在本地回调中完成，不是 ArkV3 MCP 路径。

## 注意

- 相关错误会通过服务端接口返回。
- 服务端只代理 StartVoiceChat/StopVoiceChat，不提供 STS HTTP 接口。
- 为方便本地开发，服务端 CORS 允许任意 Origin；该配置不适合直接用于生产环境。
- 服务会根据您配置的 `VoiceChat` 中是否存在视觉模型相关的配置返回相关信息给前端页面, 从而控制相关 UI 是否展示。

## 测试

```shell
yarn server:test
```
