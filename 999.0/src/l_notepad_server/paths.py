# -*- coding: utf-8 -*-
"""统一的数据目录管理。

所有本地离线数据统一存放在数据根目录（由 wuwo config.yaml 的 data_dir
模板决定，默认 ~/.Lugwit/<包名>）下：

    ~/.Lugwit/l_notepad/
    ├── notepad_list/            笔记（.md 等文件，含 _images/）
    ├── favorites/               收藏夹 + 剪贴板历史 + 热键配置（原 %APPDATA%/l_folder_favorites）
    ├── version_history.sqlite3  笔记版本历史
    ├── external_files.json      外部文件状态
    ├── note_order.json          笔记手动排序
    ├── notepad.sqlite3          服务端笔记库（backend_server 默认兜底位置）
    ├── account_favorites.json   账号收藏（原本就在此目录）
    ├── account_custom_fields.json
    ├── .accounts_key            账号加密密钥
    ├── auth_token.json          登录令牌
    └── remembered_login.json    「记住账号密码」勾选后回填的账号/明文密码

Windows 文件系统大小写不敏感，~/.Lugwit 与已存在的 ~/.lugwit 是同一目录，
账号数据无需搬迁。笔记/收藏夹等旧位置的数据在各访问函数首次调用时
自动一次性迁移（幂等：目标已存在的条目跳过）。

注意：本模块不得依赖 PySide6/Qt，以便非 UI 模块（file_store、history_store、
backend_server 等）直接使用。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# 程序包目录（旧数据位置：notepad_list/、version_history.sqlite3、external_files.json）
_PKG_DIR = Path(__file__).resolve().parent

# 包名：优先取导入包名，兜底从布局 <name>/<ver>/src/<name>/ 推导
_PKG_NAME = str(__package__ or "").split(".")[0] or _PKG_DIR.parents[2].name

# 数据根目录缓存（首次解析后固定，避免每条数据访问都读一次配置文件）
_data_root_cache: Path | None = None


def _find_config_file() -> Path | None:
    """定位 wuwo 的 config.yaml（优先级：环境变量 > 包目录向上遍历）。"""
    raw = os.environ.get("WUWO_CONFIG_FILE", "")
    if raw and Path(raw).is_file():
        return Path(raw)
    cfg_dir = os.environ.get("WUWO_CONFIG_DIR", "")
    if cfg_dir:
        cand = Path(cfg_dir) / "config.yaml"
        if cand.is_file():
            return cand
    for parent in _PKG_DIR.parents:
        for cand in (
            parent / "wuwo" / "config" / "config.yaml",
            parent / "config" / "config.yaml",
        ):
            if cand.is_file():
                return cand
    return None


def _read_data_dir_template() -> str:
    """从 config.yaml 读取顶层 data_dir 模板，失败返回空字符串。"""
    cfg = _find_config_file()
    if cfg is None:
        return ""
    try:
        text = cfg.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(r'^\s*data_dir\s*:\s*["\']?([^"\'\n]+)["\']?\s*$', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _resolve_data_root() -> Path:
    """解析数据根目录：config.yaml 的 data_dir 模板 → 实际路径。

    模板占位符：{user}=用户主目录，{pkg}=包名；解析失败回退默认 ~/.Lugwit/<包名>。
    """
    tmpl = _read_data_dir_template()
    if tmpl:
        try:
            resolved = (
                tmpl.replace("{user}", str(Path.home()))
                .replace("{pkg}", _PKG_NAME)
                .replace("\\", "/")
                .rstrip("/")
            )
            root = Path(resolved)
            if root.is_absolute():
                return root
        except Exception:
            pass
    return Path.home() / ".Lugwit" / _PKG_NAME


def data_root() -> Path:
    """本地离线数据根目录（默认 ~/.Lugwit/<包名>，可被 config.yaml 的 data_dir 覆盖）。"""
    global _data_root_cache
    if _data_root_cache is None:
        _data_root_cache = _resolve_data_root()
        _data_root_cache.mkdir(parents=True, exist_ok=True)
    return _data_root_cache


def _is_empty_dir(d: Path) -> bool:
    try:
        return not any(d.iterdir())
    except OSError:
        return True


def move_dir_contents(old: Path, new: Path) -> None:
    """把 old 目录下的顶层条目移动到 new（目标已存在的同名条目跳过）。"""
    if not old.is_dir():
        return
    try:
        new.mkdir(parents=True, exist_ok=True)
        for entry in list(old.iterdir()):
            target = new / entry.name
            if target.exists():
                continue
            try:
                shutil.move(str(entry), str(target))
            except Exception:
                pass
    except Exception:
        pass


def move_file(old: Path, new: Path) -> None:
    """旧文件移动到新路径（源不存在或目标已存在则跳过）。"""
    try:
        if old.is_file() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
    except Exception:
        pass


def notes_dir() -> Path:
    """笔记目录。旧位置：程序包内 notepad_list/。"""
    d = data_root() / "notepad_list"
    if not d.exists() or _is_empty_dir(d):
        move_dir_contents(_PKG_DIR / "notepad_list", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def favorites_dir() -> Path:
    """收藏夹/剪贴板历史目录。旧位置：%APPDATA%/l_folder_favorites。"""
    d = data_root() / "favorites"
    if not d.exists() or _is_empty_dir(d):
        app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
        move_dir_contents(Path(app_data) / "l_folder_favorites", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def version_history_db() -> Path:
    """笔记版本历史数据库。旧位置：程序包内 version_history.sqlite3。"""
    f = data_root() / "version_history.sqlite3"
    move_file(_PKG_DIR / "version_history.sqlite3", f)
    return f


def external_files_state_file() -> Path:
    """外部文件状态。旧位置：程序包内 external_files.json。"""
    f = data_root() / "external_files.json"
    move_file(_PKG_DIR / "external_files.json", f)
    return f


def note_order_file() -> Path:
    """笔记手动排序缓存。旧位置（AppConfigLocation）由 ui.py 调 move_file 迁移。"""
    return data_root() / "note_order.json"
