# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import paths


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_updated_at ON notes(updated_at DESC);

-- 笔记归属注册表：文件系统存内容，本表登记拥有者（note_path 含 owner 前缀，如 admin01/xxx.md）
CREATE TABLE IF NOT EXISTS note_registry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note_path TEXT NOT NULL UNIQUE,
  owner_username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 笔记共享关系：owner 把笔记共享给指定用户，permission = 'read' | 'write'
CREATE TABLE IF NOT EXISTS note_shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note_path TEXT NOT NULL,
  shared_with TEXT NOT NULL,
  permission TEXT NOT NULL DEFAULT 'read',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(note_path, shared_with)
);

CREATE INDEX IF NOT EXISTS idx_note_shares_shared_with ON note_shares(shared_with);

-- 笔记标签：一篇笔记多个标签，一个标签多篇笔记
CREATE TABLE IF NOT EXISTS note_tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note_path TEXT NOT NULL,
  tag TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(note_path, tag)
);

CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag);

-- 笔记 fork：记录派生笔记（fork_note_path）的来源笔记（source_note_path），用于溯源对比
CREATE TABLE IF NOT EXISTS note_forks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fork_note_path TEXT NOT NULL UNIQUE,
  source_note_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_forks_source ON note_forks(source_note_path);

-- 知识库：一个知识库可含多篇文章与目录层级，各有自己的路由 /web/kb/{name}
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,            -- 路由键 / 逻辑名（如 tech-docs）
  title TEXT NOT NULL,                  -- 展示名
  description TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL DEFAULT '',   -- 工作区本地目录（可预览其中的 .md 文档）
  depot_library TEXT NOT NULL DEFAULT '/notes',  -- 归档到的 depot 库（逻辑路径首段）
  depot_subpath TEXT NOT NULL DEFAULT '',        -- 库内子路径；空 = 用知识库名
  depot_ws TEXT NOT NULL DEFAULT '',             -- depot 工作区名（空 = 用 notes-sync）
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 知识库层级（预设目录树）：一篇知识文章归属一个目录层级（按知识库作用域）
CREATE TABLE IF NOT EXISTS knowledge_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_name TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(kb_name, path)
);

-- 知识库文章：把笔记快照发布到知识库，按预设层级归类（按知识库作用域）
CREATE TABLE IF NOT EXISTS knowledge_articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_name TEXT NOT NULL DEFAULT '',
  note_path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  category_path TEXT NOT NULL DEFAULT '',
  workspace_rel TEXT NOT NULL DEFAULT '',  -- 工作区相对路径：从个人笔记复制来的知识库文章
  owner_username TEXT NOT NULL,
  is_public INTEGER NOT NULL DEFAULT 1,
  published_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_articles_category ON knowledge_articles(kb_name, category_path);
CREATE INDEX IF NOT EXISTS idx_knowledge_articles_owner ON knowledge_articles(kb_name, owner_username);

-- 全文搜索索引文档表：登记已索引的文件（增量比对字段见下，body 原文用于生成摘要），
-- rowid 与 search_fts 的 rowid 一一对应（FTS5 只能按 rowid 增删）。
-- source='note'（个人笔记，本机文件，size/mtime 比对）/ 'kb'（知识库归档内容，来自 depot
-- 已上传版本，size/rev 比对；本机工作区目录不参与索引）
CREATE TABLE IF NOT EXISTS search_docs (
  rowid INTEGER PRIMARY KEY,
  note_path TEXT NOT NULL UNIQUE,       -- 索引键：笔记用 rel；知识库用 kb:<kb_name>:<rel>
  source TEXT NOT NULL DEFAULT 'note',
  kb_name TEXT NOT NULL DEFAULT '',
  rel TEXT NOT NULL DEFAULT '',         -- 展示/打开用的相对路径
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  mtime REAL NOT NULL DEFAULT 0,
  rev INTEGER NOT NULL DEFAULT 0,       -- depot 归档版本号（source='kb' 的增量比对字段）
  indexed_at TEXT NOT NULL
);

-- 倒排索引（FTS5）：写入前中文已按二元切分（unicode61 不会切汉字，整段汉字会变成一个 token）
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  title, body, note_path UNINDEXED, tokenize='unicode61 remove_diacritics 2'
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    root = os.environ.get("L_NOTEPAD_ROOT")
    if root:
        return Path(root) / "data" / "notepad.sqlite3"
    # 无环境变量时统一存放于 ~/.Lugwit/l_notepad（迁移 cwd 下的旧库，幂等）
    new = paths.data_root() / "notepad.sqlite3"
    paths.move_file(Path.cwd() / "notepad.sqlite3", new)
    return new


def connect(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接：WAL + busy_timeout，多线程/多请求并发安全。

    WAL 允许读写并发（读不阻塞写）；busy_timeout 避免写冲突时立刻抛
    database is locked。每请求独立连接（见 request_conn），避免跨线程共享
    同一连接对象的竞态。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """幂等补列：旧库升级到多知识库需要给已存在的表加 kb_name 列。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    # 多知识库迁移：给旧表补 kb_name 列（空 = 默认知识库，由 knowledge 模块统一归并）
    _ensure_column(conn, "knowledge_categories", "kb_name", "kb_name TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "knowledge_articles", "kb_name", "kb_name TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "knowledge_articles", "workspace_rel", "workspace_rel TEXT NOT NULL DEFAULT ''")
    # 工作区迁移：给知识库补 workspace 列（空 = 未配置工作区）
    _ensure_column(conn, "knowledge_bases", "workspace", "workspace TEXT NOT NULL DEFAULT ''")
    # depot 归档映射迁移：库（默认 /notes）+ 库内子路径（空=知识库名）+ 工作区名
    _ensure_column(conn, "knowledge_bases", "depot_library", "depot_library TEXT NOT NULL DEFAULT '/notes'")
    _ensure_column(conn, "knowledge_bases", "depot_subpath", "depot_subpath TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "knowledge_bases", "depot_ws", "depot_ws TEXT NOT NULL DEFAULT ''")
    # 搜索索引迁移：补来源列（旧行全是个人笔记）+ 归档版本列（知识库源改从 depot 取）
    _ensure_column(conn, "search_docs", "source", "source TEXT NOT NULL DEFAULT 'note'")
    _ensure_column(conn, "search_docs", "kb_name", "kb_name TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "search_docs", "rel", "rel TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "search_docs", "rev", "rev INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE search_docs SET rel = note_path WHERE rel = '' AND source = 'note'")
    conn.commit()


@contextmanager
def request_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """每请求一个连接（FastAPI Depends 用），结束即关闭。"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
