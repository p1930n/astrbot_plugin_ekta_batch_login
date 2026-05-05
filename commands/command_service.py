from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent

from ..domain.accounts import AccountCsvError, CsvAccountStore
from ..domain.settings import EktaSettings
from ..platform.event_adapter import EventSnapshot, dehydrate_event, extract_image_sources
from ..runtime.node_runner import EktaNodeDependencyError, EktaNodeRunner
from ..runtime.pending_sessions import PendingImageSessions
from ..runtime.task_queue import EktaTaskQueue


HELP_TEXT = "\n".join(
    (
        ".ekta add [--dry-run] - 解析本条图片并加入后台队列",
        ".ekta add - 本条无图片时，等待同一用户下一条图片消息",
        ".ekta account add <account> <password> - 添加或更新本地账号 CSV",
        ".ekta account delete <account> - 删除本地账号",
        ".ekta account list - 显示本地账号和密码",
        ".ekta status - 查看账号 CSV、执行器和队列状态",
    )
)
PERMISSION_DENIED = "需要 AstrBot 管理员或群管理员权限。"
NO_IMAGE_WAITING = "未检测到图片，请在 {seconds} 秒内发送一条带二维码图片的消息。"
NO_IMAGE_CANCELLED = "未收到图片，已取消本次第二课堂任务。"
NO_ACCOUNTS = "账号 CSV 不存在或不可读，请先配置 accounts_csv。"
ACCOUNT_USAGE = "\n".join(
    (
        "用法:",
        ".ekta account add <account> <password>",
        ".ekta account delete <account>",
        ".ekta account list",
    )
)


