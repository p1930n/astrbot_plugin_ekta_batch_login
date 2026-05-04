from __future__ import annotations

import time
from dataclasses import dataclass

from ..platform.event_adapter import EventSnapshot


@dataclass(frozen=True, slots=True)
class PendingImageSession:
    created_at: float
    expires_at: float
    origin_message_id: str
    dry_run: bool


class PendingImageSessions:
    def __init__(self, now_factory=time.monotonic) -> None:
        self._sessions: dict[tuple[str, str, str], PendingImageSession] = {}
        self._now_factory = now_factory

    def create(
        self,
        snapshot: EventSnapshot,
        *,
        timeout_seconds: int,
        dry_run: bool,
    ) -> None:
        now = self._now_factory()
        self._sessions[snapshot.pending_key] = PendingImageSession(
            created_at=now,
            expires_at=now + timeout_seconds,
            origin_message_id=snapshot.message_id,
            dry_run=dry_run,
        )

    def consume_for(
        self,
        snapshot: EventSnapshot,
    ) -> PendingImageSession | None:
        session = self._sessions.get(snapshot.pending_key)
        if session is None:
            return None
        if session.origin_message_id and session.origin_message_id == snapshot.message_id:
            return None
        self._sessions.pop(snapshot.pending_key, None)
        if session.expires_at < self._now_factory():
            return None
        return session

    def clear_expired(self) -> None:
        now = self._now_factory()
        expired = [key for key, session in self._sessions.items() if session.expires_at < now]
        for key in expired:
            self._sessions.pop(key, None)

    def clear(self) -> None:
        self._sessions.clear()
