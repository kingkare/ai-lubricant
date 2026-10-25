"""Tortoise model for project notes (``mc_notes`` only).

A note is free-form text a user keeps against a project — decisions made, things
to revisit, links. Deliberately minimal: the value is in having somewhere to
write it that is attached to the work, not in a rich document model.

Multi-user isolation is enforced by ``user_id`` in the service layer, same as the
rest of the project domain (see ``models_project.py``).
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class Note(Model):
    """One note belonging to a user, optionally scoped to a project."""

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()
    # 关联项目。为 NULL 的笔记是「未归入项目」的，页面按项目筛时不出现。
    project_id = fields.UUIDField(null=True)
    title = fields.CharField(max_length=255, default="")
    content = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_notes"
        indexes = (("user_id", "project_id"), ("project_id",))
