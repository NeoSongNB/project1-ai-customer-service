# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""通用小工具：读场景、深拷贝、展开 ${ENV}、去掉 None 字段。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional


# 仅匹配「整段字符串就是一个环境变量引用」的形式，例如 "${ARK_API_KEY}"
# 不会展开 "${SERVER_URL}/api/chat_callback" 这种带后缀的字符串（由 apply_custom_llm_url 整段覆盖）
ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def read_scene_files(scenes_dir: Path) -> Dict[str, Any]:
    """读取 scenes 目录下所有 .json；文件名（不含后缀）作为 SceneID。"""
    scenes: Dict[str, Any] = {}
    for path in sorted(scenes_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as handle:
            scenes[path.stem] = json.load(handle)
    return scenes


def assert_ok(expression: Any, msg: str) -> None:
    """业务校验：假值，或字符串里含空格，都视为失败（与旧 Node Demo 行为一致）。"""
    if not expression or (isinstance(expression, str) and " " in expression):
        print(f"\033[31m校验失败: {msg}\033[0m")
        raise ValueError(msg)


def deep_clone(value: Any) -> Any:
    """JSON round-trip 深拷贝，避免改运行时对象时写回场景模板。"""
    return json.loads(json.dumps(value))


def expand_env_references(value: Any, env: Mapping[str, str]) -> Any:
    """递归展开场景配置里的 ${ENV_NAME}；缺变量则抛错。"""
    if isinstance(value, list):
        return [expand_env_references(item, env) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_references(item, env) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    match = ENV_REF.match(value)
    if not match:
        return value
    name = match.group(1)
    if name not in env:
        raise ValueError(f"缺少环境变量 {name}")
    return env[name]


def omit_none(data: MutableMapping[str, Any]) -> Dict[str, Any]:
    """序列化前丢掉 None，避免 BusinessId: null 被发给前端/云端。"""
    return {key: value for key, value in data.items() if value is not None}
