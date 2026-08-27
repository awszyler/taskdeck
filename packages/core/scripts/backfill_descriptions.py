#!/usr/bin/env python
"""Backfill task.title + task.description for old rows.

Before the parsing-UX rework (docs/parsing-ux-rework.md) tasks had only a
title, and the parser sometimes pasted the whole user message into it.
This script re-summarizes those rows: it calls the same intent parser the
live app uses to produce a clean ≤60-char title plus a ≤280-char
description, and writes both back.

Scope: rows where `description IS NULL` (never backfilled) AND created
before the deploy. We re-derive from `raw_input` when present (the
original user utterance), else from the existing `prompt`.

Safety:
- `--dry-run` prints what WOULD change, writes nothing.
- `--limit N` processes at most N rows (run incrementally).
- Idempotent: a row with a non-null description is skipped, so
  re-running only picks up the remainder.
- `--prefer-longer-title` keeps the existing title when the model's new
  one is shorter AND the old one already fits in 60 chars (avoids
  "improving" titles that were already fine).

Usage:
    uv run python -m scripts.backfill_descriptions --dry-run --limit 10
    uv run python -m scripts.backfill_descriptions --limit 50
    uv run python -m scripts.backfill_descriptions            # full sweep
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from taskdeck_core.db.models import Task
from taskdeck_core.intent.parser import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    IntentParser,
    OpenAIClient,
    _clip,
    _derive_title_description,
)
from taskdeck_core.intent.schema import IntentContext, IntentInput
from taskdeck_core.settings import Settings

log = logging.getLogger("backfill_descriptions")


def _build_parser(s: Settings) -> IntentParser | None:
    """Construct the intent parser the same way main.py does. Returns
    None when no LLM backend is configured (script can still run with
    --heuristic-only)."""
    if s.bedrock_intent_enabled:
        from taskdeck_core.intent.bedrock_client import BedrockIntentClient

        client: object = BedrockIntentClient(
            model_id=s.bedrock_intent_model,
            region=s.bedrock_intent_region,
        )
        model = s.bedrock_intent_model
    elif s.litellm_base_url and s.litellm_api_key:
        client = OpenAIClient(base_url=s.litellm_base_url, api_key=s.litellm_api_key)
        model = s.intent_parser_model
    else:
        return None
    return IntentParser(
        client=client,  # type: ignore[arg-type]
        model=model,
        timeout=s.intent_parser_timeout_seconds,
    )


async def _summarize(
    parser: IntentParser | None,
    *,
    raw: str,
    caps: list[dict[str, str]],
) -> tuple[str, str | None]:
    """Return (title, description) for a row's source text. Uses the LLM
    parser when available; falls back to the deterministic heuristic
    (first line → title, clip → description) otherwise or on parser error."""
    if parser is None or not caps:
        return _derive_title_description(raw)
    try:
        parsed = await parser.parse(
            IntentInput(raw_input=raw, hint="text", context=IntentContext()),
            available_capabilities=caps,
        )
        title = _clip(parsed.title.strip() or _derive_title_description(raw)[0], TITLE_MAX)
        desc = parsed.description
        if desc and len(desc) > DESCRIPTION_MAX:
            desc = _clip(desc, DESCRIPTION_MAX)
        return title, desc
    except Exception as e:  # noqa: BLE001
        log.warning("parser failed on a row, using heuristic: %s", e)
        return _derive_title_description(raw)


async def run(args: argparse.Namespace) -> int:
    s = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(s.database_url, echo=False)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    parser = None if args.heuristic_only else _build_parser(s)
    if parser is None and not args.heuristic_only:
        log.warning(
            "no LLM backend configured; falling back to heuristic summaries"
        )

    # The parser needs a capability list to choose an agent, but here we
    # only care about title/description — agent routing is irrelevant for
    # a backfill. Pass a minimal catalogue so the tool schema is valid.
    caps = [{"capability": "claude-code", "description": "general agent"}]

    # Only rows that actually NEED a description: title > TITLE_MAX
    # (the parser pasted a long prompt into title) OR title is short but
    # the source text is long enough to warrant a subtitle. Short tasks
    # whose source already fits in the title (e.g. "echo hi") get NO
    # description and must NOT be re-selected — otherwise, since a None
    # description writes NULL, a "description IS NULL" scan would loop
    # over them forever, never converging.
    async with sm() as session:
        title_len = func.char_length(Task.title)
        source_len = func.char_length(
            func.coalesce(Task.raw_input, Task.prompt, Task.title)
        )
        stmt = (
            select(Task)
            .where(
                Task.description.is_(None),
                (title_len > TITLE_MAX) | (source_len > TITLE_MAX),
            )
            .order_by(Task.created_at.asc())
        )
        if args.limit:
            stmt = stmt.limit(args.limit)
        rows = list((await session.scalars(stmt)).all())

    log.info("found %d task(s) to backfill", len(rows))
    changed = 0

    for t in rows:
        source = (t.raw_input or t.prompt or t.title or "").strip()
        if not source:
            continue
        new_title, new_desc = await _summarize(parser, raw=source, caps=caps)
        # Every row we select here has a long source (query guarantees it),
        # so it warrants a description. If the model returned none, fall
        # back to a clipped source so the row is marked done and never
        # re-selected by the "description IS NULL" scan.
        if not new_desc:
            new_desc = _clip(source, DESCRIPTION_MAX)

        keep_title = (
            args.prefer_longer_title
            and len(t.title) <= TITLE_MAX
            and len(new_title) < len(t.title)
        )
        final_title = t.title if keep_title else new_title

        log.info(
            "task %s\n  old title: %s\n  new title: %s%s\n  new desc:  %s",
            t.id,
            t.title,
            final_title,
            " (kept old)" if keep_title else "",
            (new_desc or "")[:120],
        )

        if not args.dry_run:
            async with sm() as ws:
                row = await ws.get(Task, t.id)
                if row is None or row.description is not None:
                    continue  # raced with another runner; skip
                row.title = final_title
                row.description = new_desc
                await ws.commit()
            changed += 1

    await engine.dispose()
    log.info(
        "%s: %d row(s) %s",
        "DRY RUN" if args.dry_run else "DONE",
        changed if not args.dry_run else len(rows),
        "would change" if args.dry_run else "updated",
    )
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    ap.add_argument("--limit", type=int, default=0, help="max rows (0 = all)")
    ap.add_argument(
        "--prefer-longer-title",
        action="store_true",
        help="keep the existing title when it already fits and is longer",
    )
    ap.add_argument(
        "--heuristic-only",
        action="store_true",
        help="skip the LLM; derive title/description deterministically",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
