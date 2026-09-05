# -*- coding: utf-8 -*-
"""本地版本历史存储。

为「笔记」和「服务器日志」提供客户端的版本快照能力：
- 每次保存成功后调用 add_version()，仅当内容相对上一版本发生变化时才写入。
- 通过 list_versions()/get_version() 读取历史并切换。

数据存放在本模块同级目录的 version_history.sqlite3，不依赖服务端。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import paths

# 单个 (kind, ref) 最多保留的历史版本数，超出后删除最旧的。
MAX_VERSIONS_PER_REF = 100

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  title TEXT,
  content TEXT NOT NULL,
  saved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_ref ON versions(kind, ref, id DESC);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db_path() -> Path:
    return paths.version_history_db()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA_SQL)
        _conn.commit()
    return _conn


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_version(kind: str, ref: str, title: str, content: str) -> bool:
    """记录一个新版本。仅当内容与上一版本不同才写入。

    返回 True 表示新建了版本，False 表示内容未变化被跳过。
    """
    if not ref:
        return False
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT content FROM versions WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 1",
            (kind, ref),
        ).fetchone()
        if row is not None and row["content"] == content:
            return False
        conn.execute(
            "INSERT INTO versions(kind, ref, title, content, saved_at) VALUES(?,?,?,?,?)",
            (kind, ref, title, content, _now_iso()),
        )
        # 修剪超出上限的旧版本
        conn.execute(
            """
            DELETE FROM versions
            WHERE kind=? AND ref=? AND id NOT IN (
                SELECT id FROM versions WHERE kind=? AND ref=? ORDER BY id DESC LIMIT ?
            )
            """,
            (kind, ref, kind, ref, MAX_VERSIONS_PER_REF),
        )
        conn.commit()
        return True


def list_versions(kind: str, ref: str) -> list[dict]:
    """返回 (kind, ref) 的版本列表（新→旧），不含完整内容，仅含预览与长度。"""
    if not ref:
        return []
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, title, saved_at, content FROM versions "
            "WHERE kind=? AND ref=? ORDER BY id DESC",
            (kind, ref),
        ).fetchall()
    result = []
    for r in rows:
        content = r["content"] or ""
        preview = content.strip().splitlines()[0] if content.strip() else ""
        result.append(
            {
                "id": r["id"],
                "title": r["title"] or "",
                "saved_at": r["saved_at"],
                "length": len(content),
                "preview": preview,
            }
        )
    return result


def get_version(version_id: int) -> Optional[dict]:
    """按 id 返回完整版本（含 content）。"""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, kind, ref, title, content, saved_at FROM versions WHERE id=?",
            (version_id,),
        ).fetchone()
    return dict(row) if row else None
