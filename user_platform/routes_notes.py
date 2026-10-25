"""项目笔记的用户端 CRUD（``/api/v1/users/notes``）。

笔记挂在项目下（也可不挂），用户在项目工作区的「笔记」功能页统一管理，任务详情页
的悬浮弹框读写同一份数据。归属隔离由 ``note_service`` 按 ``user_id`` 保证。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .deps import get_current_user
from .models import User
from .note_service import note_service

router = APIRouter(prefix="/api/v1/users/notes", tags=["user-platform-notes"])


class CreateNoteReq(BaseModel):
    title: str = ""
    content: str = ""
    project_id: str | None = None


class UpdateNoteReq(BaseModel):
    title: str | None = None
    content: str | None = None
    project_id: str | None = None


@router.get("")
async def list_notes(
    project_id: str | None = None,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """列出当前用户的笔记。带 ``project_id`` 时只列该项目的（任务详情弹框用），
    不带则列全部（笔记管理页用）。"""
    return await note_service.list_notes(str(user.id), project_id=project_id)


@router.post("")
async def create_note(body: CreateNoteReq, user: User = Depends(get_current_user)) -> dict:
    """新建一条笔记（可选挂到项目下）。内容为空时 422。"""
    try:
        return await note_service.create_note(str(user.id), body.model_dump())
    except ValueError as exc:
        if str(exc) == "note_content_required":
            raise HTTPException(status_code=422, detail="笔记内容不能为空") from exc
        raise


@router.patch("/{note_id}")
async def update_note(note_id: str, body: UpdateNoteReq, user: User = Depends(get_current_user)) -> dict:
    """更新笔记（仅改传入字段）。笔记不存在 404。"""
    updated = await note_service.update_note(
        str(user.id), note_id, body.model_dump(exclude_unset=True)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return updated


@router.delete("/{note_id}")
async def delete_note(note_id: str, user: User = Depends(get_current_user)) -> dict:
    """删除笔记。笔记不存在 404。"""
    if not await note_service.delete_note(str(user.id), note_id):
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"deleted": True}
