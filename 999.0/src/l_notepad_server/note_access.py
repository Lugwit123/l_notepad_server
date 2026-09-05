# -*- coding: utf-8 -*-
"""l_notepad 笔记归属与共享权限服务

笔记文件存放在 notepad_list/<owner_username>/<path>；
note_registry 表登记拥有者，note_shares 表登记共享关系。

权限模型：
  - owner    : 完全权限（读 / 写 / 删 / 共享管理）
  - write    : 读 + 写（来自共享）
  - read     : 只读（来自共享）
  - None     : 无权限（不可见 / 不可访问）
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

_PERMISSION_VALUES = {"read", "write"}
# 公共共享：shared_with 用 "*" 表示所有登录用户可见
PUBLIC_USER = "*"


def safe_username(username: Optional[str]) -> str:
    """用户名 → 安全目录名（仅字母数字 _ - .，转小写）"""
    if not username:
        return "guest"
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", username).strip("._")
    return (cleaned or "guest").lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 注册 / 归属 ─────────────────────────────────────────

def register_note(conn: sqlite3.Connection, note_path: str, owner: str) -> None:
    """注册笔记归属（幂等：已存在则仅刷新时间戳）"""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO note_registry(note_path, owner_username, created_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(note_path) DO UPDATE SET owner_username=excluded.owner_username, updated_at=excluded.updated_at
        """,
        (note_path, owner, now, now),
    )
    conn.commit()


def get_owner(conn: sqlite3.Connection, note_path: str) -> Optional[str]:
    row = conn.execute(
        "SELECT owner_username FROM note_registry WHERE note_path = ?", (note_path,)
    ).fetchone()
    return row["owner_username"] if row else None


def is_registered(conn: sqlite3.Connection, note_path: str) -> bool:
    return get_owner(conn, note_path) is not None


# ── 权限判定 ─────────────────────────────────────────

def can_access(conn: sqlite3.Connection, note_path: str, username: str, *, admin: bool = False) -> Optional[str]:
    """返回当前用户对该笔记的权限：'owner' | 'write' | 'read' | None

    笔记文件平铺在 notepad_list/ 下，归属/共享全部以数据库为准。
    公共共享（shared_with='*'）对所有登录用户可见。
    admin=True 时对全部已注册笔记至少有只读权限（管理员全可见）。
    """
    if get_owner(conn, note_path) == username:
        return "owner"
    # 明确共享给当前用户
    row = conn.execute(
        "SELECT permission FROM note_shares WHERE note_path = ? AND shared_with = ?",
        (note_path, username),
    ).fetchone()
    if row:
        return row["permission"]
    # 公共共享
    pub = conn.execute(
        "SELECT permission FROM note_shares WHERE note_path = ? AND shared_with = ?",
        (note_path, PUBLIC_USER),
    ).fetchone()
    if pub:
        return pub["permission"]
    # 管理员全可见（只读）
    if admin and is_registered(conn, note_path):
        return "read"
    return None


def can_edit(conn: sqlite3.Connection, note_path: str, username: str, *, admin: bool = False) -> bool:
    return can_access(conn, note_path, username, admin=admin) in ("owner", "write")


# ── 共享管理 ─────────────────────────────────────────

