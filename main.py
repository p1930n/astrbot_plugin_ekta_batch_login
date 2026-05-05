from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .commands import EktaCommandService
from .domain import CsvAccountStore, EktaSettings
from .runtime import EktaNodeRunner, EktaTaskQueue, PendingImageSessions


PLUGIN_NAME = "astrbot_plugin_ekta_batch_login"
PLUGIN_AUTHOR = "p1930n"
PLUGIN_DESCRIPTION = "第二课堂活动报名与签到二维码批量处理插件。"
PLUGIN_VERSION = "0.1.0"
PLUGIN_REPO = ""


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class EktaBatchLoginPlugin(Star):
    def __init__(self, context: Context, config: Any | None = None) -> None:
        super().__init__(context)
        self._context = context
        plugin_root = Path(__file__).resolve().parent
        data_root = plugin_root.parents[1]
        settings = EktaSettings.from_config(config, data_root=data_root)
        runner = EktaNodeRunner(
            script_path=plugin_root / "vendor" / "ekta_batch_login.js",
            fallback_node_modules=data_root / "ekta_batch_login" / "node_modules",
        )
        self._pending_sessions = PendingImageSessions()
        self._task_queue = EktaTaskQueue(
            settings=settings,
            runner=runner,
            send_text=self._send_text,
        )
        self._task_queue.start()
        self._commands = EktaCommandService(
            context=context,
            settings=settings,
            runner=runner,
            account_store=CsvAccountStore(settings.accounts_csv),
            pending_sessions=self._pending_sessions,
            task_queue=self._task_queue,
        )

    async def terminate(self) -> None:
        self._pending_sessions.clear()
        await self._task_queue.shutdown()

    @filter.command_group("ekta")
    def ekta():
        pass

    @ekta.command("add")
    async def ekta_add(self, event: AstrMessageEvent, mode: str = ""):
        yield event.plain_result(await self._handle_command(event, self._commands.add, mode))

    @ekta.command("help")
    async def ekta_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._commands.help())

    @ekta.command("account")
    async def ekta_account(
        self,
        event: AstrMessageEvent,
        action: str = "",
        account: str = "",
        password: str = "",
    ):
        yield event.plain_result(
            await self._handle_command(
                event,
                self._commands.account,
                action,
                account,
                password,
            )
        )

    @ekta.command("status")
    async def ekta_status(self, event: AstrMessageEvent):
        yield event.plain_result(await self._commands.status())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        try:
            result = await self._commands.handle_pending_message(event)
        except Exception as exc:
            logger.error("[EktaBatchLogin] pending message failed: %s", exc, exc_info=True)
            result = "第二课堂任务执行失败。"
        if result:
            event.stop_event()
            yield event.plain_result(result)

    async def _handle_command(
        self,
        event: AstrMessageEvent,
        handler: Any,
        *args: str,
    ) -> str:
        try:
            return await handler(event, *args)
        except Exception as exc:
            logger.error("[EktaBatchLogin] command failed: %s", exc, exc_info=True)
            return "第二课堂命令执行失败。"

    async def _send_text(self, origin: str, text: str) -> None:
        try:
            from astrbot.api.event import MessageChain

            message = MessageChain().message(text)
        except Exception:
            message = text
        await self._context.send_message(origin, message)
