from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..domain.settings import EktaSettings

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class EktaNodeRunner:
    def __init__(
        self,
        *,
        script_path: Path,
        fallback_node_modules: Path,
    ) -> None:
        self._script_path = script_path
        self._fallback_node_modules = fallback_node_modules

    async def decode_images(
        self,
        *,
        settings: EktaSettings,
        image_sources: tuple[str, ...],
    ) -> dict[str, Any]:
        command = [
            settings.node_path,
            str(self._script_path),
            "--decode-only",
        ]
        for source in image_sources:
            command.extend(["--image", source])
        return await self._run_json_command(command, settings.run_timeout_seconds)

    async def run_task_stream(
        self,
        *,
        settings: EktaSettings,
        task: dict[str, Any],
        dry_run: bool,
        on_event: ProgressCallback,
    ) -> None:
        command = [
            settings.node_path,
            str(self._script_path),
            "--accounts",
            str(settings.accounts_csv),
            "--delay-ms",
            str(settings.delay_ms),
            "--max-accounts",
            str(settings.max_accounts_per_run),
            "--task-json",
            json.dumps(task, ensure_ascii=False),
            "--jsonl",
        ]
        if settings.school_code:
            command.extend(["--school-code", settings.school_code])
        if dry_run:
            command.append("--dry-run")

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            await asyncio.wait_for(
                self._consume_jsonl(process, on_event),
                timeout=settings.run_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError("第二课堂队列任务执行超时") from None
        except asyncio.CancelledError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.communicate()
            raise

        stderr = await stderr_task
        if process.returncode != 0:
            detail = _clean_process_text(stderr)
            raise RuntimeError(detail or "第二课堂队列任务执行失败")

    async def run_images(
        self,
        *,
        settings: EktaSettings,
        image_sources: tuple[str, ...],
        dry_run: bool,
    ) -> dict[str, Any]:
        command = [
            settings.node_path,
            str(self._script_path),
            "--accounts",
            str(settings.accounts_csv),
            "--delay-ms",
            str(settings.delay_ms),
            "--max-accounts",
            str(settings.max_accounts_per_run),
        ]
        if settings.school_code:
            command.extend(["--school-code", settings.school_code])
        if dry_run:
            command.append("--dry-run")
        for source in image_sources:
            command.extend(["--image", source])

        return await self._run_json_command(command, settings.run_timeout_seconds)

    async def _run_json_command(
        self,
        command: list[str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError("第二课堂批量任务执行超时") from None

        if process.returncode != 0:
            detail = _clean_process_text(stderr) or _clean_process_text(stdout)
            raise RuntimeError(detail or "第二课堂批量任务执行失败")

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("第二课堂执行器返回了无法解析的结果") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("第二课堂执行器返回格式不正确")
        return payload

    async def _consume_jsonl(
        self,
        process: asyncio.subprocess.Process,
        on_event: ProgressCallback,
    ) -> None:
        if process.stdout is None:
            raise RuntimeError("第二课堂执行器 stdout 不可用")
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                await on_event(payload)
        await process.wait()

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._fallback_node_modules.is_dir():
            node_path = str(self._fallback_node_modules)
            existing = env.get("NODE_PATH")
            env["NODE_PATH"] = (
                node_path if not existing else node_path + os.pathsep + existing
            )
        return env


def _clean_process_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > 500:
        return text[:500] + "..."
    return text
