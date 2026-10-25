"""Project notes: CRUD with ownership isolation.

A note belongs to exactly one user and optionally one project. Isolation is by
``user_id`` on every read and write — the same rule the rest of the project
domain follows (see ``project_service``), so a note never leaks across users
even when both are looking at the same project.
"""
from __future__ import annotations

import uuid
from typing import Any

from .models_note import Note


def _note_dict(note: Note) -> dict:
    return {
        "id": str(note.id),
        "project_id": str(note.project_id) if note.project_id else None,
        "title": note.title or "",
        "content": note.content or "",
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
    }


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None


class NoteService:
    """Task-level CRUD over ``mc_notes``."""

    async def list_notes(
        self, user_id: str, *, project_id: str | None = None
    ) -> list[dict]:
        """List the caller's notes, newest first, optionally scoped to a project.

        An absent ``project_id`` lists everything the user owns (the management
        page); a present one lists just that project's notes (the task-detail
        dialog).
        """
        query = Note.filter(user_id=uuid.UUID(user_id))
        if project_id is not None:
            project_uuid = _maybe_uuid(project_id)
            if project_uuid is None:
                # 传了 project_id 但不是合法 UUID：空结果而不是全量，避免调用方
                # 以为自己在看某个项目的笔记、实际拿到的是全部。
                return []
            query = query.filter(project_id=project_uuid)
        rows = await query.order_by("-updated_at", "-created_at")
        return [_note_dict(note) for note in rows]

    async def _owned_note(self, user_id: str, note_id: str) -> Note | None:
        """Fetch a note only if the caller owns it.

        Returns None for both "missing" and "someone else's" so the caller cannot
        use the response to probe which note ids exist.
        """
        note_uuid = _maybe_uuid(note_id)
        if note_uuid is None:
            return None
        return await Note.filter(id=note_uuid, user_id=uuid.UUID(user_id)).first()

    async def create_note(self, user_id: str, req: dict) -> dict:
        content = str(req.get("content") or "").strip()
        title = str(req.get("title") or "").strip()
        if not content and not title:
            raise ValueError("note_content_required")
        note = await Note.create(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            project_id=_maybe_uuid(req.get("project_id")),
            title=title[:255],
            content=content or None,
        )
        return _note_dict(note)

    async def update_note(self, user_id: str, note_id: str, req: dict) -> dict | None:
        note = await self._owned_note(user_id, note_id)
        if note is None:
            return None
        fields: list[str] = []
        if "title" in req:
            note.title = str(req.get("title") or "").strip()[:255]
            fields.append("title")
        if "content" in req:
            note.content = str(req.get("content") or "").strip() or None
            fields.append("content")
        if "project_id" in req:
            note.project_id = _maybe_uuid(req.get("project_id"))
            fields.append("project_id")
        if not fields:
            return _note_dict(note)
        await note.save(update_fields=[*fields, "updated_at"])
        return _note_dict(note)

    async def delete_note(self, user_id: str, note_id: str) -> bool:
        note = await self._owned_note(user_id, note_id)
        if note is None:
            return False
        await note.delete()
        return True


note_service = NoteService()
