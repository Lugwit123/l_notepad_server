# -*- coding: utf-8 -*-
"""账号收藏 / 通用收藏项 API：转发 Auth Service（l_notepad 不直连用户库）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import server_config

router = APIRouter(prefix="/api", tags=["accounts"])


class AccountIn(BaseModel):
    """账号收藏数据（密码/自定义字段由后端加密后入库）"""
    name: str = ""
    username: str = ""
    password: str = ""
    server: str = ""
    notes: str = ""
    custom_fields: dict[str, str] = {}


def _account_proxy(
    request: Request,
    sub_path: str,
    method: str = "GET",
    payload: dict | None = None,
) -> dict:
    """把账号请求转发到 Auth Service，携带当前登录 token。"""
    token = str(getattr(request.state, "login_token", "") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未认证，请先登录")
    url = f"{server_config.auth_url()}{sub_path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise HTTPException(
            status_code=exc.code, detail=str(detail) or "Auth Service 请求失败"
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Auth Service 不可用: {exc}")


@router.get("/accounts/custom-fields")
async def api_list_account_custom_fields(request: Request) -> list[str]:
    data = await run_in_threadpool(
        _account_proxy, request, "/api/v1/accounts/custom-fields"
    )
    return data.get("names", [])


@router.put("/accounts/custom-fields")
async def api_save_account_custom_fields(request: Request, payload: dict[str, list[str]]) -> dict:
    data = await run_in_threadpool(
        _account_proxy,
        request,
        "/api/v1/accounts/custom-fields",
        "PUT",
        {"names": payload.get("names") or []},
    )
    return {"ok": data.get("ok", False)}


@router.get("/accounts")
async def api_list_accounts(request: Request) -> list[dict]:
    data = await run_in_threadpool(
        _account_proxy, request, "/api/v1/accounts"
    )
    return data.get("accounts", [])


@router.post("/accounts")
async def api_add_account(request: Request, payload: AccountIn) -> dict:
    data = await run_in_threadpool(
        _account_proxy, request, "/api/v1/accounts", "POST", payload.model_dump()
    )
    return data.get("account", {})


@router.put("/accounts/{account_id}")
async def api_update_account(request: Request, account_id: int, payload: AccountIn) -> dict:
    data = await run_in_threadpool(
        _account_proxy,
        request,
        f"/api/v1/accounts/{account_id}",
        "PUT",
        payload.model_dump(),
    )
    return data.get("account", {})


@router.delete("/accounts/{account_id}")
async def api_delete_account(request: Request, account_id: int) -> dict:
    data = await run_in_threadpool(
        _account_proxy, request, f"/api/v1/accounts/{account_id}", "DELETE"
    )
    return {"ok": data.get("ok", False)}


# ── 通用收藏项（文件夹/命令/网址 云同步）API：转发 Auth Service ──


@router.get("/fav-items")
async def api_list_fav_items(request: Request, kind: str = "") -> list[dict]:
    sub_path = "/api/v1/fav-items"
    if kind:
        sub_path += f"?kind={urllib.parse.quote(kind)}"
    data = await run_in_threadpool(_account_proxy, request, sub_path)
    return data.get("items", [])


@router.post("/fav-items")
async def api_add_fav_item(request: Request, payload: dict) -> dict:
    data = await run_in_threadpool(
        _account_proxy, request, "/api/v1/fav-items", "POST", payload
    )
    return data.get("item", {})


@router.put("/fav-items/{item_id}")
async def api_update_fav_item(request: Request, item_id: int, payload: dict) -> dict:
    data = await run_in_threadpool(
        _account_proxy, request, f"/api/v1/fav-items/{item_id}", "PUT", payload
    )
    return data.get("item", {})


@router.delete("/fav-items/{item_id}")
async def api_delete_fav_item(request: Request, item_id: int) -> dict:
    data = await run_in_threadpool(
        _account_proxy, request, f"/api/v1/fav-items/{item_id}", "DELETE"
    )
    return {"ok": data.get("ok", False)}
