# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from . import paths


# ── 本地笔记变更钩子：云端镜像同步（cloud_sync）注册进来，笔记增删改后据此推送 ──
_note_change_hook: "Callable[[str, str], None] | None" = None


def set_note_change_hook(hook: "Callable[[str, str], None] | None") -> None:
    """注册笔记增删改回调（action: upsert/delete, rel_path）。None 表示卸载。"""
    global _note_change_hook
    _note_change_hook = hook


def _notify_note_change(action: str, rel_path: str) -> None:
    hook = _note_change_hook
    if hook is not None:
        try:
            hook(action, rel_path)
        except Exception:
            pass


@dataclass(frozen=True)
class FileNoteMeta:
    """List view metadata without reading full file content."""

    path: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FileNote:
    path: str  # posix-like relative path under notepad_list
    title: str  # file name
    content: str
    created_at: str
    updated_at: str

    @property
    def is_markdown(self) -> bool:
        v = (self.title or "").strip().lower()
        return v.endswith(".md") or v.endswith(".mdc")

    @property
    def is_mindmap(self) -> bool:
        """脑图专属格式（.mmd）：整篇文件即一份脑图大纲。"""
        return (self.title or "").strip().lower().endswith(".mmd")

    def content_snippet(self, max_len: int = 220) -> str:
        s = (self.content or "").replace("\r\n", "\n").strip()
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + "…"


_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f]+")


def default_root_dir() -> Path:
    return paths.notes_dir()


