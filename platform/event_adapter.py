from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from astrbot.api.event import AstrMessageEvent


MESSAGE_ID_FIELDS = ("get_message_id", "message_id")
MESSAGE_TEXT_FIELDS = ("get_message_str", "message_str", "raw_message")
PLATFORM_FIELDS = ("get_platform_name", "platform_name")
GROUP_ID_FIELDS = ("get_group_id", "group_id")
SENDER_ID_FIELDS = ("get_sender_id", "sender_id", "user_id")
ROLE_FIELDS = ("get_sender_role", "sender_role", "group_role", "member_role", "role")
OWNER_BOOL_FIELDS = ("is_group_owner", "sender_is_group_owner", "is_owner")
ADMIN_BOOL_FIELDS = ("is_group_admin", "sender_is_group_admin", "is_admin")
RAW_FIELDS = ("raw_message", "raw_event", "raw")
IMAGE_SOURCE_FIELDS = ("url", "file", "path", "src")


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    platform: str
    session_id: str
    sender_id: str
    message_id: str
    message_text: str
    sender_is_group_manager: bool

    @property
    def pending_key(self) -> tuple[str, str, str]:
        return (self.platform, self.session_id, self.sender_id)


def dehydrate_event(event: AstrMessageEvent) -> EventSnapshot:
    platform = _event_value(event, *PLATFORM_FIELDS)
    group_id = _event_value(event, *GROUP_ID_FIELDS)
    sender_id = _event_value(event, *SENDER_ID_FIELDS)
    session_id = group_id or f"private:{sender_id}"
    return EventSnapshot(
        platform=platform,
        session_id=session_id,
        sender_id=sender_id,
        message_id=_event_value(event, *MESSAGE_ID_FIELDS),
        message_text=_event_value(event, *MESSAGE_TEXT_FIELDS),
        sender_is_group_manager=_sender_is_group_manager(event),
    )


def extract_image_sources(event: AstrMessageEvent) -> tuple[str, ...]:
    sources: list[str] = []
    for source in _image_candidate_sources(event):
        _collect_image_sources(source, sources)
    return tuple(_dedupe(sources))


def _collect_image_sources(source: object, output: list[str]) -> None:
    if source is None:
        return
    if isinstance(source, str):
        output.extend(_extract_cq_image_sources(source))
        return
    if isinstance(source, Mapping):
        segment_type = str(source.get("type") or source.get("message_type") or "")
        data = source.get("data")
        if segment_type.casefold() == "image":
            output.extend(_usable_image_values(source))
            output.extend(_usable_image_values(data))
        _collect_nested_values(source.values(), output)
        return
    if isinstance(source, bytes):
        return
    if isinstance(source, Iterable):
        _collect_nested_values(source, output)
        return

    class_name = source.__class__.__name__.casefold()
    if class_name == "image":
        output.extend(_usable_image_values(source))
    for attr in ("data", "message", "chain"):
        child = _source_value(source, attr)
        if child is not None:
            _collect_image_sources(child, output)


def _collect_nested_values(values: Iterable[object], output: list[str]) -> None:
    for item in values:
        if isinstance(item, Mapping):
            item_type = str(item.get("type") or item.get("message_type") or "")
            if item_type.casefold() == "image":
                _collect_image_sources(item, output)
                continue
        elif item is not None and item.__class__.__name__.casefold() == "image":
            _collect_image_sources(item, output)
            continue
        elif isinstance(item, Iterable) and not isinstance(item, str | bytes):
            _collect_image_sources(item, output)


def _usable_image_values(source: object) -> list[str]:
    values: list[str] = []
    for field in IMAGE_SOURCE_FIELDS:
        value = _source_value(source, field)
        if value is not None:
            values.append(str(value))
    return [value for value in values if _is_usable_image_source(value)]


def _extract_cq_image_sources(text: str) -> list[str]:
    if "[CQ:image" not in text or len(text) > 10_000:
        return []
    sources: list[str] = []
    start = 0
    while True:
        marker = text.find("[CQ:image", start)
        if marker < 0:
            break
        end = text.find("]", marker)
        if end < 0:
            break
        segment = text[marker + len("[CQ:image") : end].lstrip(",")
        fields = {}
        for item in segment.split(","):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            fields[key.strip()] = value.strip()
        for field in IMAGE_SOURCE_FIELDS:
            value = fields.get(field)
            if value and _is_usable_image_source(value):
                sources.append(value)
        start = end + 1
    return sources


def _is_usable_image_source(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.casefold()
    if lowered.startswith(("http://", "https://", "file://")):
        return True
    try:
        return Path(text).is_absolute()
    except Exception:
        return False


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in values:
        value = raw.strip()
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _image_candidate_sources(event: AstrMessageEvent) -> tuple[object, ...]:
    sources: list[object] = []
    message_obj = _message_obj_from_event(event)
    if message_obj is not None:
        sources.append(message_obj)
        for raw_name in RAW_FIELDS:
            raw = _source_value(message_obj, raw_name)
            if raw is not None:
                sources.append(raw)
    sources.append(event)
    return tuple(sources)


def _sender_is_group_manager(event: AstrMessageEvent) -> bool:
    for source in _sender_sources(event):
        role = _source_value(source, *ROLE_FIELDS)
        if role and str(role).strip().casefold() in ("admin", "owner"):
            return True
        if _source_bool(source, *OWNER_BOOL_FIELDS):
            return True
        if _source_bool(source, *ADMIN_BOOL_FIELDS):
            return True
    return False


def _sender_sources(event: AstrMessageEvent) -> tuple[object, ...]:
    sources: list[object] = [event]
    message_obj = _message_obj_from_event(event)
    if message_obj is None:
        return tuple(sources)
    sources.append(message_obj)
    sender = _source_value(message_obj, "sender")
    if sender is not None:
        sources.append(sender)
    for raw_name in RAW_FIELDS:
        raw = _source_value(message_obj, raw_name)
        if raw is not None:
            sources.append(raw)
            raw_sender = _source_value(raw, "sender")
            if raw_sender is not None:
                sources.append(raw_sender)
    return tuple(sources)


def _event_value(event: AstrMessageEvent, *names: str) -> str:
    for source in _event_sources(event):
        value = _source_value(source, *names)
        if value is not None and str(value):
            return str(value)
    return ""


def _event_sources(event: AstrMessageEvent) -> tuple[object, ...]:
    sources: list[object] = [event]
    message_obj = _message_obj_from_event(event)
    if message_obj is None:
        return tuple(sources)
    sources.append(message_obj)
    for raw_name in RAW_FIELDS:
        raw = _source_value(message_obj, raw_name)
        if raw is not None:
            sources.append(raw)
    return tuple(sources)


def _source_bool(source: object, *names: str) -> bool:
    value = _source_value(source, *names)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "y", "on")
    return False


def _source_value(source: object, *names: str) -> object:
    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name)
        else:
            try:
                value = getattr(source, name, None)
            except Exception:
                continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None:
            return value
    return None


def _message_obj_from_event(event: AstrMessageEvent) -> object | None:
    try:
        return getattr(event, "message_obj", None)
    except Exception:
        return None
