# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

"""RTC 进房 AccessToken 序列化（从 Node Demo 移植）。

前端 joinRoom 需要：AppId + RoomId + UserId + Token。
Token 由服务端用 AppKey 对「房间/用户/权限」消息做 HMAC 后 Base64 编码。

格式：VERSION(3) + AppId(24) + Base64( msg_len + msg + sig_len + sig )

注意：privileges 的 TreeMap 必须按 key 数字升序写入。
若按插入顺序序列化，云端验签会报 INVALID_TOKEN。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import struct
import time
from typing import Dict, Optional

VERSION = "001"
VERSION_LENGTH = 3
APP_ID_LENGTH = 24

# 权限位：PrivPublishStream 会自动展开为音/视/数三个子权限
PRIV_PUBLISH_STREAM = 0
PRIV_PUBLISH_AUDIO_STREAM = 1
PRIV_PUBLISH_VIDEO_STREAM = 2
PRIV_PUBLISH_DATA_STREAM = 3
PRIV_SUBSCRIBE_STREAM = 4


class Privileges:
    PrivPublishStream = PRIV_PUBLISH_STREAM
    PrivSubscribeStream = PRIV_SUBSCRIBE_STREAM


def _encode_hmac(key: str, message: bytes) -> bytes:
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()


class ByteBuf:
    """小端二进制缓冲，对齐官方 Token 打包格式。"""

    def __init__(self) -> None:
        self._buffer = bytearray(1024)
        self._position = 0

    def pack(self) -> bytes:
        return bytes(self._buffer[: self._position])

    def put_uint16(self, value: int) -> "ByteBuf":
        struct.pack_into("<H", self._buffer, self._position, value)
        self._position += 2
        return self

    def put_uint32(self, value: int) -> "ByteBuf":
        struct.pack_into("<I", self._buffer, self._position, value & 0xFFFFFFFF)
        self._position += 4
        return self

    def put_bytes(self, data: bytes) -> "ByteBuf":
        self.put_uint16(len(data))
        end = self._position + len(data)
        if end > len(self._buffer):
            self._buffer.extend(bytearray(end - len(self._buffer) + 1024))
        self._buffer[self._position : end] = data
        self._position = end
        return self

    def put_string(self, value: str) -> "ByteBuf":
        return self.put_bytes(value.encode("utf-8"))

    def put_tree_map_uint32(self, mapping: Optional[Dict[int, int]]) -> "ByteBuf":
        """写入 privilege → expire_at 映射；key 必须数字升序（等同 Java TreeMap）。"""
        if not mapping:
            self.put_uint16(0)
            return self
        keys = sorted((int(key) for key in mapping.keys()))
        self.put_uint16(len(keys))
        for key in keys:
            self.put_uint16(key)
            self.put_uint32(int(mapping[key]))
        return self


class AccessToken:
    def __init__(self, app_id: str, app_key: str, room_id: str, user_id: str) -> None:
        self.app_id = app_id
        self.app_key = app_key
        self.room_id = room_id
        self.user_id = user_id
        self.issued_at = int(time.time())
        self.nonce = random.randint(0, 0xFFFFFFFF)
        self.expire_at = 0
        self.privileges: Dict[int, int] = {}

    def add_privilege(self, privilege: int, expire_timestamp: int) -> None:
        self.privileges[privilege] = expire_timestamp
        if privilege == PRIV_PUBLISH_STREAM:
            # 发布流权限隐含音/视/数据三个子权限，过期时间相同
            self.privileges[PRIV_PUBLISH_VIDEO_STREAM] = expire_timestamp
            self.privileges[PRIV_PUBLISH_AUDIO_STREAM] = expire_timestamp
            self.privileges[PRIV_PUBLISH_DATA_STREAM] = expire_timestamp

    def expire_time(self, expire_timestamp: int) -> None:
        self.expire_at = expire_timestamp

    def pack_msg(self) -> bytes:
        buf = ByteBuf()
        buf.put_uint32(self.nonce)
        buf.put_uint32(self.issued_at)
        buf.put_uint32(self.expire_at)
        buf.put_string(self.room_id)
        buf.put_string(self.user_id)
        buf.put_tree_map_uint32(self.privileges)
        return buf.pack()

    def serialize(self) -> str:
        msg = self.pack_msg()
        signature = _encode_hmac(self.app_key, msg)
        content = ByteBuf().put_bytes(msg).put_bytes(signature).pack()
        return VERSION + self.app_id + base64.b64encode(content).decode("utf-8")
