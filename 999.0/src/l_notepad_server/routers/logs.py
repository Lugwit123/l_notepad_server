# -*- coding: utf-8 -*-
"""服务器日志 API：/api/logs*（仅管理员）。

设计参考 Dozzle / Loki 的日志查看模式：
  - 列表：rglob 日志目录返回元信息
  - 读取：默认只读文件尾部（tail 字节），避免大文件整读进内存
  - 下载：FileResponse 流式返回
  - follow：SSE 增量推送新写入内容（1s 轮询文件追加）
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime as _dt
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .deps import require_admin

router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(require_admin)])

SERVER_LOG_DIR = Path(os.environ.get("L_NOTEPAD_LOG_DIR", r"D:\Temp\Log"))

# 单次返回内容上限（超过则从尾部截断），防止大日志打爆内存
DEFAULT_TAIL_BYTES = 512 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024


class LogEntryOut(BaseModel):
    path: str
    size: int
    mtime: str


class LogContentOut(BaseModel):
    path: str
    content: str
    total_size: int
    truncated: bool


class LogUpdate(BaseModel):
    content: str = Field(default="")


def _resolve_safe(log_path: str) -> Path:
    """把相对日志路径解析为日志目录内绝对路径，防目录穿越。"""
    log_root = SERVER_LOG_DIR.resolve()
    target = (SERVER_LOG_DIR / log_path.replace("/", os.sep)).resolve()
    if log_root not in target.parents and target != log_root:
        raise HTTPException(status_code=403, detail="Access denied")
    return target


def _read_tail(target: Path, tail_bytes: int | None) -> tuple[str, int, bool]:
    size = target.stat().st_size
    want = MAX_READ_BYTES if tail_bytes is None else min(tail_bytes, MAX_READ_BYTES)
    with target.open("rb") as f:
        if size > want:
            f.seek(-want, 2)
            data = f.read()
        else:
            f.seek(0)
            data = f.read()
    return data.decode("utf-8", errors="replace"), size, want < size


@router.get("", response_model=list[LogEntryOut])
def list_logs(max_size: int = 2 * 1024 * 1024) -> list[LogEntryOut]:
    log_root = SERVER_LOG_DIR
    if not log_root.exists() or not log_root.is_dir():
        return []
    result: list[LogEntryOut] = []
    for p in sorted(log_root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            st = p.stat()
            if st.st_size > max_size:
                continue
        except OSError:
            continue
        rel = p.relative_to(log_root).as_posix()
        result.append(
            LogEntryOut(
                path=rel,
                size=st.st_size,
                mtime=_dt.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            )
        )
    return result


@router.get("/{log_path:path}", response_model=LogContentOut)
def get_log(log_path: str, tail: int | None = None) -> LogContentOut:
    target = _resolve_safe(log_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        content, size, truncated = _read_tail(target, tail)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Read failed: {e}")
    return LogContentOut(path=log_path, content=content, total_size=size, truncated=truncated)


@router.get("/{log_path:path}/download")
def download_log(log_path: str) -> FileResponse:
    target = _resolve_safe(log_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(
        target,
        media_type="text/plain",
        filename=target.name,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


@router.put("/{log_path:path}")
def update_log(log_path: str, payload: LogUpdate) -> dict[str, Any]:
    target = _resolve_safe(log_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"ok": True}


@router.delete("/{log_path:path}")
def delete_log(log_path: str) -> dict[str, Any]:
    target = _resolve_safe(log_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"ok": True}


@router.get("/{log_path:path}/follow")
async def follow_log(log_path: str) -> StreamingResponse:
    """SSE 增量跟踪日志追加（Dozzle 式 tail -f）。

    每秒检查一次文件新增字节；文件被截断/轮转（size < pos）则从头读。
    客户端断开时由 StreamingResponse 取消生成器。
    """
    target = _resolve_safe(log_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")

    def event_stream() -> Iterator[str]:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pos = 0
        with target.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            yield 'data: {"connected": true}\n\n'
            while True:
                try:
                    size = target.stat().st_size
                except OSError:
                    size = 0
                if size < pos:  # 截断/轮转 → 重头读
                    pos = 0
                if size > pos:
                    f.seek(pos)
                    data = f.read()
                    if data:
                        pos += len(data)
                        # 增量解码：多字节字符跨块边界不乱码
                        text = decoder.decode(data)
                        if text:
                            yield "data: " + json.dumps({"data": text}) + "\n\n"
                        continue
                time.sleep(1.0)

    return StreamingResponse(
        run_sync_iter(event_stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def run_sync_iter(sync_iter: Iterator[str]) -> Any:
    """把同步生成器包装为异步迭代器（每个片段在线程池产出，不阻塞事件循环）。"""

    async def async_gen():
        while True:
            chunk = await run_in_threadpool(next, sync_iter, None)
            if chunk is None:
                return
            yield chunk

    return async_gen()
