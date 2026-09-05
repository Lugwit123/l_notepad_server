# -*- coding: utf-8 -*-
"""l_notepad 知识库服务（多知识库）

把笔记快照发布为知识库文章，按预设层级（目录树）归类。
- 支持多个知识库：每个知识库有自己的路由 /web/kb/{name}，含多篇文章与目录层级。
- 文章与源笔记解耦：发布时快照 title/content，源笔记删除不影响知识库；
  更新源笔记后需重新发布（覆盖快照）才能同步到知识库。
- 兼容旧数据：无 kb_name 的文章/层级自动归入默认知识库 "default"。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

_DEFAULT_BASE = "default"
_DEFAULT_BASE_TITLE = "默认知识库"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kb(name: Optional[str]) -> str:
    """知识库逻辑名，空 → 默认知识库。"""
    return (name or "").strip() or _DEFAULT_BASE


def _slugify(name: str) -> str:
    """知识库路由键：仅保留字母数字 . - _（路径段安全，不含 /）。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-")


# ── 知识库 ────────────────────────────────────────────────


def ensure_default_base(conn: sqlite3.Connection) -> None:
    """确保默认知识库存在，并把遗留（kb_name=''）文章/层级归入默认知识库（幂等）。"""
    now = _now_iso()
    conn.execute(
        "INSERT INTO knowledge_bases(name, title, description, created_at, updated_at) "
        "VALUES(?, ?, '', ?, ?) ON CONFLICT(name) DO NOTHING",
        (_DEFAULT_BASE, _DEFAULT_BASE_TITLE, now, now),
    )
    conn.execute(
        "UPDATE knowledge_categories SET kb_name = ? WHERE kb_name = ''", (_DEFAULT_BASE,)
    )
    conn.execute(
        "UPDATE knowledge_articles SET kb_name = ? WHERE kb_name = ''", (_DEFAULT_BASE,)
    )
    conn.commit()


def create_base(conn: sqlite3.Connection, name: str, title: str = "", description: str = "") -> bool:
    """新建知识库；name 会净化成路由键。返回是否成功（重名/空名失败）。"""
    ensure_default_base(conn)
    slug = _slugify(name)
    if not slug:
        return False
    now = _now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO knowledge_bases(name, title, description, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (slug, (title or slug).strip(), (description or "").strip(), now, now),
    )
    conn.commit()
    return cur.rowcount > 0


def list_bases(conn: sqlite3.Connection) -> list[dict]:
    """全部知识库（含文章数 / 层级数）。"""
    ensure_default_base(conn)
    rows = conn.execute(
        """
        SELECT b.name, b.title, b.description, b.created_at, b.updated_at,
               (SELECT COUNT(*) FROM knowledge_articles a WHERE a.kb_name = b.name) AS article_count,
               (SELECT COUNT(*) FROM knowledge_categories c WHERE c.kb_name = b.name) AS category_count
        FROM knowledge_bases b
        ORDER BY b.created_at, b.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_base(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM knowledge_bases WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def set_workspace(conn: sqlite3.Connection, name: str, workspace: str) -> bool:
    """设置某知识库的工作区本地目录（可预览其中的 .md 文档）。返回是否更新。"""
    kb = _kb(name)
    if kb == _DEFAULT_BASE:
        # 默认知识库不强制限制，但保留该分支以免误删逻辑
        pass
    now = _now_iso()
    cur = conn.execute(
        "UPDATE knowledge_bases SET workspace = ?, updated_at = ? WHERE name = ?",
        ((workspace or "").strip(), now, kb),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_base(conn: sqlite3.Connection, name: str) -> bool:
    """删除知识库及其全部文章与层级；默认知识库不可删。"""
    kb = _kb(name)
    if kb == _DEFAULT_BASE:
        return False
    cur = conn.execute("DELETE FROM knowledge_bases WHERE name = ?", (kb,))
    conn.execute("DELETE FROM knowledge_articles WHERE kb_name = ?", (kb,))
    conn.execute("DELETE FROM knowledge_categories WHERE kb_name = ?", (kb,))
    conn.commit()
    return cur.rowcount > 0


# ── 层级（预设目录）───────────────────────────────────────


def normalize_path(path: Optional[str]) -> str:
    """层级路径归一化：去空白/首尾斜杠/重复斜杠，最长 128 字符。"""
    v = str(path or "")
    v = "/".join(p.strip() for p in v.split("/") if p.strip())
    return v[:128]


def list_categories(conn: sqlite3.Connection, kb_name: str = "") -> list[str]:
    """某知识库已有层级（升序）。"""
    kb = _kb(kb_name)
    rows = conn.execute(
        "SELECT path FROM knowledge_categories WHERE kb_name = ? ORDER BY sort, path", (kb,)
    ).fetchall()
    return [r["path"] for r in rows]


def ensure_category(conn: sqlite3.Connection, kb_name: str, path: str) -> str:
    """确保某知识库层级存在（幂等，返回归一化路径）。"""
    kb = _kb(kb_name)
    p = normalize_path(path)
    if not p:
        return ""
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO knowledge_categories(kb_name, path, sort, created_at, updated_at)
        VALUES(?, ?, 0, ?, ?)
        ON CONFLICT(kb_name, path) DO NOTHING
        """,
        (kb, p, now, now),
    )
    conn.commit()
    return p