def ensure_root(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def sanitize_title_to_filename(title: str) -> str:
    """
    Windows-safe filename. Keep Chinese and most Unicode; remove control chars; replace reserved characters.
    Automatically adds .md extension if not present.
    """
    v = (title or "").strip()
    if not v:
        v = "未命名"
    v = _CONTROL_CHARS.sub("", v)
    v = _INVALID_FILENAME_CHARS.sub("_", v)
    v = v.strip(" .")
    v = v or "未命名"
    # 自动添加 .md 后缀（如果没有后缀）；.mmd 为脑图专属格式
    if not v.lower().endswith(('.md', '.mdc', '.mmd', '.txt', '.py', '.json', '.log')):
        v += ".md"
    return v


def normalize_rel_posix_path(rel_path: str) -> str:
    """
    Normalize a relative posix-like path and prevent path traversal.
    """
    rel = (rel_path or "").strip().lstrip("/").replace("\\", "/")
    p = PurePosixPath(rel)
    parts: list[str] = []
    for part in p.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("invalid path traversal")
        parts.append(part)
    if not parts:
        raise ValueError("empty path")
    return "/".join(parts)


def resolve_note_path(root_dir: Path, rel_posix_path: str) -> Path:
    rel = normalize_rel_posix_path(rel_posix_path)
    p = root_dir.joinpath(*rel.split("/")).resolve()
    root_real = root_dir.resolve()
    if root_real not in p.parents and p != root_real:
        raise ValueError("path escapes root")
    return p


def iter_note_files(root_dir: Path) -> Iterable[Path]:
    if not root_dir.exists():
        return []
    return (p for p in root_dir.rglob("*") if p.is_file())


def list_group_dirs(root_dir: Path) -> list[tuple[str, int]]:
    """列出全部分组目录（相对 posix 路径 + 直接子文件数），跳过隐藏/下划线目录。"""
    ensure_root(root_dir)
    out: list[tuple[str, int]] = []
    for p in sorted(root_dir.rglob("*")):
        if not p.is_dir():
            continue
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        rel = p.relative_to(root_dir).as_posix()
        if "/." in rel or "/_" in rel:
            continue
        try:
            count = sum(1 for f in p.iterdir() if f.is_file())
        except OSError:
            count = 0
        out.append((rel, count))
    return out


# 列表场景只需读文件头即可生成摘要，避免把整篇大文件读进内存。
_BRIEF_HEAD_CHARS = 8192


def _scan_files_sorted(root_dir: Path, limit: int) -> list[tuple[Path, os.stat_result]]:
    """扫描一次目录，每个文件只 stat 一次，按修改时间倒序返回受限列表。"""
    ensure_root(root_dir)
    entries: list[tuple[Path, os.stat_result]] = []
    for p in iter_note_files(root_dir):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((p, st))
    entries.sort(key=lambda e: e[1].st_mtime, reverse=True)
    return entries[: max(1, limit)]


def list_notes_meta(root_dir: Path, limit: int = 500) -> list[FileNoteMeta]:
    out: list[FileNoteMeta] = []
    for p, st in _scan_files_sorted(root_dir, limit):
        try:
            rel = p.relative_to(root_dir).as_posix()
        except Exception:
            continue
        out.append(
            FileNoteMeta(
                path=rel,
                title=rel,
                created_at=_iso_from_ts(st.st_ctime),
                updated_at=_iso_from_ts(st.st_mtime),
            )
        )
    return out


def list_notes_brief(root_dir: Path, limit: int = 500, *, head_chars: int = _BRIEF_HEAD_CHARS) -> list[FileNote]:
    """列出笔记，每个文件只读开头 head_chars 个字符用于生成摘要。

    适用于只展示标题/时间/摘要、不需要全文的列表场景。
    """
    out: list[FileNote] = []
    for p, st in _scan_files_sorted(root_dir, limit):
        try:
            rel = p.relative_to(root_dir).as_posix()
        except Exception:
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                content = f.read(head_chars)
        except Exception:
            content = ""
        out.append(
            FileNote(
                path=rel,
                title=rel,
                content=content,
                created_at=_iso_from_ts(st.st_ctime),
                updated_at=_iso_from_ts(st.st_mtime),
            )
        )
    return out


# ── 列表 TTL 缓存：避免每个请求都 rglob 全目录 + stat + 读文件头 ──
_BRIEF_CACHE_TTL = 3.0
_BRIEF_CACHE_LIMIT = 1000
_brief_cache: dict[Path, tuple[float, list[FileNote]]] = {}


def invalidate_brief_cache(root_dir: Path | None = None) -> None:
    """笔记增删改后调用；None 表示清空全部。"""
    if root_dir is None:
        _brief_cache.clear()
    else:
        _brief_cache.pop(root_dir, None)


def list_notes_brief_cached(root_dir: Path, limit: int = 500) -> list[FileNote]:
    """带 TTL 的列表缓存（Web 每页渲染多次调用此函数，避免重复扫描）。

    桌面端等外部写入靠 TTL 兜底（最多 3 秒陈旧）；本服务内的增删改会主动失效。
    """
    now = time.monotonic()
    hit = _brief_cache.get(root_dir)
    if hit is None or now - hit[0] >= _BRIEF_CACHE_TTL:
        hit = (now, list_notes_brief(root_dir, limit=_BRIEF_CACHE_LIMIT))
        _brief_cache[root_dir] = hit
    entries = hit[1]
    return entries[: max(1, limit)] if limit < len(entries) else entries


def read_text_capped(p: Path, max_bytes: int = 2 * 1024 * 1024) -> str:
    """读取文件内容，超过 max_bytes 截断（尾部），返回文本。"""
    with p.open("rb") as f:
        try:
            f.seek(0, 2)
            size = f.tell()
            if size > max_bytes:
                f.seek(-max_bytes, 2)
            else:
                f.seek(0)
        except OSError:
            f.seek(0)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def list_notes(root_dir: Path, limit: int = 500) -> list[FileNote]:
    out: list[FileNote] = []
    for p, st in _scan_files_sorted(root_dir, limit):
        try:
            rel = p.relative_to(root_dir).as_posix()
        except Exception:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            content = ""
        out.append(
            FileNote(
                path=rel,
                title=rel,
                content=content,
                created_at=_iso_from_ts(st.st_ctime),
                updated_at=_iso_from_ts(st.st_mtime),
            )
        )
    return out


def get_note(root_dir: Path, rel_posix_path: str) -> FileNote | None:
    ensure_root(root_dir)
    try:
        p = resolve_note_path(root_dir, rel_posix_path)
    except ValueError:
        return None
    if not p.exists() or not p.is_file():
        return None
    st = p.stat()
    try:
        content = p.read_text(encoding="utf-8")
    except Exception:
        content = ""
    rel = p.relative_to(root_dir).as_posix()
    return FileNote(
        path=rel,
        title=rel,
        content=content,
        created_at=_iso_from_ts(st.st_ctime),
        updated_at=_iso_from_ts(st.st_mtime),
    )


def _unique_path(root_dir: Path, target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for i in range(2, 10_000):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError("cannot find unique filename")


def _ensure_extension(filename: str, ext: str = ".md") -> str:
    """确保文件名有指定扩展名。"""
    if not filename.lower().endswith(('.md', '.mdc', '.mmd', '.txt', '.py', '.json', '.log')):
        return filename + ext
    return filename


def create_note(root_dir: Path, title: str, content: str, category_dir: str = "") -> FileNote:
    ensure_root(root_dir)
    filename = sanitize_title_to_filename(title)
    rel_dir = (category_dir or "").strip().lstrip("/").replace("\\", "/")
    rel_dir = str(PurePosixPath(rel_dir)) if rel_dir not in {"", "."} else ""
    if rel_dir.startswith(".."):
        raise ValueError("invalid category")
    base = root_dir.joinpath(
        *([p for p in rel_dir.split("/") if p] if rel_dir else []))
    base.mkdir(parents=True, exist_ok=True)
    target = _unique_path(root_dir, (base / filename))
    # newline="\n"：Windows 下 write_text 默认会把 \n 翻译成 \r\n，
    # 而表单提交的文本本身就是 \r\n，会叠成 \r\r\n（读回变双倍空行）
    target.write_text(content or "", encoding="utf-8", newline="\n")
    invalidate_brief_cache(root_dir)
    rel = target.relative_to(root_dir).as_posix()
    note = get_note(root_dir, rel)
    if not note:
        raise RuntimeError("failed to create note")
    _notify_note_change("upsert", note.path)
    return note


def update_note(
    root_dir: Path,
    rel_posix_path: str,
    *,
    new_title: str | None = None,
    new_content: str | None = None,
) -> FileNote | None:
    ensure_root(root_dir)
    note = get_note(root_dir, rel_posix_path)
    if not note:
        return None
    p = resolve_note_path(root_dir, note.path)
    if new_content is not None:
        p.write_text(new_content, encoding="utf-8", newline="\n")
    if new_title is not None:
        # 只使用文件名重命名，避免用户输入包含 '/' 的路径破坏文件名
        new_name_raw = new_title.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        new_name = sanitize_title_to_filename(new_name_raw)
        if new_name and new_name != p.name:
            new_p = _unique_path(root_dir, p.with_name(new_name))
            p.rename(new_p)
            p = new_p
    invalidate_brief_cache(root_dir)
    rel = p.relative_to(root_dir).as_posix()
    _notify_note_change("upsert", rel)
    return get_note(root_dir, rel)


def move_note(root_dir: Path, rel_posix_path: str, dst_dir: str) -> FileNote | None:
    """把笔记文件移动到 dst_dir 目录下（保持原文件名），返回新位置的笔记。"""
    ensure_root(root_dir)
    note = get_note(root_dir, rel_posix_path)
    if not note:
        return None
    src = resolve_note_path(root_dir, note.path)
    rel_dir = (dst_dir or "").strip().lstrip("/").replace("\\", "/")
    rel_dir = str(PurePosixPath(rel_dir)) if rel_dir not in {"", "."} else ""
    if rel_dir.startswith(".."):
        raise ValueError("invalid dest dir")
    base = (
        root_dir.joinpath(*[p for p in rel_dir.split("/") if p])
        if rel_dir
        else root_dir
    )
    base.mkdir(parents=True, exist_ok=True)
    target = base / src.name
    if target.resolve() == src.resolve():
        return get_note(root_dir, src.relative_to(root_dir).as_posix())
    target = _unique_path(root_dir, target)
    src.rename(target)
    invalidate_brief_cache(root_dir)
    new_rel = target.relative_to(root_dir).as_posix()
    _notify_note_change("delete", note.path)
    _notify_note_change("upsert", new_rel)
    return get_note(root_dir, new_rel)


def delete_note(root_dir: Path, rel_posix_path: str) -> bool:
    ensure_root(root_dir)
    try:
        p = resolve_note_path(root_dir, rel_posix_path)
    except ValueError:
        return False
    if not p.exists() or not p.is_file():
        return False
    rel = p.relative_to(root_dir).as_posix()
    _notify_note_change("delete", rel)
    p.unlink()
    invalidate_brief_cache(root_dir)
    # cleanup empty parents up to root
    cur = p.parent
    root_real = root_dir.resolve()
    while cur != root_real:
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent
    return True
