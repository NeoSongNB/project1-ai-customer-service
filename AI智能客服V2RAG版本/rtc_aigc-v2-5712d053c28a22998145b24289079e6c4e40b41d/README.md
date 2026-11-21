# 交互式 AIGC 场景 AIGC Demo

此 Demo 为简化版本, 如您有 1.5.x 版本 UI 的诉求, 可切换至 1.5.1 分支。
首次运行需要填写根目录 `.env.local` 中的凭证，并配置 `server/scenes/*.json`。

项目代码分为两个并列子项目：

- `web/`：CRA 前端。
- `server/`：Python FastAPI 本地开发服务。

根目录 `package.json` 只负责编排并同时启动两个子项目。

## 简介

- 在 AIGC 对话场景下，火山引擎 AIGC-RTC Server 云端服务，通过整合 RTC 音视频流处理，ASR 语音识别，大模型接口调用集成，以及 TTS 语音生成等能力，提供基于流式语音的端到端 AIGC 能力链路。
- 用户只需调用基于标准的 OpenAPI 接口即可配置所需的 ASR、LLM、TTS 类型和参数。火山引擎云端计算服务负责边缘用户接入、云端资源调度、音视频流压缩、文本与语音转换处理以及数据订阅传输等环节。简化开发流程，让开发者更专注在对大模型核心能力的训练及调试，从而快速推进 AIGC 产品应用创新。
- 同时火山引擎 RTC 拥有成熟的音频 3A 处理、视频处理等技术以及大规模音视频聊天能力，可支持 AIGC 产品更便捷的支持多模态交互、多人互动等场景能力，保持交互的自然性和高效性。

## 【必看】环境准备

