# -*- coding: utf-8 -*-
"""
账号收藏的 PostgreSQL 存储（替代本地 JSON）。

- 表: l_notepad_accounts / l_notepad_custom_fields（位于 chatroom 库）
- 敏感字段（密码、自定义字段值）用 cryptography Fernet 加密，
  密钥在 ~/.Lugwit/l_notepad/.accounts_key（自动生成）
- PG 不可用时 available=False，上层回退本地 JSON
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import asyncpg
from cryptography.fernet import Fernet

from pytracemp import lprint

from .paths import data_root

PG_URL = os.environ.get(
    "L_NOTEPAD_PG_URL",
    "postgresql://postgres:OC.123456@127.0.0.1:5432/chatroom",
)

_KEY_PATH = data_root() / ".accounts_key"


def _get_fernet() -> Optional[Fernet]:
    try:
        if _KEY_PATH.exists():
            key = _KEY_PATH.read_bytes()
        else:
            key = Fernet.generate_key()
            _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _KEY_PATH.write_bytes(key)
        return Fernet(key)
    except Exception as e:  # noqa: BLE001
        lprint(f"[l_notepad] 账号加密密钥不可用: {e}")
        return None


class AccountStore:
    """PostgreSQL 账号存储（asyncpg 连接池）。"""

    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None
        self.available = False
        self._fernet = _get_fernet()

    # ── 连接 / 建表 ──
    async def connect(self) -> None:
        try:
            self.pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=4)
            await self._init_tables()
            self.available = True
            await self._migrate_from_local_json()
            lprint(f"[l_notepad] PostgreSQL 账号存储已就绪 ({PG_URL})")
        except Exception as e:  # noqa: BLE001
            self.available = False
            lprint(f"[l_notepad] PostgreSQL 账号存储不可用: {e}")

    async def close(self) -> None:
        if self.pool is not None:
            try:
                await self.pool.close()
            except Exception:  # noqa: BLE001
                pass
            self.pool = None
        self.available = False

    async def _init_tables(self) -> None:
        assert self.pool is not None
        await self.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS l_notepad_accounts (
                id SERIAL PRIMARY KEY,
                owner TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                password_enc TEXT NOT NULL DEFAULT '',
                server TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                custom_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # 兼容旧表（无 owner 列）：补充列并把历史数据归给 admin01
        await self.pool.execute(
            "ALTER TABLE l_notepad_accounts ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT ''"
        )
        await self.pool.execute(
            "UPDATE l_notepad_accounts SET owner='admin01' WHERE owner=''"
        )
        # 自定义字段名：按用户隔离，重建为 (field_name, owner) 主键
        await self.pool.execute("DROP TABLE IF EXISTS l_notepad_custom_fields")
        await self.pool.execute(
            """
            CREATE TABLE l_notepad_custom_fields (
                field_name TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT '',
                sort_order INT NOT NULL DEFAULT 0,
                PRIMARY KEY (field_name, owner)
            )
            """
        )

    # ── 加密 ──
    def _encrypt(self, text: str) -> str:
        if not text or self._fernet is None:
            return text
        try:
            return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")
        except Exception:  # noqa: BLE001
            return text

    def _decrypt(self, token: str) -> str:
        if not token or self._fernet is None:
            return token
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return token

    def _encrypt_custom_fields(self, fields: Optional[dict]) -> dict:
        return {str(k): self._encrypt(str(v)) for k, v in (fields or {}).items()}

    def _decrypt_custom_fields(self, fields: Optional[Any]) -> dict:
        if not fields:
            return {}
        # asyncpg 对 JSONB 列默认返回 str，需先解析为 dict
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:  # noqa: BLE001
                return {}
        if not isinstance(fields, dict):
            return {}
        try:
            return {str(k): self._decrypt(str(v)) for k, v in fields.items()}
        except Exception:  # noqa: BLE001
            return {str(k): str(v) for k, v in fields.items()}

    # ── 账号 CRUD ──
    async def list_accounts(self, owner: str) -> list[dict[str, Any]]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT id, name, username, password_enc, server, notes, custom_fields, sort_order "
            "FROM l_notepad_accounts WHERE owner = $1 ORDER BY sort_order, id",
            owner,
        )
        return [self._row_to_dict(r) for r in rows]

    async def add_account(self, owner: str, data: dict[str, Any]) -> dict[str, Any]:
        assert self.pool is not None
        sort_order = await self._next_sort_order(owner)
        row = await self.pool.fetchrow(
            "INSERT INTO l_notepad_accounts "
            "(owner, name, username, password_enc, server, notes, custom_fields, sort_order) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "RETURNING id, name, username, password_enc, server, notes, custom_fields, sort_order",
            owner,
            str(data.get("name", "")),
            str(data.get("username", "")),
            self._encrypt(str(data.get("password", ""))),
            str(data.get("server", "")),
            str(data.get("notes", "")),
            json.dumps(self._encrypt_custom_fields(data.get("custom_fields")), ensure_ascii=False),
            sort_order,
        )
        return self._row_to_dict(row)

    async def update_account(self, owner: str, account_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        assert self.pool is not None
        row = await self.pool.fetchrow(
            "UPDATE l_notepad_accounts SET "
            "name=$1, username=$2, password_enc=$3, server=$4, notes=$5, "
            "custom_fields=$6, updated_at=now() WHERE id=$7 AND owner=$8 "
            "RETURNING id, name, username, password_enc, server, notes, custom_fields, sort_order",
            str(data.get("name", "")),
            str(data.get("username", "")),
            self._encrypt(str(data.get("password", ""))),
            str(data.get("server", "")),
            str(data.get("notes", "")),
            json.dumps(self._encrypt_custom_fields(data.get("custom_fields")), ensure_ascii=False),
            account_id,
            owner,
        )
        return self._row_to_dict(row) if row else None

    async def delete_account(self, owner: str, account_id: int) -> bool:
        assert self.pool is not None
        result = await self.pool.execute(
            "DELETE FROM l_notepad_accounts WHERE id = $1 AND owner = $2", account_id, owner
        )
        return "DELETE" in result

    # ── 自定义字段名 ──
    async def list_custom_fields(self, owner: str) -> list[str]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT field_name FROM l_notepad_custom_fields WHERE owner = $1 "
            "ORDER BY sort_order, field_name",
            owner,
        )
        return [r["field_name"] for r in rows]

    async def save_custom_fields(self, owner: str, names: list[str]) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM l_notepad_custom_fields WHERE owner = $1", owner
                )
                for i, n in enumerate(names or []):
                    await conn.execute(
                        "INSERT INTO l_notepad_custom_fields (field_name, owner, sort_order) "
                        "VALUES ($1, $2, $3)",
                        str(n), owner, i,
                    )

    # ── 辅助 ──
    async def _next_sort_order(self, owner: str) -> int:
        assert self.pool is not None
        row = await self.pool.fetchval(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM l_notepad_accounts WHERE owner = $1",
            owner,
        )
        return int(row or 0)

    def _row_to_dict(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "username": row["username"],
            "password": self._decrypt(row["password_enc"]),
            "server": row["server"],
            "notes": row["notes"],
            "custom_fields": self._decrypt_custom_fields(row["custom_fields"]),
        }

    # ── 迁移本地 JSON → PG（幂等）──
    async def _migrate_from_local_json(self) -> None:
        """迁移本地 JSON → PG（账号与自定义字段名各自独立判断，幂等）"""
        try:
            # 账号（迁移数据归给 admin01，后续各用户各自管理）
            _owner = "admin01"
            json_path = data_root() / "account_favorites.json"
            cnt = await self.pool.fetchval("SELECT COUNT(*) FROM l_notepad_accounts")
            if json_path.exists() and (not cnt or int(cnt) == 0):
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    for item in data:
                        await self.add_account(_owner, item)
                    lprint(f"[l_notepad] 已从本地 JSON 迁移 {len(data)} 条账号到 PostgreSQL (owner={_owner})")
            # 自定义字段名
            cnames_path = data_root() / "account_custom_fields.json"
            ccnt = await self.pool.fetchval("SELECT COUNT(*) FROM l_notepad_custom_fields")
            if cnames_path.exists() and (not ccnt or int(ccnt) == 0):
                with open(cnames_path, "r", encoding="utf-8") as f:
                    names = json.load(f)
                if isinstance(names, list) and names:
                    await self.save_custom_fields(_owner, [str(n) for n in names if str(n).strip()])
                    lprint(f"[l_notepad] 已从本地 JSON 迁移 {len(names)} 个自定义字段名到 PostgreSQL (owner={_owner})")
        except Exception as e:  # noqa: BLE001
            lprint(f"[l_notepad] 迁移账号到 PostgreSQL 失败: {e}")
