"""Seed a demo workspace + a few sample tasks for the localhost demo.

Run inside the core container:
    docker compose -f docker-compose.demo.yml exec core python /app/scripts/seed-demo.py
"""
from __future__ import annotations

import asyncio

from taskdeck_core.db.engine import (
    get_sessionmaker_for_tests,  # named "for_tests" but logic is fine for one-shot scripts
)
from taskdeck_core.db.models import Task, Workspace


async def _seed() -> None:
    sm = await get_sessionmaker_for_tests()
    async with sm() as db:
        ws = Workspace(slug="demo", name="Demo workspace")
        db.add(ws)
        await db.flush()

        rows = [
            ("Echo a marker file", "echo demo > /tmp/taskdeck-demo.txt", "done", 0),
            ("Sleep then echo", "sleep 60 && echo done", "running", None),
            ("Failing command", "exit 7", "failed", 7),
        ]
        for title, prompt, status, exit_code in rows:
            t = Task(
                workspace_id=ws.id,
                title=title,
                prompt=prompt,
                origin="web",
                agent="shell",
                status=status,
                exit_code=exit_code,
            )
            db.add(t)
        await db.commit()
        await db.refresh(ws)
        print(f"Seeded workspace slug={ws.slug} id={ws.id}")
        print("Open http://localhost:5173 and select 'demo' from the workspace picker.")


if __name__ == "__main__":
    asyncio.run(_seed())
