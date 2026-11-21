# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""解析 CustomLLM 回调用的公网 Origin（不含 path）。

RTC 云端必须能 HTTP 访问本服务的 /api/chat_callback。
本地开发通常用 ngrok 得到 https://xxxx.ngrok-free.app，写入 SERVER_URL。

规则：
  1. 优先用环境变量 SERVER_URL（去掉末尾 /）
  2. 未配置时，仅允许 localhost / 127.0.0.1 / ::1 / RFC1918 私网作为回退
  3. 公网 IP/域名回退一律拒绝，强制显式配置 SERVER_URL，避免误把内网地址交给云端
"""

from __future__ import annotations

import ipaddress
from typing import Mapping, Optional
from urllib.parse import urlsplit


def resolve_server_url(
    env: Mapping[str, str],
    fallback_url: Optional[str] = None,
) -> str:
    """返回绝对 Origin，例如 https://abcd.ngrok-free.app（不要带 /api/...）。"""
    configured = (env.get("SERVER_URL") or "").strip().rstrip("/")
    if configured:
        return configured

    if not fallback_url:
        raise ValueError("CustomLLM 场景需要配置 SERVER_URL（RTC 云端可访问的公网 Origin）")

    try:
        if "\\" in fallback_url:
            raise ValueError
        parsed = urlsplit(fallback_url.rstrip("/"))
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        hostname = parsed.hostname
        port = parsed.port
        if hostname is None:
            raise ValueError
        if hostname == "localhost":
            normalized_host = hostname
            address = None
        else:
            address = ipaddress.ip_address(hostname)
            normalized_host = address.compressed
    except ValueError as exc:
        raise ValueError("不安全的服务地址，请配置 SERVER_URL") from exc

    if address is not None:
        allowed_addresses = {
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("::1"),
        }
        private_networks = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        if address not in allowed_addresses and not any(
            address in network for network in private_networks
        ):
            raise ValueError("不安全的服务地址，请配置 SERVER_URL")
        if address.version == 6:
            normalized_host = f"[{normalized_host}]"

    normalized_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{normalized_host}{normalized_port}"
