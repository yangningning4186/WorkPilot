"""幂等回填 Cowork PostgreSQL 数据，并在双读一致后生成启用标记。"""

from __future__ import annotations

import asyncio
import json

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.cowork_store.jsonl import JsonlConversationStore
from app.cowork_store.migration import migrate_and_verify
from app.cowork_store.sqlite import SqliteCoworkStore


async def run() -> None:
    settings = Settings()
    root = settings.cowork_data_path.expanduser().resolve()
    async with session_factory() as session:
        report = await migrate_and_verify(
            session,
            sqlite_store=SqliteCoworkStore(root / "cowork.db"),
            jsonl_store=JsonlConversationStore(root / "conversations"),
            report_path=root / "migration-report.json",
        )
    (root / "sqlite-ready").write_text(report.generated_at + "\n", encoding="utf-8")
    (root / "sqlite-ready").chmod(0o600)
    print(json.dumps({"ok": report.ok, "tables": len(report.tables) + 1}, ensure_ascii=False))
    await close_database()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
