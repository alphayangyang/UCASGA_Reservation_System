from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from qqbot.domain.models import DateRange, OperationResult
from qqbot.interfaces.qq import client as client_module
from qqbot.interfaces.qq.client import PianoBotClient


class FakeAPI:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.calls: list[dict] = []

    async def post_group_message(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if "content" in kwargs:
            self.messages.append(kwargs["content"])


@dataclass
class FakeAuthor:
    member_openid: str


class FakeMessage:
    def __init__(self, text: str, user_id: str, api: FakeAPI) -> None:
        self.content = text
        self.group_openid = "test-group"
        self.author = FakeAuthor(user_id)
        self._api = api
        self.id = f"message-{len(api.messages)}"


def test_qq_adapter_end_to_end(yql_config, tmp_path: Path) -> None:
    api = FakeAPI()

    async def exercise() -> None:
        client = PianoBotClient(
            {"yql": yql_config},
            tmp_path / "control.db",
        )
        await client.on_group_at_message_create(FakeMessage("#绑定配置 yql", "owner-external", api))
        await client.on_group_at_message_create(FakeMessage("/绑定 张三 2024K8009926001", "normal-user", api))
        await client.on_group_at_message_create(FakeMessage("/预约 7-8.5", "normal-user", api))

    asyncio.run(exercise())

    assert "已绑定到" in api.messages[0]
    assert "绑定成功" in api.messages[1]
    assert "预约成功" in api.messages[2]


class FakeRenderer:
    available = True

    def __init__(self) -> None:
        self.results: list[OperationResult] = []

    async def render(self, result: OperationResult) -> bytes:
        self.results.append(result)
        return b"png"

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_query_result_is_rendered_uploaded_and_sent_as_media(
    yql_config,
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeUploader:
        def __init__(self, api) -> None:
            self.api = api

        async def upload_image(self, group_openid, content, file_name):
            assert group_openid == "test-group"
            assert content == b"png"
            assert file_name.endswith(".png")
            return {"file_info": "opaque"}

    monkeypatch.setattr(client_module, "QQMediaUploader", FakeUploader)
    renderer = FakeRenderer()
    api = FakeAPI()
    message = FakeMessage("/查询", "normal-user", api)
    period = DateRange(date(2026, 8, 10), date(2026, 8, 10))
    result = OperationResult.success(
        "schedule_range",
        date_range=period,
        room_ids=["yql-main"],
        days=[
            {
                "date": period.start,
                "offset": 0,
                "occupancies": [],
                "admin_view": False,
            }
        ],
    )

    async def exercise() -> None:
        client = PianoBotClient(
            {"yql": yql_config},
            tmp_path / "control.db",
            renderers={"yql": renderer},
        )
        await client._send_result(message, "yql", result, "request-1")

    asyncio.run(exercise())

    assert renderer.results == [result]
    assert api.calls == [
        {
            "group_openid": "test-group",
            "msg_type": 7,
            "msg_id": "message-0",
            "media": {"file_info": "opaque"},
        }
    ]