def share_note(conn: sqlite3.Connection, note_path: str, with_username: str, permission: str = "read") -> None:
    """把笔记共享给指定用户（仅 owner 可调用）。permission: read | write"""
    if permission not in _PERMISSION_VALUES:
        permission = "read"
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO note_shares(note_path, shared_with, permission, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(note_path, shared_with)
        DO UPDATE SET permission=excluded.permission, updated_at=excluded.updated_at
        """,
        (note_path, with_username, permission, now, now),
    )
    conn.commit()


def unshare_note(conn: sqlite3.Connection, note_path: str, with_username: str) -> bool:
    cur = conn.execute(
        "DELETE FROM note_shares WHERE note_path = ? AND shared_with = ?",
        (note_path, with_username),
    )
    conn.commit()
    return cur.rowcount > 0


def list_shares(conn: sqlite3.Connection, note_path: str) -> list[dict]:
    rows = conn.execute(
        "SELECT shared_with, permission, updated_at FROM note_shares WHERE note_path = ? ORDER BY id",
        (note_path,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_shared_with_me(conn: sqlite3.Connection, username: str) -> set[str]:
    """返回共享给当前用户的 note_path 集合（含公共共享）"""
    rows = conn.execute(
        "SELECT note_path FROM note_shares WHERE shared_with = ? OR shared_with = ?",
        (username, PUBLIC_USER),
    ).fetchall()
    return {r["note_path"] for r in rows}


def list_owned_by(conn: sqlite3.Connection, username: str) -> set[str]:
    """返回当前用户拥有的全部 note_path 集合"""
    rows = conn.execute(
        "SELECT note_path FROM note_registry WHERE owner_username = ?", (username,)
    ).fetchall()
    return {r["note_path"] for r in rows}


def list_accessible(conn: sqlite3.Connection, username: str, *, admin: bool = False) -> set[str]:
    """返回当前用户可访问的全部 note_path（拥有 + 共享给我的）。

    admin=True 时返回全部已注册笔记（管理员全可见）。
    """
    base = list_owned_by(conn, username) | list_shared_with_me(conn, username)
    if admin:
        rows = conn.execute("SELECT note_path FROM note_registry").fetchall()
        return base | {r["note_path"] for r in rows}
    return base


# ── 迁移遗留笔记 ─────────────────────────────────────────

def migrate_legacy_notes(conn: sqlite3.Connection, notes_root, to_username: str) -> list[str]:
    """把 notepad_list 下所有未注册的遗留笔记注册给指定用户（递归，保留相对路径）。

    文件已平铺（或存在分类子目录），无需移动，仅登记归属。返回注册的 note_path 列表。
    """
    root = notes_root.resolve()
    moved: list[str] = []
    if not root.exists():
        return moved
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        note_path = p.relative_to(root).as_posix()
        if is_registered(conn, note_path):
            continue
        register_note(conn, note_path, to_username)
        moved.append(note_path)
    return moved


# ── 管理员 / 公共笔记 ─────────────────────────────────────────

def is_public(conn: sqlite3.Connection, note_path: str) -> bool:
    """该笔记是否已设为全部共享（公共）"""
    row = conn.execute(
        "SELECT 1 FROM note_shares WHERE note_path = ? AND shared_with = ?",
        (note_path, PUBLIC_USER),
    ).fetchone()
    return row is not None


def set_public(conn: sqlite3.Connection, note_path: str, public: bool, permission: str = "read") -> None:
    """设置笔记是否对所有人可见（公共开关）"""
    if public:
        share_note(conn, note_path, PUBLIC_USER, permission)
    else:
        unshare_note(conn, note_path, PUBLIC_USER)


def set_owner(conn: sqlite3.Connection, note_path: str, new_owner: str) -> bool:
    """修改笔记拥有者（管理员用）"""
    cur = conn.execute(
        "UPDATE note_registry SET owner_username = ?, updated_at = ? WHERE note_path = ?",
        (new_owner, _now_iso(), note_path),
    )
    conn.commit()
    return cur.rowcount > 0


def migrate_note_path(conn: sqlite3.Connection, old_path: str, new_path: str) -> bool:
    """笔记文件改名/移动后同步注册表、共享关系与标签的路径（否则改名后笔记从列表消失）。"""
    if old_path == new_path:
        return False
    now = _now_iso()
    # 目标路径若已有旧注册（如覆盖同名文件），先清掉避免 UNIQUE 冲突
    registered = conn.execute(
        "SELECT 1 FROM note_registry WHERE note_path = ?", (new_path,)
    ).fetchone()
    if registered:
        conn.execute("DELETE FROM note_registry WHERE note_path = ?", (new_path,))
        conn.execute("DELETE FROM note_shares WHERE note_path = ?", (new_path,))
        conn.execute("DELETE FROM note_tags WHERE note_path = ?", (new_path,))
        conn.execute("DELETE FROM note_forks WHERE fork_note_path = ?", (new_path,))
    cur = conn.execute(
        "UPDATE note_registry SET note_path = ?, updated_at = ? WHERE note_path = ?",
        (new_path, now, old_path),
    )
    moved = cur.rowcount > 0
    conn.execute(
        "UPDATE note_shares SET note_path = ?, updated_at = ? WHERE note_path = ?",
        (new_path, now, old_path),
    )
    conn.execute(
        "UPDATE note_tags SET note_path = ?, updated_at = ? WHERE note_path = ?",
        (new_path, now, old_path),
    )
    conn.execute(
        "UPDATE note_forks SET fork_note_path = ?, updated_at = ? WHERE fork_note_path = ?",
        (new_path, now, old_path),
    )
    conn.execute(
        "UPDATE note_forks SET source_note_path = ?, updated_at = ? WHERE source_note_path = ?",
        (new_path, now, old_path),
    )
    conn.commit()
    return moved


# ── 标签 ─────────────────────────────────────────


def normalize_tag(tag: str) -> str:
    """标签归一化：去空白/控制符/前导 #，最长 50 字符。"""
    v = re.sub(r"[\x00-\x1f]", "", str(tag or ""))
    v = v.strip().lstrip("#").strip()
    return v[:50]


def list_tags(conn: sqlite3.Connection, note_path: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM note_tags WHERE note_path = ? ORDER BY tag", (note_path,)
    ).fetchall()
    return [r["tag"] for r in rows]


def add_tag(conn: sqlite3.Connection, note_path: str, tag: str) -> bool:
    t = normalize_tag(tag)
    if not t:
        return False
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO note_tags(note_path, tag, created_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(note_path, tag) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (note_path, t, now, now),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_tag(conn: sqlite3.Connection, note_path: str, tag: str) -> bool:
    cur = conn.execute(
        "DELETE FROM note_tags WHERE note_path = ? AND tag = ?",
        (note_path, normalize_tag(tag)),
    )
    conn.commit()
    return cur.rowcount > 0


def set_tags(conn: sqlite3.Connection, note_path: str, tags: "Iterable[str]") -> None:
    """把笔记标签精确同步为给定列表（增删差集）。"""
    wanted: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        t = normalize_tag(t)
        if t and t not in seen:
            seen.add(t)
            wanted.append(t)
    current = set(list_tags(conn, note_path))
    for t in current - seen:
        conn.execute("DELETE FROM note_tags WHERE note_path = ? AND tag = ?", (note_path, t))
    now = _now_iso()
    for t in seen - current:
        conn.execute(
            "INSERT INTO note_tags(note_path, tag, created_at, updated_at) VALUES(?,?,?,?)",
            (note_path, t, now, now),
        )
    conn.commit()


def all_tag_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """全部 (note_path, tag) 对，调用方自行按可见性过滤。"""
    rows = conn.execute("SELECT note_path, tag FROM note_tags").fetchall()
    return [(r["note_path"], r["tag"]) for r in rows]


def distinct_owners(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT owner_username FROM note_registry ORDER BY owner_username"
    ).fetchall()
    return [r["owner_username"] for r in rows]


def list_all_notes(conn: sqlite3.Connection) -> list[dict]:
    """返回全部已注册笔记及其归属/共享信息（管理员用）"""
    rows = conn.execute(
        "SELECT note_path, owner_username FROM note_registry ORDER BY note_path"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        path = r["note_path"]
        out.append({
            "path": path,
            "owner": r["owner_username"],
            "public": is_public(conn, path),
            "shares": list_shares(conn, path),
        })
    return out


def owner_map(conn: sqlite3.Connection) -> dict[str, str]:
    """返回 {note_path: owner_username} 全量映射（供列表模板显示拥有者）"""
    rows = conn.execute(
        "SELECT note_path, owner_username FROM note_registry"
    ).fetchall()
    return {r["note_path"]: r["owner_username"] for r in rows}


# ── fork：笔记派生与溯源 ─────────────────────────────────


def register_fork(conn: sqlite3.Connection, fork_note_path: str, source_note_path: str) -> None:
    """登记一条 fork 关系：fork_note_path 由 source_note_path 派生（幂等）。"""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO note_forks(fork_note_path, source_note_path, created_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(fork_note_path)
        DO UPDATE SET source_note_path=excluded.source_note_path, updated_at=excluded.updated_at
        """,
        (fork_note_path, source_note_path, now, now),
    )
    conn.commit()


def get_fork_source(conn: sqlite3.Connection, fork_note_path: str) -> Optional[str]:
    """返回该笔记的来源笔记路径；若它不是 fork 则返回 None。"""
    row = conn.execute(
        "SELECT source_note_path FROM note_forks WHERE fork_note_path = ?", (fork_note_path,)
    ).fetchone()
    return row["source_note_path"] if row else None


def get_forks_of(conn: sqlite3.Connection, source_note_path: str) -> list[str]:
    """返回由该来源笔记派生出的全部 fork 笔记路径。"""
    rows = conn.execute(
        "SELECT fork_note_path FROM note_forks WHERE source_note_path = ? ORDER BY id",
        (source_note_path,),
    ).fetchall()
    return [r["fork_note_path"] for r in rows]


def delete_note_forks(conn: sqlite3.Connection, note_path: str) -> None:
    """删除引用该笔记的所有 fork 关系（它作为来源或被派生）。"""
    conn.execute("DELETE FROM note_forks WHERE fork_note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_forks WHERE source_note_path = ?", (note_path,))
    conn.commit()


__all__ = [
    "PUBLIC_USER",
    "safe_username",
    "register_note",
    "get_owner",
    "is_registered",
    "can_access",
    "can_edit",
    "share_note",
    "unshare_note",
    "list_shares",
    "list_shared_with_me",
    "list_owned_by",
    "list_accessible",
    "migrate_legacy_notes",
    "is_public",
    "set_public",
    "set_owner",
    "migrate_note_path",
    "normalize_tag",
    "list_tags",
    "add_tag",
    "remove_tag",
    "set_tags",
    "all_tag_pairs",
    "distinct_owners",
    "list_all_notes",
    "owner_map",
    "register_fork",
    "get_fork_source",
    "get_forks_of",
    "delete_note_forks",
]
