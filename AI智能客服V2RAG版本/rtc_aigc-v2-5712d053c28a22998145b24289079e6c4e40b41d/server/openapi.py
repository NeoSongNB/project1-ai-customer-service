# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""火山 OpenAPI HMAC-SHA256 签名（对齐官方 @volcengine/openapi / 控制台签名规则）。

本仓库两处会用到：
  1. RTC VoiceChat：service=rtc，sign_content_type=False（content-type 不进签名）
  2. 知识库检索：service=air，sign_content_type=True（历史实现会把 content-type 签进去）

签名流程简述：
  canonical_request → string_to_sign → 派生密钥 kSigning → Signature
  最终 Authorization: HMAC-SHA256 Credential=.../SignedHeaders=.../Signature=...
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping
from urllib.parse import quote


# 这些头默认不参与签名；知识库路径可通过 sign_content_type=True 把 content-type 加回去
UNSIGNABLE_HEADERS = {
    "authorization",
    "content-type",
    "content-length",
    "user-agent",
    "presigned-expires",
    "expect",
}


def dump_json_body(body: Any) -> str:
    """序列化请求体；必须与真正发出去的字节一致，否则签名校验失败。

    使用紧凑分隔符（无空格），对齐 Node JSON.stringify 默认行为。
    """
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _uri_escape(value: str) -> str:
    # RFC3986 未保留字符保持原样；* 额外编码（与官方 SDK 一致）
    return quote(str(value), safe="-_.~").replace("*", "%2A")


def _query_params_to_string(params: Mapping[str, str]) -> str:
    """Query 按 key 字典序拼接，参与 canonical_request。"""
    parts = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        parts.append(f"{_uri_escape(key)}={_uri_escape(value)}")
    return "&".join(parts)


def sign_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    body: Any,
    access_key_id: str,
    secret_key: str,
    region: str,
    service: str,
    content_type: str = "application/json",
    sign_content_type: bool = False,
) -> Dict[str, str]:
    """生成 Host / Content-type / X-Date / X-Content-Sha256 / Authorization。

    RTC OpenAPI：content-type 不参与签名（默认）。
    知识库 air：历史实现包含 content-type，传 sign_content_type=True。
    """
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    body_str = dump_json_body(body)
    payload_hash = _sha256_hex(body_str)

    headers = {
        "Host": host,
        "Content-type": content_type,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
    }

    unsignable = set(UNSIGNABLE_HEADERS)
    if sign_content_type:
        unsignable.discard("content-type")
    signed_header_names = sorted(
        key.lower() for key in headers if key.lower() not in unsignable
    )
    canonical_header_lines = []
    for name in signed_header_names:
        value = next(v for k, v in headers.items() if k.lower() == name)
        # 连续空白折叠成单个空格（签名规范要求）
        canonical_value = " ".join(str(value).split())
        canonical_header_lines.append(f"{name}:{canonical_value}")

    canonical_request = "\n".join(
        [
            method.upper(),
            path or "/",
            _query_params_to_string(query),
            "\n".join(canonical_header_lines) + "\n",
            ";".join(signed_header_names),
            payload_hash,
        ]
    )
    credential_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            x_date,
            credential_scope,
            _sha256_hex(canonical_request),
        ]
    )
    # 四级密钥派生：SK → kDate → kRegion → kService → kSigning
    k_date = _hmac_sha256(secret_key.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={';'.join(signed_header_names)}, Signature={signature}"
    )
    return headers