def rename_category(conn: sqlite3.Connection, kb_name: str, old: str, new: str) -> bool:
    """重命名某知识库层级（同步迁移该层级下所有文章的 category_path 前缀）。"""
    kb = _kb(kb_name)
    old = normalize_path(old)
    new = normalize_path(new)
    if not old or not new or old == new:
        return False
    # 目标路径已存在且不是 old 本身 → 冲突，避免 UNIQUE(kb_name, path) 失败
    if conn.execute(
        "SELECT 1 FROM knowledge_categories WHERE kb_name = ? AND path = ? AND path != ?",
        (kb, new, old),
    ).fetchone():
        return False
    now = _now_iso()
    cur = conn.execute(
        "UPDATE knowledge_categories SET path = ?, updated_at = ? WHERE kb_name = ? AND path = ?",
        (new, now, kb, old),
    )
    prefix_old = old + "/"
    conn.execute(
        "UPDATE knowledge_articles SET category_path = ? WHERE kb_name = ? AND category_path = ?",
        (new, kb, old),
    )
    conn.execute(
        "UPDATE knowledge_articles SET category_path = ? || substr(category_path, ?) "
        "WHERE kb_name = ? AND category_path LIKE ?",
        (new, len(prefix_old) + 1, kb, prefix_old + "%"),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_category(conn: sqlite3.Connection, kb_name: str, path: str) -> bool:
    """删除某知识库层级（仅目录本身，文章保留但归入空类别）。"""
    kb = _kb(kb_name)
    path = normalize_path(path)
    if not path:
        return False
    now = _now_iso()
    prefix = path + "/"
    cur = conn.execute(
        "DELETE FROM knowledge_categories WHERE kb_name = ? AND (path = ? OR path LIKE ?)",
        (kb, path, prefix + "%"),
    )
    conn.execute(
        "UPDATE knowledge_articles SET category_path = '' WHERE kb_name = ? AND category_path = ?",
        (kb, path),
    )
    conn.execute(
        "UPDATE knowledge_articles SET category_path = substr(category_path, ?) "
        "WHERE kb_name = ? AND category_path LIKE ?",
        (len(prefix) + 1, kb, prefix + "%"),
    )
    conn.commit()
    return cur.rowcount > 0


# ── 文章 ──────────────────────────────────────────────────


def publish(
    conn: sqlite3.Connection,
    kb_name: str,
    note_path: str,
    title: str,
    content: str,
    category: str = "",
    owner: str = "",
    is_public: bool = True,
    workspace_rel: str = "",
) -> str:
    """发布笔记快照到某知识库（幂等：同 note_path 覆盖更新）。返回 note_path。

    workspace_rel：从个人笔记复制到知识库工作区的文件相对路径（非空 ⇒ 打「来自个人笔记」标记）。
    """
    kb = _kb(kb_name)
    cat = normalize_path(category)
    if cat:
        ensure_category(conn, kb, cat)
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO knowledge_articles(kb_name, note_path, title, content, category_path, workspace_rel, owner_username, is_public, published_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(note_path) DO UPDATE SET
          kb_name=excluded.kb_name, title=excluded.title, content=excluded.content,
          category_path=excluded.category_path, workspace_rel=excluded.workspace_rel,
          is_public=excluded.is_public, updated_at=excluded.updated_at
        """,
        (kb, note_path, title, content, cat, workspace_rel, owner, 1 if is_public else 0, now, now),
    )
    conn.commit()
    return note_path


def unpublish(conn: sqlite3.Connection, kb_name: str, note_path: str) -> bool:
    cur = conn.execute(
        "DELETE FROM knowledge_articles WHERE kb_name = ? AND note_path = ?",
        (_kb(kb_name), note_path),
    )
    conn.commit()
    return cur.rowcount > 0


def get_article(conn: sqlite3.Connection, kb_name: str, note_path: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM knowledge_articles WHERE kb_name = ? AND note_path = ?",
        (_kb(kb_name), note_path),
    ).fetchone()
    return dict(row) if row else None


def is_published(conn: sqlite3.Connection, kb_name: str, note_path: str) -> bool:
    return get_article(conn, kb_name, note_path) is not None


def list_articles(conn: sqlite3.Connection, kb_name: str = "", *, only_public: bool = False) -> list[dict]:
    """某知识库文章列表（按层级 + 更新时间倒序）。"""
    kb = _kb(kb_name)
    sql = "SELECT * FROM knowledge_articles WHERE kb_name = ?"
    if only_public:
        sql += " AND is_public = 1"
    sql += " ORDER BY category_path, updated_at DESC"
    return [dict(r) for r in conn.execute(sql, (kb,)).fetchall()]


def list_published_paths(conn: sqlite3.Connection, kb_name: str = "") -> set[str]:
    rows = conn.execute(
        "SELECT note_path FROM knowledge_articles WHERE kb_name = ?", (_kb(kb_name),)
    ).fetchall()
    return {r["note_path"] for r in rows}


def list_shared_rels(conn: sqlite3.Connection, kb_name: str = "") -> set[str]:
    """某知识库内由「分享个人笔记」复制而来（已打标）的工作区相对路径集合。"""
    rows = conn.execute(
        "SELECT workspace_rel FROM knowledge_articles WHERE kb_name = ? AND workspace_rel != ''",
        (_kb(kb_name),),
    ).fetchall()
    return {r["workspace_rel"] for r in rows}


def delete_note_cleanup(conn: sqlite3.Connection, note_path: str) -> None:
    """删除源笔记时移除其知识库文章（全局：note_path 唯一）。"""
    conn.execute("DELETE FROM knowledge_articles WHERE note_path = ?", (note_path,))
    conn.commit()


__all__ = [
    "normalize_path",
    "ensure_default_base",
    "create_base",
    "list_bases",
    "get_base",
    "set_workspace",
    "delete_base",
    "list_categories",
    "ensure_category",
    "rename_category",
    "delete_category",
    "publish",
    "unpublish",
    "get_article",
    "is_published",
    "list_articles",
    "list_published_paths",
    "list_shared_rels",
    "delete_note_cleanup",
]
