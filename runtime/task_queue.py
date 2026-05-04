from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from ..domain.settings import EktaSettings
from .node_runner import EktaNodeRunner

SendText = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    accepted: bool
    message: str
    activity_queue_size: int = 0
    sign_queue_size: int = 0


@dataclass(frozen=True, slots=True)
class QueuedQrTask:
    origin: str
    task: dict[str, Any]
    dry_run: bool

    @property
    def kind(self) -> str:
        return str(self.task.get("kind") or "")


class EktaTaskQueue:
    def __init__(
        self,
        *,
        settings: EktaSettings,
        runner: EktaNodeRunner,
        send_text: SendText,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._send_text = send_text
        self._activity_queue: deque[QueuedQrTask] = deque()
        self._sign_queue: deque[QueuedQrTask] = deque()
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._stop = False
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def shutdown(self) -> None:
        self._stop = True
        self._wakeup.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            result = await asyncio.gather(self._worker_task, return_exceptions=True)
            if result and isinstance(result[0], BaseException) and not isinstance(
                result[0],
                asyncio.CancelledError,
            ):
                logger.error("[EktaBatchLogin] queue shutdown failed: %s", result[0])
        self._worker_task = None

    async def enqueue(
        self,
        *,
        origin: str,
        tasks: list[dict[str, Any]],
        dry_run: bool,
    ) -> EnqueueResult:
        queue_tasks = [
            QueuedQrTask(origin=origin, task=task, dry_run=dry_run)
            for task in tasks
            if task.get("kind") in {"activity_join", "sign"}
        ]
        if not queue_tasks:
            return EnqueueResult(False, "没有可处理的第二课堂二维码任务。")

        async with self._lock:
            pending = len(self._activity_queue) + len(self._sign_queue)
            if pending + len(queue_tasks) > self._settings.max_queued_qr_tasks:
                return EnqueueResult(
                    False,
                    f"队列已满，当前最多保留 {self._settings.max_queued_qr_tasks} 个二维码任务。",
                    len(self._activity_queue),
                    len(self._sign_queue),
                )

            for queue_task in queue_tasks:
                if queue_task.kind == "activity_join":
                    self._activity_queue.append(queue_task)
                else:
                    self._sign_queue.append(queue_task)
            activity_size = len(self._activity_queue)
            sign_size = len(self._sign_queue)
        self._wakeup.set()
        return EnqueueResult(
            True,
            "已加入第二课堂后台队列。",
            activity_size,
            sign_size,
        )

    async def status(self) -> str:
        async with self._lock:
            activity_size = len(self._activity_queue)
            sign_size = len(self._sign_queue)
        return f"队列: 活动加入 {activity_size}，签到处理 {sign_size}"

    async def _worker_loop(self) -> None:
        while not self._stop:
            queue_task = await self._take_next()
            if queue_task is None:
                continue
            await self._run_one(queue_task)

    async def _take_next(self) -> QueuedQrTask | None:
        while not self._stop:
            async with self._lock:
                if self._activity_queue:
                    return self._activity_queue.popleft()
                if self._sign_queue:
                    return self._sign_queue.popleft()
            self._wakeup.clear()
            await self._wakeup.wait()
        return None

    async def _run_one(self, queue_task: QueuedQrTask) -> None:
        try:
            await self._runner.run_task_stream(
                settings=self._settings,
                task=queue_task.task,
                dry_run=queue_task.dry_run,
                on_event=lambda payload: self._handle_progress(queue_task, payload),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[EktaBatchLogin] queued task failed: %s", exc, exc_info=True)
            await self._safe_send(
                queue_task.origin,
                f"{_task_prefix(queue_task.task)} 执行失败: {exc}",
            )

    async def _handle_progress(
        self,
        queue_task: QueuedQrTask,
        payload: dict[str, Any],
    ) -> None:
        event_type = payload.get("type")
        if event_type == "task_start":
            if self._settings.realtime_progress:
                await self._safe_send(queue_task.origin, _format_task_start(payload))
        elif event_type == "account_result":
            if self._should_send_account_result(payload):
                await self._safe_send(queue_task.origin, _format_account_result(payload))
        elif event_type == "task_done":
            payload["maxCompletionDetailLines"] = (
                self._settings.max_completion_detail_lines
            )
            await self._safe_send(queue_task.origin, _format_task_done(payload))

    def _should_send_account_result(self, payload: dict[str, Any]) -> bool:
        if not self._settings.realtime_progress:
            return False
        status = str(payload.get("status") or "")
        if status in {"失败", "跳过"}:
            return True
        if not self._settings.send_success_detail:
            return False
        interval = self._settings.progress_message_interval
        index = _int_value(payload.get("index"))
        account_count = _int_value(payload.get("accountCount"))
        return index == account_count or index % interval == 0

    async def _safe_send(self, origin: str, text: str) -> None:
        try:
            await self._send_text(origin, text)
        except Exception as exc:
            logger.error("[EktaBatchLogin] progress send failed: %s", exc, exc_info=True)


def _format_task_start(payload: dict[str, Any]) -> str:
    mode = "dry-run" if payload.get("dryRun") else "真实提交"
    return (
        f"开始{payload.get('label') or '第二课堂任务'} "
        f"activityId={payload.get('activityId') or ''}，"
        f"账号 {payload.get('accountCount') or 0} 个，{mode}"
    )


def _format_account_result(payload: dict[str, Any]) -> str:
    account = payload.get("accountCode") or "未知账号"
    status = payload.get("status") or "未知"
    message = str(payload.get("message") or "").strip()
    progress = f"[{payload.get('index') or 0}/{payload.get('accountCount') or 0}]"
    return f"{progress} {account}: {status}" + (f" - {message}" if message else "")


def _format_task_done(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    lines = [
        (
            f"完成{payload.get('label') or '第二课堂任务'} "
            f"activityId={payload.get('activityId') or ''}: "
            f"成功 {counts.get('ok', 0)}，"
            f"已完成 {counts.get('already', 0)}，"
            f"失败 {counts.get('failed', 0)}，"
            f"跳过 {counts.get('skipped', 0)}"
        )
    ]
    details = _completion_detail_lines(payload)
    if details:
        lines.append("失败/跳过明细:")
        lines.extend(details)
    return "\n".join(lines)


def _completion_detail_lines(payload: dict[str, Any]) -> list[str]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    limit = _int_value(payload.get("maxCompletionDetailLines"))
    if limit <= 0:
        return []
    lines: list[str] = []
    omitted = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("status") not in {"失败", "跳过"}:
            continue
        if len(lines) >= limit:
            omitted += 1
            continue
        account = result.get("accountCode") or "未知账号"
        status = result.get("status") or "未知"
        message = str(result.get("message") or "").strip()
        lines.append(f"{account}: {status}" + (f" - {message}" if message else ""))
    if omitted:
        lines.append(f"... 其余 {omitted} 条失败/跳过明细已省略")
    return lines


def _task_prefix(task: dict[str, Any]) -> str:
    label = "活动加入" if task.get("kind") == "activity_join" else "签到处理"
    return f"{label} activityId={task.get('activityId') or ''}"


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