class EktaCommandService:
    def __init__(
        self,
        *,
        context: Any,
        plugin_version: str,
        settings: EktaSettings,
        runner: EktaNodeRunner,
        account_store: CsvAccountStore,
        pending_sessions: PendingImageSessions,
        task_queue: EktaTaskQueue,
    ) -> None:
        self._context = context
        self._plugin_version = plugin_version
        self._settings = settings
        self._runner = runner
        self._account_store = account_store
        self._pending_sessions = pending_sessions
        self._task_queue = task_queue

    async def add(self, event: AstrMessageEvent, mode: str = "") -> str:
        snapshot = dehydrate_event(event)
        if not self._can_use(snapshot):
            return PERMISSION_DENIED

        dry_run = self._is_dry_run_requested(snapshot.message_text, mode)
        images = extract_image_sources(event)
        if not images:
            self._pending_sessions.create(
                snapshot,
                timeout_seconds=self._settings.pending_timeout_seconds,
                dry_run=dry_run,
            )
            return NO_IMAGE_WAITING.format(
                seconds=self._settings.pending_timeout_seconds,
            )
        return await self._enqueue_images(event, images, dry_run=dry_run)

    async def handle_pending_message(self, event: AstrMessageEvent) -> str | None:
        self._pending_sessions.clear_expired()
        snapshot = dehydrate_event(event)
        if _is_ekta_command_text(snapshot.message_text):
            return None
        session = self._pending_sessions.consume_for(snapshot)
        if session is None:
            return None

        images = extract_image_sources(event)
        if not images:
            return NO_IMAGE_CANCELLED
        return await self._enqueue_images(event, images, dry_run=session.dry_run)

    def help(self) -> str:
        return HELP_TEXT

    async def account(
        self,
        event: AstrMessageEvent,
        action: str = "",
        account: str = "",
        password: str = "",
    ) -> str:
        snapshot = dehydrate_event(event)
        if not self._can_use(snapshot):
            return PERMISSION_DENIED

        normalized_action = action.strip().casefold()
        try:
            if normalized_action == "add":
                return await self._account_add(account, password)
            if normalized_action in {"delete", "del", "remove", "rm"}:
                return await self._account_delete(account)
            if normalized_action in {"list", "ls"}:
                return await self._account_list()
        except AccountCsvError as exc:
            return str(exc)
        return ACCOUNT_USAGE

    async def status(self) -> str:
        accounts_status = "存在" if self._settings.accounts_csv.is_file() else "缺失"
        mode = "dry-run" if self._settings.dry_run_by_default else "真实提交"
        node_dependency_status = await self._runner.node_dependency_status(
            self._settings,
        )
        queue_status = await self._task_queue.status()
        return "\n".join(
            (
                "第二课堂批量插件状态",
                f"插件版本: {self._plugin_version}",
                "图片格式: JPG/PNG",
                f"Node 依赖: {node_dependency_status}",
                f"账号 CSV: {accounts_status}",
                f"最大账号数: {self._settings.max_accounts_per_run}",
                f"最大图片数: {self._settings.max_images_per_run}",
                f"默认模式: {mode}",
                queue_status,
            )
        )

    async def _account_add(self, account: str, password: str) -> str:
        if not account or not password:
            return "缺少账号或密码。\n" + ACCOUNT_USAGE
        result = await self._account_store.add(account, password)
        action = "已更新" if result.existed else "已添加"
        return f"{action}账号 {result.account}，当前账号数 {result.count}。"

    async def _account_delete(self, account: str) -> str:
        if not account:
            return "缺少账号。\n" + ACCOUNT_USAGE
        result = await self._account_store.delete(account)
        if not result.existed:
            return f"账号 {result.account} 不存在，当前账号数 {result.count}。"
        return f"已删除账号 {result.account}，当前账号数 {result.count}。"

    async def _account_list(self) -> str:
        result = await self._account_store.list_accounts()
        if result.total == 0:
            return "账号 CSV 为空。"
        lines = [f"账号列表（共 {result.total} 个，显示密码）"]
        lines.extend(
            f"{index}. {item.code}, {item.password}"
            for index, item in enumerate(result.accounts, start=1)
        )
        if result.omitted:
            lines.append(
                f"... 其余 {result.omitted} 个账号未显示，单次最多显示 {result.display_limit} 个。"
            )
        return "\n".join(lines)

    async def _enqueue_images(
        self,
        event: AstrMessageEvent,
        image_sources: tuple[str, ...],
        *,
        dry_run: bool,
    ) -> str:
        if not self._settings.accounts_csv.is_file():
            return NO_ACCOUNTS
        if len(image_sources) > self._settings.max_images_per_run:
            return f"图片数量超过上限，本次最多处理 {self._settings.max_images_per_run} 张。"

        origin = _event_origin(event)
        if not origin:
            return "无法获取当前会话，不能创建后台队列任务。"

        try:
            payload = await self._runner.decode_images(
                settings=self._settings,
                image_sources=image_sources,
            )
        except EktaNodeDependencyError as exc:
            return str(exc)
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        if not tasks:
            return _format_no_qr_tasks(payload)

        effective_dry_run = dry_run or self._settings.dry_run_by_default
        enqueue_result = await self._task_queue.enqueue(
            origin=origin,
            tasks=tasks,
            dry_run=effective_dry_run,
        )
        if not enqueue_result.accepted:
            return enqueue_result.message

        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        mode = "dry-run" if effective_dry_run else "真实提交"
        return "\n".join(
            (
                f"{enqueue_result.message}（{mode}）",
                (
                    "本次识别: "
                    f"活动码 {summary.get('activityTaskCount', 0)}，"
                    f"签到码 {summary.get('signTaskCount', 0)}，"
                    f"跳过图片 {summary.get('skippedImageCount', 0)}"
                ),
                (
                    "待处理队列: "
                    f"活动加入 {enqueue_result.activity_queue_size}，"
                    f"签到处理 {enqueue_result.sign_queue_size}"
                ),
            )
        )

    def _can_use(self, snapshot: EventSnapshot) -> bool:
        if self._is_global_admin(snapshot.sender_id):
            return True
        return self._settings.allow_group_manager and snapshot.sender_is_group_manager

    def _is_global_admin(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        config = _context_config(self._context)
        admins = _config_value(config, "admins_id")
        if admins is None:
            admins = _config_value(config, "admin_ids")
        return sender_id in _normalized_id_set(admins)

    @staticmethod
    def _is_dry_run_requested(message_text: str, mode: str) -> bool:
        tokens = {item.strip().casefold() for item in (message_text, mode) if item}
        joined = " ".join(tokens)
        return "--dry-run" in joined or "dry-run" in joined


def _is_ekta_command_text(text: str) -> bool:
    normalized = text.strip().casefold()
    return normalized.startswith((".ekta", "/ekta"))


def _format_no_qr_tasks(payload: dict[str, Any]) -> str:
    lines = ["没有识别到可处理的第二课堂二维码。"]
    skipped = payload.get("skippedImages")
    if not isinstance(skipped, list):
        return lines[0]

    detail_lines: list[str] = []
    for item in skipped[:5]:
        if not isinstance(item, dict):
            continue
        image_index = item.get("imageIndex") or "?"
        reason = str(item.get("reason") or "二维码类型未知").strip()
        detail_lines.append(f"图片 {image_index}: {reason}")
    if detail_lines:
        lines.append("跳过原因:")
        lines.extend(detail_lines)
    omitted = len(skipped) - len(detail_lines)
    if omitted > 0:
        lines.append(f"... 其余 {omitted} 张图片已省略")
    return "\n".join(lines)


def _context_config(context: Any) -> object:
    try:
        getter = getattr(context, "get_config", None)
    except Exception:
        getter = None
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _config_value(config: object, key: str) -> object:
    if config is None:
        return None
    if hasattr(config, "get"):
        try:
            value = config.get(key)
        except Exception:
            value = None
        if value is not None:
            return value
    try:
        return getattr(config, key, None)
    except Exception:
        return None


def _normalized_id_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {
            item
            for item in (part.strip() for part in value.replace(",", " ").split())
            if item
        }
    if isinstance(value, list | tuple | set | frozenset):
        return {item for item in (str(raw).strip() for raw in value) if item}
    normalized = str(value).strip()
    return {normalized} if normalized else set()


def _event_origin(event: AstrMessageEvent) -> str:
    try:
        return str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        return ""
