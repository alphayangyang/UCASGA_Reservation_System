from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from botpy.http import Route

PutPart = Callable[[str, bytes, int], Awaitable[None]]
MD5_10M_BYTES = 10_002_432
MAX_MEDIA_BYTES = 200 * 1024 * 1024


class QQOpenAPIRoute(Route):
    """qq-botpy 1.2.1 仍使用旧域名；新富媒体接口使用当前统一域名。"""

    DOMAIN = "api.bot.qq.com"


class QQMediaUploader:
    """QQ 群本地文件分片上传适配器。"""

    def __init__(self, api: Any, put_part: PutPart | None = None) -> None:
        self.api = api
        self._custom_put_part = put_part

    @staticmethod
    def _md5(value: bytes) -> str:
        return hashlib.md5(value, usedforsecurity=False).hexdigest()

    @staticmethod
    def _route(method: str, path: str, group_openid: str) -> QQOpenAPIRoute:
        return QQOpenAPIRoute(
            method,
            path,
            group_openid=group_openid,
        )

    async def _request(
        self,
        method: str,
        path: str,
        group_openid: str,
        payload: dict[str, Any],
    ) -> Any:
        return await self.api._http.request(
            self._route(method, path, group_openid),
            json=payload,
        )

    async def _put_part(self, url: str, content: bytes, retry_delay: int) -> None:
        if self._custom_put_part is not None:
            await self._custom_put_part(url, content, retry_delay)
            return

        timeout = aiohttp.ClientTimeout(total=300)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.put(
                        url,
                        data=content,
                        headers={"Content-Type": "application/octet-stream"},
                    ) as response:
                        if 200 <= response.status < 300:
                            return
                        body = (await response.text())[:200]
                        raise RuntimeError(f"QQ 分片存储返回 HTTP {response.status}: {body}")
            except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(max(0, retry_delay))
        raise RuntimeError("QQ 图片分片上传失败") from last_error

    async def upload_image(
        self,
        group_openid: str,
        content: bytes,
        file_name: str = "schedule.png",
    ) -> dict[str, str]:
        if not content:
            raise ValueError("不能上传空图片")
        if len(content) > MAX_MEDIA_BYTES:
            raise ValueError("图片超过 QQ 富媒体 200 MB 上限")

        prepare = await self._request(
            "POST",
            "/v2/groups/{group_openid}/upload_prepare",
            group_openid,
            {
                "file_type": 1,
                "file_size": str(len(content)),
                "file_name": file_name,
                "md5": self._md5(content),
                "sha1": hashlib.sha1(content, usedforsecurity=False).hexdigest(),
                "md5_10m": self._md5(content[:MD5_10M_BYTES]),
            },
        )
        if not isinstance(prepare, dict) or not prepare.get("upload_id"):
            raise RuntimeError("QQ 富媒体预上传未返回 upload_id")

        upload_id = str(prepare["upload_id"])
        try:
            block_size = int(prepare["block_size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("QQ 富媒体预上传未返回有效 block_size") from exc
        if block_size <= 0:
            raise RuntimeError("QQ 富媒体预上传返回了无效 block_size")

        upload_config = prepare.get("upload_config") or {}
        retry_delay = min(10, max(0, int(upload_config.get("retry_delay", 1))))
        parts = prepare.get("parts") or []
        # 手册 25.1：服务端 part_index 只作协议编号原样回传 part_finish；
        # 本地切片位置用独立 cursor——兼容服务端 index 从 0 或 1 开始（曾跳过首块、末片越界）。
        cursor = 0
        for part in sorted(parts, key=lambda item: int(item["index"])):
            part_index = int(part["index"])
            expected_size = int(part.get("block_size") or block_size)
            chunk = content[cursor : cursor + expected_size]
            if not chunk:
                raise RuntimeError(f"QQ 富媒体分片 {part_index} 超出文件范围")
            cursor += len(chunk)
            presigned_url = str(part.get("presigned_url") or "")
            if not presigned_url:
                raise RuntimeError(f"QQ 富媒体分片 {part_index} 缺少上传地址")

            await self._put_part(presigned_url, chunk, retry_delay)
            await self._request(
                "POST",
                "/v2/groups/{group_openid}/upload_part_finish",
                group_openid,
                {
                    "upload_id": upload_id,
                    "part_index": part_index,
                    "block_size": str(len(chunk)),
                    "md5": self._md5(chunk),
                },
            )

        if cursor != len(content):
            raise RuntimeError("QQ 分片上传字节数不完整（服务端分片表与文件长度不一致）")

        merged = await self._request(
            "POST",
            "/v2/groups/{group_openid}/files",
            group_openid,
            {
                "file_type": 1,
                "srv_send_msg": False,
                "file_name": file_name,
                "upload_id": upload_id,
            },
        )
        if not isinstance(merged, dict) or not merged.get("file_info"):
            raise RuntimeError("QQ 富媒体合并未返回 file_info")
        return {"file_info": str(merged["file_info"])}
