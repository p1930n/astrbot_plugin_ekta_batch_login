from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DELAY_MS = 1000
DEFAULT_MAX_ACCOUNTS = 80
DEFAULT_MAX_IMAGES = 8
DEFAULT_PENDING_TIMEOUT_SECONDS = 60
DEFAULT_RUN_TIMEOUT_SECONDS = 900
DEFAULT_SEND_SUCCESS_DETAIL = False
DEFAULT_PROGRESS_MESSAGE_INTERVAL = 5
DEFAULT_MAX_QUEUED_QR_TASKS = 50
DEFAULT_MAX_COMPLETION_DETAIL_LINES = 30
DEFAULT_AUTO_INSTALL_NODE_DEPENDENCIES = True
DEFAULT_NPM_PATH = "npm"
DEFAULT_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 180
MIN_DELAY_MS = 0
MAX_DELAY_MS = 60_000
MIN_MAX_ACCOUNTS = 1
MAX_MAX_ACCOUNTS = 500
MIN_MAX_IMAGES = 1
MAX_MAX_IMAGES = 20
MIN_PENDING_TIMEOUT_SECONDS = 10
MAX_PENDING_TIMEOUT_SECONDS = 600
MIN_RUN_TIMEOUT_SECONDS = 30
MAX_RUN_TIMEOUT_SECONDS = 7200
MIN_PROGRESS_MESSAGE_INTERVAL = 1
MAX_PROGRESS_MESSAGE_INTERVAL = 50
MIN_MAX_QUEUED_QR_TASKS = 1
MAX_MAX_QUEUED_QR_TASKS = 500
MIN_MAX_COMPLETION_DETAIL_LINES = 0
MAX_MAX_COMPLETION_DETAIL_LINES = 200
MIN_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 10
MAX_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class EktaSettings:
    node_path: str
    accounts_csv: Path
    school_code: str
    delay_ms: int
    max_accounts_per_run: int
    max_images_per_run: int
    pending_timeout_seconds: int
    run_timeout_seconds: int
    dry_run_by_default: bool
    allow_group_manager: bool
    realtime_progress: bool
    send_success_detail: bool
    progress_message_interval: int
    max_queued_qr_tasks: int
    max_completion_detail_lines: int
    auto_install_node_dependencies: bool
    npm_path: str
    node_dependency_install_timeout_seconds: int

    @classmethod
    def from_config(
        cls,
        config: Any | None,
        *,
        data_root: Path,
    ) -> "EktaSettings":
        data = _as_mapping(config)
        accounts_csv = _path_value(
            data.get("accounts_csv"),
            default=data_root / "ekta_batch_login" / "accounts.csv",
        )
        return cls(
            node_path=_string_value(data.get("node_path"), "node"),
            accounts_csv=accounts_csv,
            school_code=_string_value(data.get("school_code"), ""),
            delay_ms=_bounded_int(
                data.get("delay_ms"),
                default=DEFAULT_DELAY_MS,
                minimum=MIN_DELAY_MS,
                maximum=MAX_DELAY_MS,
            ),
            max_accounts_per_run=_bounded_int(
                data.get("max_accounts_per_run"),
                default=DEFAULT_MAX_ACCOUNTS,
                minimum=MIN_MAX_ACCOUNTS,
                maximum=MAX_MAX_ACCOUNTS,
            ),
            max_images_per_run=_bounded_int(
                data.get("max_images_per_run"),
                default=DEFAULT_MAX_IMAGES,
                minimum=MIN_MAX_IMAGES,
                maximum=MAX_MAX_IMAGES,
            ),
            pending_timeout_seconds=_bounded_int(
                data.get("pending_timeout_seconds"),
                default=DEFAULT_PENDING_TIMEOUT_SECONDS,
                minimum=MIN_PENDING_TIMEOUT_SECONDS,
                maximum=MAX_PENDING_TIMEOUT_SECONDS,
            ),
            run_timeout_seconds=_bounded_int(
                data.get("run_timeout_seconds"),
                default=DEFAULT_RUN_TIMEOUT_SECONDS,
                minimum=MIN_RUN_TIMEOUT_SECONDS,
                maximum=MAX_RUN_TIMEOUT_SECONDS,
            ),
            dry_run_by_default=_bool_value(data.get("dry_run_by_default"), False),
            allow_group_manager=_bool_value(data.get("allow_group_manager"), True),
            realtime_progress=_bool_value(data.get("realtime_progress"), True),
            send_success_detail=_bool_value(
                data.get("send_success_detail"),
                DEFAULT_SEND_SUCCESS_DETAIL,
            ),
            progress_message_interval=_bounded_int(
                data.get("progress_message_interval"),
                default=DEFAULT_PROGRESS_MESSAGE_INTERVAL,
                minimum=MIN_PROGRESS_MESSAGE_INTERVAL,
                maximum=MAX_PROGRESS_MESSAGE_INTERVAL,
            ),
            max_queued_qr_tasks=_bounded_int(
                data.get("max_queued_qr_tasks"),
                default=DEFAULT_MAX_QUEUED_QR_TASKS,
                minimum=MIN_MAX_QUEUED_QR_TASKS,
                maximum=MAX_MAX_QUEUED_QR_TASKS,
            ),
            max_completion_detail_lines=_bounded_int(
                data.get("max_completion_detail_lines"),
                default=DEFAULT_MAX_COMPLETION_DETAIL_LINES,
                minimum=MIN_MAX_COMPLETION_DETAIL_LINES,
                maximum=MAX_MAX_COMPLETION_DETAIL_LINES,
            ),
            auto_install_node_dependencies=_bool_value(
                data.get("auto_install_node_dependencies"),
                DEFAULT_AUTO_INSTALL_NODE_DEPENDENCIES,
            ),
            npm_path=_string_value(data.get("npm_path"), DEFAULT_NPM_PATH),
            node_dependency_install_timeout_seconds=_bounded_int(
                data.get("node_dependency_install_timeout_seconds"),
                default=DEFAULT_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
                minimum=MIN_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
                maximum=MAX_NODE_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
            ),
        )


def _as_mapping(config: Any | None) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return config
    if hasattr(config, "items"):
        try:
            return dict(config.items())
        except Exception:
            return {}
    return {}


def _string_value(value: object, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _path_value(value: object, *, default: Path) -> Path:
    if value is None:
        return default
    text = str(value).strip()
    return Path(text) if text else default


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _bool_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in ("1", "true", "yes", "y", "on"):
            return True
        if normalized in ("0", "false", "no", "n", "off"):
            return False
    return default