| 依赖 | 版本要求 |
| :--- | :--- |
| Node.js | 22 |
| Yarn | 1.22.22 |
| [uv](https://docs.astral.sh/uv/) | 最新稳定版（由它安装并管理 Python 3.12） |

以下命令均在**项目根目录**执行。

## 快速开始

### 步骤 1：安装依赖

```shell
# 根目录编排工具（concurrently）
yarn install --frozen-lockfile

# 前端依赖
yarn --cwd web install --frozen-lockfile

# Python 服务端：uv 会按 server/.python-version 准备 Python 3.12，并安装依赖到 server/.venv
uv sync --directory server --frozen
```

未安装 uv 时：

```shell
# macOS
brew install uv

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# 或：winget install --id=astral-sh.uv -e
```

安装后重新打开终端，再执行上面的 `uv sync`。

### 步骤 2：准备配置文件

首次运行时复制示例文件；已有 `.env.local` 时保留原文件，仅补充所需字段。

```shell
cp -n .env.example .env.local
cp -n web/.env.example web/.env.local
```

在根目录 `.env.local` 中填写必填凭证（缺一不可，否则服务端无法启动）：

| 变量 | 说明 |
| :--- | :--- |
| `RTC_APP_ID` / `RTC_APP_KEY` | [RTC 控制台](https://console.volcengine.com/rtc) 应用凭证 |
| `VOLCENGINE_ACCESS_KEY_ID` / `VOLCENGINE_SECRET_ACCESS_KEY` | 火山引擎 AK/SK，用于签名调用 VoiceChat OpenAPI（知识库检索复用同一对 AK/SK） |
| `RTC_BUSINESS_ID` | 可选，同时传给 RTC SDK 和 VoiceChat |

**知识库 / CustomLLM 场景（`rag`）额外配置：**

| 变量 | 说明 | 获取方式 |
| :--- | :--- | :--- |
| `SERVER_URL` | RTC 云端可访问的公网 Origin（无 path） | 本地用 ngrok 等隧道，例如 `ngrok http 3001` 得到的 `https://xxxx.ngrok-free.app` |
| `ARK_API_KEY` / `ARK_ENDPOINT_ID` | 回调里调用方舟大模型 | [火山方舟控制台](https://console.volcengine.com/ark) 创建推理接入点后复制 |
| `VOLC_ACCOUNT_ID` / `KB_COLLECTION_NAME` | 火山知识库主账号 ID 与集合名 | 知识库控制台 / 账号中心 |
| `KB_PROJECT_NAME` | 知识库项目名，默认 `default` | 可选 |
| `CUSTOM_LLM_API_KEY` | 保护 `/api/chat_callback` 的自建密钥（写入 `LLMConfig.APIKey`，RTC 回调带 Bearer） | **自行生成，不是控制台领取**（见下方） |
| `RAG_SYSTEM_PROMPT` | 覆盖知识库场景默认人设 | 可选 |

`CUSTOM_LLM_API_KEY` 与 `ARK_API_KEY` 不同：

| | `CUSTOM_LLM_API_KEY` | `ARK_API_KEY` |
| :--- | :--- | :--- |
| 用途 | RTC → 本服务回调鉴权 | 本服务 → 方舟推理鉴权 |
| 来源 | 自己生成一串密钥 | 方舟控制台 API Key |
| 不填时 | 本服务跳过 Bearer 校验（公网不建议） | 知识库场景无法调模型 |

生成示例：

```shell
openssl rand -hex 32
```

把输出填进 `.env.local` 的 `CUSTOM_LLM_API_KEY=`。

按需编辑 `server/scenes/default.json`（ArkV3）或 `server/scenes/rag.json`（CustomLLM + 知识库）。
`VoiceChat.AgentConfig.UserId` 必须填写；`SceneConfig.name` 和 `icon` 用于前端展示。
新增场景时复制 JSON 并改文件名，文件名（不含 `.json`）即 SceneID。
密钥只放在 `.env.local`，场景 JSON 通过 `${ENV_NAME}` 引用。

场景配置说明：

- `SceneConfig`：场景名称、头像等展示信息。
- `VoiceChat`：AIGC 配置，可参考 [VoiceChat 参数说明](https://docs.volcengine.com/docs/6348/2123348)。
- `default`：`LLMConfig.Mode=ArkV3`，云端直连方舟。
- `rag`：`LLMConfig.Mode=CustomLLM`，RTC 回调本服务 `/api/chat_callback`，先查知识库再调方舟流式补全。
- 也可在 [我的智能体](https://console.volcengine.com/conversational-ai/agentManage) 创建智能体后，从管理页 `代码示例` 取 `Config` / `AgentConfig` 填入场景 JSON。
- AppId、RoomId、TaskId、TargetUserId 由本 Demo 服务端设置，无需手写进场景文件。

### 步骤 3：启动项目

一键同时启动 Python 服务端 + 前端：

```shell
yarn dev
```

`yarn dev` 里的服务端是 `uv run --directory server python app.py`，解释器来自 `server/.venv`（Python 3.12），不使用当前 shell 的 `python3`、pyenv 或 conda。这条命令在 Windows 的 cmd 和 PowerShell 里同样可用；热重载默认开启，不需要再写 Unix 风格的 `RELOAD=1`。

启动成功后：

| 服务 | 地址 |
| :--- | :--- |
| 前端页面 | [http://localhost:3000](http://localhost:3000)（若 `web/.env.local` 设置了 `PORT`，以该端口为准） |
| 服务端 | [http://127.0.0.1:3001](http://127.0.0.1:3001) |
| 健康检查 | [http://127.0.0.1:3001/health](http://127.0.0.1:3001/health)（返回 `{"ok":true}` 只代表进程已起） |

打开前端，选择场景并开始通话，按浏览器提示授权麦克风。

使用**知识库助手**（`rag`）场景前：

1. 填写上方 RAG / CustomLLM 相关环境变量（含自行生成的 `CUSTOM_LLM_API_KEY`）。
2. 按下方 **ngrok 配置** 得到公网 `SERVER_URL`，写入 `.env.local` 后重启服务端。
3. 前端选择「知识库助手」再开始通话。未配置公网地址时，CustomLLM 回调失败，表现常为 AI 无回复。
4. 联调检索可访问 [http://127.0.0.1:3001/debug/rag?query=测试](http://127.0.0.1:3001/debug/rag?query=测试)。

#### ngrok 配置（CustomLLM / 知识库场景必做）

RTC 云端需要回调本机 `/api/chat_callback`，本地开发必须把 **服务端 3001** 暴露为公网 HTTPS Origin。

1. 安装并登录（首次）：

```shell
# macOS
brew install ngrok
# 或从 https://ngrok.com/download 安装

# 在 https://dashboard.ngrok.com/get-started/your-authtoken 复制 authtoken 后执行
ngrok config add-authtoken <YOUR_AUTHTOKEN>
```

2. 先启动本项目（`yarn dev` 或 `yarn server`），确认 [http://127.0.0.1:3001/health](http://127.0.0.1:3001/health) 返回 `{"ok":true}`。

3. 另开终端穿透 3001 端口：

```shell
ngrok http 3001
```

终端或 [http://127.0.0.1:4040](http://127.0.0.1:4040) 会显示类似：

```text
Forwarding    https://abcd-12-34-56-78.ngrok-free.app -> http://localhost:3001
```

4. 将 **Origin 整段**（只要 `https://...ngrok-free.app`，不要加 `/api/...` 路径，不要末尾 `/`）写入根目录 `.env.local`：

```env
SERVER_URL=https://abcd-12-34-56-78.ngrok-free.app
```

5. **重启服务端**（改 `.env.local` 不会热更新），刷新前端页面，再选「知识库助手」通话。

注意：

- 免费版 ngrok 每次重启域名可能变化，域名变了要同步改 `SERVER_URL` 并重启服务端。
- 浏览器打开 ngrok 域名可能出现 Interstitial 提示页；RTC 服务端回调一般不受影响。若回调异常，可在 ngrok 控制台关闭 browser warning，或换付费/固定域名。
- `SERVER_URL` 只填 Origin，正确示例：`https://xxxx.ngrok-free.app`；错误示例：`https://xxxx.ngrok-free.app/api/chat_callback`。
- 也可用 Cloudflare Tunnel、frp、自有 HTTPS 反代，只要 RTC 能访问该 Origin 即可。

### 步骤 4（可选）：分开启动 / 测试 / 构建

```shell
# 终端 1：仅服务端
yarn server

# 终端 2：仅前端
yarn --cwd web start

# 自动化测试（在 uv 环境中运行）
yarn server:test
yarn --cwd web test --runInBand

# 前端生产构建，产物位于 web/build/
yarn --cwd web build
```

### 启动后注意

- 修改根目录 `.env.local` 后需重启服务端；修改 `web/.env.local` 后需重启前端。
- 修改场景 JSON 后重启服务端并刷新页面；服务端重启后旧 SessionID 失效，也需刷新。
- 前端构建不包含 Python 服务，页面运行时仍需访问服务端。
- 改服务端端口：根目录 `.env.local` 的 `PORT`，并同步 `web/.env.local` 的 `REACT_APP_AIGC_PROXY_HOST`。
- 不要在跑 `yarn dev` 的 shell 里统一设 `PORT`，否则前后端会抢同一个端口。
- OpenAPI 版本 `2025-06-01`、签名 region/service 写在 `server/app.py`，无需环境变量。
- 同一页面同时只能有一个场景通话；切换场景前先挂断。

### 常见问题

| 问题                                                                                                                          | 解决方案                                                                                                                                                                                                                                                                                                                                     |
| :---------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 如何使用第三方模型、Coze Bot                                                                                                  | 在 `server/scenes/` 下的 JSON 填写对应模型参数，并把第三方 secret 放进 `.env.local` 后通过 `${ENV_NAME}` 引用。                                                                                                                                                                                                                              |
| **启动后对话无反馈，或一直停留在“AI 准备中”“数字人准备中”** | 检查页面错误和服务端日志，按 [VoiceChat 参数说明](https://docs.volcengine.com/docs/6348/2123348) 核对字段大小写、类型及配置值。数字人场景还需检查数字人 AppId、Token 和并发限制。 |
| **浏览器报了 `token_error` 错误** | 检查根目录 `.env.local` 中 RTC_APP_ID 与 RTC_APP_KEY 是否属于同一应用；Token 由服务端生成，不需要手填。修改凭证后重启服务端并刷新页面；页面长时间未刷新时也应重新获取 Token。 |
| **StartVoiceChat 提示任务已启动** | 正常页面流程会合并同一 session 的重复启动请求。若自行修改了请求逻辑，检查是否重复调用云端接口，并使用对应的 AppId、RoomId、TaskId 停止原任务后再启动；不要通过在场景 JSON 中修改 RoomId 或 TaskId 排查，它们由服务端生成。 |
| 为什么麦克风、摄像头开启失败？浏览器报了`TypeError: Cannot read properties of undefined (reading 'getUserMedia')`             | 检查当前页面是否为[安全上下文](https://developer.mozilla.org/zh-CN/docs/Web/Security/Secure_Contexts)（简单来说，检查当前页面是否为 `localhost` 或者 是否为 https 协议）。浏览器[限制](https://developer.mozilla.org/zh-CN/docs/Web/Security/Secure_Contexts/features_restricted_to_secure_contexts) `getUserMedia` 只能在安全上下文中使用。 |
| 为什么我的麦克风正常、摄像头也正常，但是设备没有正常工作?                                                                     | 可能是设备权限未授予，详情可参考 [Web 排查设备权限获取失败问题](https://www.volcengine.com/docs/6348/1356355?s=g)。                                                                                                                                                                                                                          |
| 接口调用时, 返回 "Invalid 'Authorization' header, Pls check your authorization header" 错误                                   | 检查 `.env.local` 中成对配置的 AK/SK 及其权限。                                                                                                                                                                                                                                                                   |
| 什么是 RTC                                                                                                                    | **R**eal **T**ime **C**ommunication, RTC 的概念可参考[官网文档](https://www.volcengine.com/docs/6348/66812?s=g)。                                                                                                                                                                                                                            |
| 不清楚什么是主账号，什么是子账号                                                                                              | 可以参考[官方概念](https://www.volcengine.com/docs/6257/64963?hyperlink_open_type=lark.open_in_browser&s=g) 。                                                                                                                                                                                                                               |
| 我有自己的服务端，如何接入？ | 在 `web/.env.local` 设置 `REACT_APP_AIGC_PROXY_HOST`，重启前端，生产构建则需重新构建。保持 [服务端接口约定](server/README.md#接口约定) 一致；接口路径或响应格式不同时，再调整 `web/src/app/` 下的请求适配。 |

如果有上述以外的问题，欢迎联系我们反馈。

### 相关文档

- [场景介绍](https://www.volcengine.com/docs/6348/1310537?s=g)
- [Demo 体验](https://www.volcengine.com/docs/6348/1310559?s=g)
- [场景搭建方案](https://www.volcengine.com/docs/6348/1310560?s=g)

## Security and privacy

This project takes security seriously.
For vulnerability reporting and supported versions, see [SECURITY.md](SECURITY.md)

## 更新日志

### OpenAPI 更新

参考 [OpenAPI 更新](https://www.volcengine.com/docs/6348/1544162) 中与 实时对话式 AI 相关的更新内容。

### Demo 更新

#### [1.6.0]

- 2026-10-14
  - 更新 RTC Web SDK 至 `4.69.3`。

- 2026-09-11
  - 更新 VoiceChat OpenAPI 至 `2025-06-01`，RTC Web SDK 至 `4.68.1`。
  - 拆分 `web/` 与 `server/`，支持根目录 `yarn dev` 同时启动。
  - 凭证改为环境变量配置，按页面 session 隔离通话，支持可选 BusinessId。

- 2025-09-30
  - 更新数字人场景相关配置

- 2025-07-08
  - 更新 RTC Web SDK 版本至 4.66.20
- 2025-06-26
  - 修复进房有问题的 BUG
- 2025-06-23
  - 简化 Demo 使用, 配置归一化。
  - 删除无用组件。
  - 追加服务端 README。
- 2025-06-18
  - 更新 RTC Web SDK 版本至 4.66.16
  - 更新 UI 和参数配置方式
  - 更新 Readme 文档
  - 追加 Node 服务的参数检测能力
  - 追加 Node 服务的 Token 生成能力
