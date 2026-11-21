# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""方舟（Ark）流式对话，供 CustomLLM 回调使用。

与 ARK_API_KEY / CUSTOM_LLM_API_KEY 的区别：
  ARK_API_KEY          本服务 → 方舟推理鉴权（控制台领取）
  CUSTOM_LLM_API_KEY   RTC → 本服务 /api/chat_callback 鉴权（自行生成）

消息构造：
  system = 人设（RAG_SYSTEM_PROMPT 或默认） + 可选「参考知识库」段落
  + RTC 传来的 messages 历史
"""

from __future__ import annotations

import threading
from typing import Any, Iterator, List, Mapping, Optional

DEFAULT_SYSTEM_PROMPT = """你是一个简洁、友好的语音助手。
请优先依据【参考知识库】回答用户问题；知识库没有相关内容时，明确说明无法从资料中确认，不要编造事实。
回答尽量简短，适合语音播报。""".strip()


class LLMService:
    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        source = env if env is not None else {}
        self.api_key = (source.get("ARK_API_KEY") or "").strip()
        self.endpoint_id = (source.get("ARK_ENDPOINT_ID") or "").strip()
        self.system_prompt = (
            (source.get("RAG_SYSTEM_PROMPT") or "").strip() or DEFAULT_SYSTEM_PROMPT
        )
        self.client = None
        self._client_lock = threading.Lock()

    def _get_client(self):
        """懒加载 Ark 客户端，避免启动时强依赖方舟配置（default 场景不需要）。"""
        if self.client is None:
            with self._client_lock:
                if self.client is None:
                    if not self.api_key or not self.endpoint_id:
                        raise ValueError("ARK_API_KEY 和 ARK_ENDPOINT_ID 不能为空")
                    from volcenginesdkarkruntime import Ark

                    self.client = Ark(
                        base_url="https://ark.cn-beijing.volces.com/api/v3",
                        api_key=self.api_key,
                        timeout=1800,
                    )
        return self.client

    def chat_stream(
        self, history_messages: List[Mapping[str, Any]], rag_context: str = ""
    ) -> Iterator[Any]:
        """同步流式生成；调用方应放到线程池，避免阻塞 asyncio 事件循环。"""
        system_blocks = [self.system_prompt]
        if rag_context:
            # 明确分段，帮助模型优先遵循检索结果
            system_blocks.append(f"### 参考知识库（绝对准则）\n{rag_context.strip()}")
        messages = [{"role": "system", "content": "\n\n".join(system_blocks)}]
        messages.extend(list(history_messages))

        try:
            client = self._get_client()
            stream = client.chat.completions.create(
                model=self.endpoint_id,
                messages=messages,
                temperature=0.3,  # 偏低：回答更贴知识库，少幻觉
                stream=True,
                stream_options={"include_usage": True},
            )
            for chunk in stream:
                yield chunk
        except Exception:
            print("LLM 调用失败")
            yield None
