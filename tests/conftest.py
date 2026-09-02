"""共享测试夹具。"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest

from tg_assistant.logging_setup import account_logger, configure_logging
from tg_assistant.paths import Paths
from tg_assistant.store import Store


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(data_dir=tmp_path / "data").ensure()


@pytest.fixture
def store(paths: Paths) -> Store:
    return Store(paths).bootstrap()


@pytest.fixture(autouse=True)
def _logging(paths: Paths):
    """把日志写到临时目录，避免污染仓库，同时保证 alog 可用。"""
    configure_logging(paths.log_dir, "DEBUG", console=False)
    yield
    import logging

    logger = logging.getLogger("tg-assistant")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def alog():
    return account_logger("test-account")


# --------------------------------------------------------------------------- #
# 轻量消息替身
#
# 不引入 pyrogram 的真实类型：构造它们需要 client 实例与大量必填字段，
# 而被测代码只通过 getattr 读属性，鸭子类型足够且更快。
# --------------------------------------------------------------------------- #
class FakeChat:
    def __init__(
        self,
        chat_id: int,
        title: Optional[str] = None,
        username: Optional[str] = None,
        chat_type: str = "supergroup",
    ) -> None:
        self.id = chat_id
        self.title = title
        self.username = username
        self.type = types.SimpleNamespace(value=chat_type)
        self.first_name = None
        self.last_name = None


class FakeUser:
    def __init__(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: str = "张",
        last_name: Optional[str] = "三",
        is_self: bool = False,
        is_bot: bool = False,
    ) -> None:
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.is_self = is_self
        self.is_bot = is_bot


class FakeButton:
    def __init__(
        self, text: str, callback_data: Optional[bytes] = None, url: Optional[str] = None
    ) -> None:
        self.text = text
        self.callback_data = callback_data
        self.url = url
        self.web_app = None
        self.login_url = None
        self.switch_inline_query = None
        self.callback_game = None


class FakeMarkup:
    def __init__(self, rows: list[list[FakeButton]], inline: bool = True) -> None:
        if inline:
            self.inline_keyboard = rows
            self.keyboard = None
        else:
            self.inline_keyboard = None
            self.keyboard = rows


#: 用于区分"没传 sender（用默认值）"和"显式要求没有 from_user（频道消息）"
DEFAULT_SENDER = object()


def make_message(
    text: Optional[str] = None,
    *,
    message_id: int = 100,
    chat: Optional[FakeChat] = None,
    sender: Any = DEFAULT_SENDER,
    sender_chat: Optional[FakeChat] = None,
    caption: Optional[str] = None,
    markup: Optional[FakeMarkup] = None,
    media_group_id: Optional[str] = None,
    service: Any = None,
    date: Any = None,
    **extra: Any,
) -> Any:
    if sender is DEFAULT_SENDER:
        sender = FakeUser(555, username="alice")
    message = types.SimpleNamespace(
        id=message_id,
        text=text,
        caption=caption,
        chat=chat or FakeChat(-1001234567890, title="测试群", username="testgroup"),
        from_user=sender,
        sender_chat=sender_chat,
        reply_markup=markup,
        media=None,
        media_group_id=media_group_id,
        service=service,
        date=date,
        entities=None,
        caption_entities=None,
        message_thread_id=None,
        outgoing=False,
        photo=None,
        video=None,
        document=None,
        audio=None,
        animation=None,
        voice=None,
        sticker=None,
        video_note=None,
        poll=None,
        location=None,
        contact=None,
        web_page=None,
    )
    for key, value in extra.items():
        setattr(message, key, value)
    return message


class FakeClient:
    """记录调用而不真的联网。"""

    def __init__(
        self,
        *,
        callback_answer: Optional[str] = None,
        callback_error: Optional[BaseException] = None,
        send_error: Optional[BaseException] = None,
        forward_error: Optional[BaseException] = None,
    ) -> None:
        self.me = FakeUser(1, username="me", is_self=True)
        self.callback_answer = callback_answer
        self.callback_error = callback_error
        self.send_error = send_error
        self.forward_error = forward_error
        self.sent: list[dict[str, Any]] = []
        self.forwarded: list[dict[str, Any]] = []
        self.callbacks: list[dict[str, Any]] = []
        self.handlers: list[tuple[Any, int]] = []
        self.next_message_id = 9000

    # --- handler 管理 ---
    def add_handler(self, handler: Any, group: int = 0) -> tuple[Any, int]:
        self.handlers.append((handler, group))
        return handler, group

    def remove_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers = [h for h in self.handlers if h[0] is not handler]

    # --- API ---
    async def send_message(self, **kwargs: Any) -> Any:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(kwargs)
        self.next_message_id += 1
        return types.SimpleNamespace(id=self.next_message_id)

    async def forward_messages(self, **kwargs: Any) -> Any:
        if self.forward_error is not None:
            raise self.forward_error
        self.forwarded.append(kwargs)
        ids = kwargs.get("message_ids")
        if isinstance(ids, (list, tuple)):
            result = []
            for _ in ids:
                self.next_message_id += 1
                result.append(types.SimpleNamespace(id=self.next_message_id))
            return result
        self.next_message_id += 1
        return types.SimpleNamespace(id=self.next_message_id)

    async def request_callback_answer(self, **kwargs: Any) -> Any:
        self.callbacks.append(kwargs)
        if self.callback_error is not None:
            raise self.callback_error
        return types.SimpleNamespace(message=self.callback_answer)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def pytest_configure() -> None:
    # 让 `pytest` 在未安装包时也能从仓库根目录跑起来
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
