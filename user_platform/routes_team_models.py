"""Team login-methods routes.

The team-admin surface lives under ``/api/v1/teams/*``; every path is written in
full and no ``prefix`` is set on the router. All handlers are storage-side only;
the test endpoint issues a minimal outbound HTTP probe (reachability only).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import client_meta, get_current_team_id, get_current_user
from .models import User
from .team_models_service import team_models_service
from .team_users_service import team_users_service

router = APIRouter(tags=["user-platform-team-models"])


# ── models ────────────────────────────────────────────────────────────────────


@router.get("/api/v1/teams/models")
async def list_team_models(team_id: str = Depends(get_current_team_id)) -> dict:
    """列出本团队的模型配置。"""
    return await team_models_service.list_models(team_id)


@router.post("/api/v1/teams/models")
async def create_team_model(
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """为团队新增模型配置，并记录 model.create 审计。

    body 为原始 JSON（模型名/base_url/key 等由服务层校验）；审计含请求与
    响应体，记入 ``mc_audits``。
    """
    body = await request.json()
    result = await team_models_service.create_model(team_id, body)
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "model.create",
        request=body, response=result, source_ip=ip, user_agent=ua,
    )
    return result


@router.put("/api/v1/teams/models/{model_id}")
async def update_team_model(
    model_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """更新团队模型配置并记录 model.update 审计；模型不存在或无权访问返回 404。"""
    body = await request.json()
    result = await team_models_service.update_model(team_id, model_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="模型不存在或无权访问")
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "model.update",
        request={"model_id": model_id, **body} if isinstance(body, dict) else body,
        response=result, source_ip=ip, user_agent=ua,
    )
    return result


@router.delete("/api/v1/teams/models/{model_id}")
async def delete_team_model(
    model_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """删除团队模型配置并记录 model.delete 审计；模型不存在或无权访问返回 404。"""
    ok = await team_models_service.delete_model(team_id, model_id)
    if not ok:
        raise HTTPException(status_code=404, detail="模型不存在或无权访问")
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "model.delete",
        request={"model_id": model_id}, response={"deleted": True},
        source_ip=ip, user_agent=ua,
    )
    return {}


@router.post("/api/v1/teams/models/health-check")
async def health_check_team_model(
    request: Request, team_id: str = Depends(get_current_team_id)
) -> dict:
    """对未落库的临时模型配置发起连通性探测（不发请求日志）。"""
    body = await request.json()
    return await team_models_service.health_check_adhoc(body)


@router.get("/api/v1/teams/models/{model_id}/health-check")
async def health_check_stored_team_model(
    model_id: str, team_id: str = Depends(get_current_team_id)
) -> dict:
    """对已保存的团队模型发起连通性探测；模型不存在或无权访问返回 404。"""
    result = await team_models_service.health_check_stored(team_id, model_id)
    if result is None:
        raise HTTPException(status_code=404, detail="模型不存在或无权访问")
    return result


# ── login methods ─────────────────────────────────────────────────────────────


@router.get("/api/v1/teams/login-methods")
async def list_login_methods(
    request: Request, team_id: str = Depends(get_current_team_id)
) -> dict:
    """列出团队登录方式，附带按当前请求 origin 拼好的入口地址。"""
    return await team_models_service.list_methods(
        team_id, base_url=str(request.base_url).rstrip("/")
    )


@router.post("/api/v1/teams/login-methods")
async def create_login_method(
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """新增团队登录方式并记录 login_method.create 审计。

    body 为原始 JSON；服务层校验不通过（如重复名称/非法类型）抛 ValueError，
    这里统一转成 400 并把原因放进 detail。
    """
    body = await request.json()
    try:
        result = await team_models_service.create_method(
            team_id, body, base_url=str(request.base_url).rstrip("/")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "login_method.create",
        request=body, response=result, source_ip=ip, user_agent=ua,
    )
    return result


@router.put("/api/v1/teams/login-methods/{method_id}")
async def update_login_method(
    method_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """更新团队登录方式并记录 login_method.update 审计。

    ``method_not_found`` 返回 404，其余校验失败返回 400（detail 即原因码）。
    """
    body = await request.json()
    try:
        result = await team_models_service.update_method(
            team_id, method_id, body, base_url=str(request.base_url).rstrip("/")
        )
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "method_not_found" else 400
        raise HTTPException(status_code=status, detail=code) from exc
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "login_method.update",
        request={"method_id": method_id, **body} if isinstance(body, dict) else body,
        response=result, source_ip=ip, user_agent=ua,
    )
    return result


@router.delete("/api/v1/teams/login-methods/{method_id}")
async def delete_login_method(
    method_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """删除团队登录方式并记录 login_method.delete 审计；不存在或无权访问返回 404。"""
    try:
        await team_models_service.delete_method(team_id, method_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ip, ua = client_meta(request)
    await team_users_service.record_audit(
        team_id, str(user.id), "login_method.delete",
        request={"method_id": method_id}, response={"deleted": True},
        source_ip=ip, user_agent=ua,
    )
    return {"ok": True}


@router.post("/api/v1/teams/login-methods/test")
async def test_login_method(
    request: Request, team_id: str = Depends(get_current_team_id)
) -> dict:
    """试跑一个登录方式配置：只发出最小可达性探测，不落库。"""
    body = await request.json()
    return await team_models_service.test_method(body)
