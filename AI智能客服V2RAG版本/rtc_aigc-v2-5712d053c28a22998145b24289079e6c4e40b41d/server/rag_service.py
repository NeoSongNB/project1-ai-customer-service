# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""火山在线知识库检索（供 CustomLLM 回调使用）。

调用链：/api/chat_callback → RagService.retrieve(用户问题) → 拼进 LLM system prompt

依赖环境变量：
  VOLCENGINE_ACCESS_KEY_ID / VOLCENGINE_SECRET_ACCESS_KEY  签名（与 RTC OpenAPI 共用）
  VOLC_ACCOUNT_ID                                         主账号 ID（请求头 V-Account-Id）
  KB_COLLECTION_NAME                                      知识库集合名
  KB_PROJECT_NAME                                         项目名，默认 default（控制台左上角项目）
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import httpx

from openapi import dump_json_body, sign_headers


KB_HOST = "api-knowledgebase.mlp.cn-beijing.volces.com"
KB_PATH = "/api/knowledge/collection/search_knowledge"
KB_REGION = "cn-north-1"
KB_SERVICE = "air"  # 知识库签名用的 service 名


class RagService:
    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        source = env if env is not None else {}
        self.ak = (source.get("VOLCENGINE_ACCESS_KEY_ID") or "").strip()
        self.sk = (source.get("VOLCENGINE_SECRET_ACCESS_KEY") or "").strip()
        self.collection_name = (source.get("KB_COLLECTION_NAME") or "").strip()
        # 对应控制台「项目」选择器里的名称，如 default / rtc_demo；不是显示文案「默认项目」
        self.project_name = (source.get("KB_PROJECT_NAME") or "default").strip() or "default"
        self.account_id = (source.get("VOLC_ACCOUNT_ID") or "").strip()

    def _configured(self) -> bool:
        return bool(
            self.ak and self.sk and self.collection_name and self.project_name and self.account_id
        )

    async def retrieve(self, query: str) -> str:
        """按用户问题检索知识库，返回拼接后的参考文本；失败或未配置时返回空串。"""
        if not self._configured():
            print("[RagService] 配置缺失（需要 VOLCENGINE AK/SK、VOLC_ACCOUNT_ID、KB_COLLECTION_NAME）")
            return ""

        body: dict[str, Any] = {
            "project": self.project_name,
            "name": self.collection_name,
            "query": query,
            "limit": 1,  # 只取最相关的一条，降低首包延迟
            "pre_processing": {
                "need_instruction": True,
                "return_token_usage": True,
                "messages": [{"role": "user", "content": query}],
            },
            "post_processing": {"get_attachment_link": True},
        }
        body_str = dump_json_body(body)
        # 知识库（air）签名历史实现会把 content-type 纳入签名；RTC OpenAPI 则不会
        headers = sign_headers(
            method="POST",
            host=KB_HOST,
            path=KB_PATH,
            query={},
            body=body_str,
            access_key_id=self.ak,
            secret_key=self.sk,
            region=KB_REGION,
            service=KB_SERVICE,
            content_type="application/json",
            sign_content_type=True,
        )
        # V-Account-Id 是知识库必填头，但不参与签名
        headers["V-Account-Id"] = self.account_id
        url = f"https://{KB_HOST}{KB_PATH}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=headers,
                    content=body_str,  # 必须与签名时的 body 字节一致
                    timeout=10.0,
                )
            if response.status_code != 200:
                print(f"[RagService] 请求失败: {response.status_code}")
                return ""

            data = response.json()
            result_list = data.get("data", {}).get("result_list", [])
            contents = [
                item.get("content", "") for item in result_list if item.get("content")
            ]
            if not contents:
                print("[RagService] 未检索到匹配的知识内容")
                return ""
            print(f"[RagService] 成功提取 {len(contents)} 条知识内容")
            return "\n\n".join(contents)
        except Exception:
            print("[RagService] 请求异常")
            return ""
