from __future__ import annotations


def render_dependency_outputs(outputs) -> str:
    """Render a list of DependencyOutput Pydantic objects as XML-ish wrapper.

    Returns an empty string when outputs is empty or None.
    Shell agent callers should not call this — guard at the call site.

    Three sections per parent:
      <workspace path="../<id>/"/>      — where to find parent's files,
                                          relative to child's cwd
      <outputs>                          — entries from parent's output.yml
        <output kind=... entry=... label=.../>
      <artifact ...>                     — git-diff / branch / decision
                                          (existing legacy contract)
    """
    if not outputs:
        return ""
    lines = ["<dependency-outputs>"]
    for p in outputs:
        lines.append(
            f'<parent id="{p.parent_task_id}" title={_q(p.parent_title)} '
            f'status="{p.parent_status}">'
        )
        # Path hint comes first so a careful agent reading top-down
        # knows where to look before seeing the structured outputs.
        ws_path = getattr(p, "parent_workspace_path", "") or ""
        if ws_path:
            lines.append(f'<workspace path={_q(ws_path)} relative-from-cwd="true"/>')
        # Structured manifest entries — what files the parent produced.
        parent_outputs = getattr(p, "parent_outputs", None) or []
        if parent_outputs:
            lines.append("<outputs>")
            for o in parent_outputs:
                attrs = f'kind="{o.kind}" entry={_q(o.entry)}'
                if o.label:
                    attrs += f" label={_q(o.label)}"
                lines.append(f"<output {attrs}/>")
            lines.append("</outputs>")
        for a in p.artifacts:
            attrs = f'kind="{a.kind}"'
            for k, v in a.meta.items():
                attrs += f" {k}={_q(v)}"
            if a.truncated:
                attrs += ' truncated="true"'
            lines.append(f"<artifact {attrs}>")
            lines.append(a.content.rstrip("\n"))
            lines.append("</artifact>")
        lines.append("</parent>")
    lines.append("</dependency-outputs>\n")
    return "\n".join(lines)


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
