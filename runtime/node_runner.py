from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from ..domain.settings import EktaSettings

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class EktaNodeDependencyError(RuntimeError):
    """Raised when the Node runner dependencies are not available."""


class EktaNodeRunner:
    def __init__(
        self,
        *,
        script_path: Path,
        fallback_node_modules: Path,
    ) -> None:
        self._script_path = script_path
        self._vendor_dir = script_path.parent
        self._package_json = self._vendor_dir / "package.json"
        self._local_node_modules = self._vendor_dir / "node_modules"
        self._fallback_node_modules = fallback_node_modules
        self._dependency_lock = asyncio.Lock()
        self._dependencies_ready = False

    async def decode_images(
        self,
        *,
        settings: EktaSettings,
        image_sources: tuple[str, ...],
    ) -> dict[str, Any]:
        await self.ensure_dependencies(settings)
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
        await self.ensure_dependencies(settings)
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
        await self.ensure_dependencies(settings)
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

    async def ensure_dependencies(self, settings: EktaSettings) -> None:
        if self._dependencies_ready:
            return

        missing = await asyncio.to_thread(self._missing_node_dependencies)
        if not missing:
            self._dependencies_ready = True
            return

        if not settings.auto_install_node_dependencies:
            raise EktaNodeDependencyError(_format_missing_dependency_message(missing))

        async with self._dependency_lock:
            missing = await asyncio.to_thread(self._missing_node_dependencies)
            if not missing:
                self._dependencies_ready = True
                return

            await self._install_node_dependencies(settings, missing)
            missing = await asyncio.to_thread(self._missing_node_dependencies)
            if missing:
                raise EktaNodeDependencyError(
                    _format_missing_dependency_message(missing),
                )
            self._dependencies_ready = True

    async def node_dependency_status(self, settings: EktaSettings) -> str:
        missing = await asyncio.to_thread(self._missing_node_dependencies)
        install_mode = "自动安装开启" if settings.auto_install_node_dependencies else "自动安装关闭"
        if not missing:
            return f"已满足（{install_mode}）"
        return f"缺失 {', '.join(missing)}（{install_mode}）"

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

    async def _install_node_dependencies(
        self,
        settings: EktaSettings,
        missing: tuple[str, ...],
    ) -> None:
        logger.info(
            "[EktaBatchLogin] installing Node dependencies: %s",
            ", ".join(missing),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                settings.npm_path,
                "install",
                "--omit=dev",
                cwd=str(self._vendor_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise EktaNodeDependencyError(
                "找不到 npm，请先安装 Node.js/npm，或手动准备插件 vendor/node_modules。",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.node_dependency_install_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise EktaNodeDependencyError(
                "Node 依赖自动安装超时，请稍后重试或手动运行 npm install --omit=dev。",
            ) from None

        if process.returncode != 0:
            logger.error(
                "[EktaBatchLogin] npm install failed: stdout=%s stderr=%s",
                _clean_process_text(stdout),
                _clean_process_text(stderr),
            )
            raise EktaNodeDependencyError(
                "Node 依赖自动安装失败，请在插件 vendor 目录手动运行 npm install --omit=dev。",
            )

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._fallback_node_modules.is_dir():
            node_path = str(self._fallback_node_modules)
            existing = env.get("NODE_PATH")
            env["NODE_PATH"] = (
                node_path if not existing else node_path + os.pathsep + existing
            )
        return env

    def _missing_node_dependencies(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self._node_dependency_names()
            if not self._node_dependency_exists(name)
        )

    def _node_dependency_names(self) -> tuple[str, ...]:
        try:
            payload = json.loads(self._package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, dict):
            return ()
        names = [name for name in dependencies if isinstance(name, str) and name]
        return tuple(sorted(names))

    def _node_dependency_exists(self, name: str) -> bool:
        return self._dependency_root(self._local_node_modules, name).is_dir() or (
            self._dependency_root(self._fallback_node_modules, name).is_dir()
        )

    @staticmethod
    def _dependency_root(node_modules: Path, name: str) -> Path:
        dependency_path = node_modules
        for part in name.split("/"):
            dependency_path /= part
        return dependency_path


def _clean_process_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > 500:
        return text[:500] + "..."
    return text


def _format_missing_dependency_message(missing: tuple[str, ...]) -> str:
    return (
        "缺少 Node 依赖 "
        + ", ".join(missing)
        + "，请在插件 vendor 目录运行 npm install --omit=dev。"
    )
