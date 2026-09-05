# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db as dbmod
from . import file_store
from pytracemp import lprint


def _parse_iso(ts: str) -> float | None:
    try:
        # db.py stores timezone-aware ISO in UTC by default
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def migrate(db_path: Path, root_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)

    file_store.ensure_root(root_dir)
    notes = conn.execute("SELECT id, title, content, created_at, updated_at FROM notes ORDER BY id ASC").fetchall()

    report_dir = root_dir / "_migration"
    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    ok = 0
    for r in notes:
        note_id = int(r["id"])
        title = str(r["title"] or "").strip() or f"未命名_{note_id}"
        content = str(r["content"] or "")
        created_at = str(r["created_at"] or "")
        updated_at = str(r["updated_at"] or "")

        filename = file_store.sanitize_title_to_filename(title)
        # keep explicit markdown extensions if present in title
        # (sanitize_title_to_filename keeps suffix)
        target = root_dir / filename
        target = file_store._unique_path(root_dir, target)  # type: ignore[attr-defined]

        rel = target.relative_to(root_dir).as_posix()
        items.append(
            {
                "sqlite_id": note_id,
                "title": title,
                "target_rel_path": rel,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

        if dry_run:
            ok += 1
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        ts = _parse_iso(updated_at) or _parse_iso(created_at)
        if ts is not None:
            try:
                os.utime(target, (ts, ts))
            except Exception:
                pass
        ok += 1

    summary = {
        "db_path": str(db_path),
        "root_dir": str(root_dir),
        "dry_run": dry_run,
        "sqlite_count": len(notes),
        "exported_count": ok,
        "items": items[:200],  # cap inline for readability
        "items_total": len(items),
    }

    if not dry_run:
        (report_dir / "migration_report.json").write_text(
            json.dumps(
                {
                    **summary,
                    "items": items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate l_notepad SQLite notes to notepad_list file tree")
    parser.add_argument("--db", default=None, help="sqlite db path (default: L_NOTEPAD_DB or package default)")
    parser.add_argument("--root", default=None, help="notepad_list root dir (default: package src/l_notepad/notepad_list)")
    parser.add_argument("--dry-run", action="store_true", help="only compute plan, do not write files")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else (Path(os.environ.get("L_NOTEPAD_DB")) if os.environ.get("L_NOTEPAD_DB") else dbmod.default_db_path())
    root_dir = Path(args.root) if args.root else file_store.default_root_dir()

    summary = migrate(db_path, root_dir, dry_run=bool(args.dry_run))
    lprint(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

