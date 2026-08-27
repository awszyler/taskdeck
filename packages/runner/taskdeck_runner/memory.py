from __future__ import annotations


def render_memory(chunks) -> str:
    """Render a list of MemoryChunk Pydantic objects as an XML-ish <memory> block.

    Returns an empty string when *chunks* is empty or None.
    Shell agent callers should not call this — guard at the call site.
    """
    if not chunks:
        return ""
    lines = ["<memory>"]
    for c in chunks:
        task_ref = c.source_task_id or "—"
        lines.append(
            f'<chunk source="{c.source_kind}" task="{task_ref}" score="{c.score:.3f}">'
        )
        lines.append(c.text.rstrip("\n"))
        lines.append("</chunk>")
    lines.append("</memory>\n")
    return "\n".join(lines)
