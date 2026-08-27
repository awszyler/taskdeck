"use client";
/**
 * Full-page viewer for non-interactive task outputs.
 *
 * URL shape: /viewer/<task_id>?path=<entry>&kind=<kind>
 *
 * Reached when the user clicks an output in the kanban ⋯ menu and
 * the kind isn't `interactive` or `archive`. The drawer is too
 * narrow for code / csv / markdown at full fidelity, so we open a
 * dedicated tab with the existing viewer components rendered at
 * full width.
 *
 * Auth: piggy-backs on the user's session cookie (Caddy / SPA
 * routing means this is the same origin as the kanban). No login
 * gate needed at this layer; the underlying API calls return 401
 * if the cookie's stale.
 */
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CodeViewer } from "./board/output/CodeViewer";
import { DataViewer } from "./board/output/DataViewer";
import { ImageViewer } from "./board/output/ImageViewer";
import { MarkdownViewer } from "./board/output/MarkdownViewer";

type Props = {
  taskId: string;
};

const VIEWER_KINDS = ["document", "code", "data", "image"] as const;
type ViewerKind = (typeof VIEWER_KINDS)[number];

function parseQuery(): { path: string; kind: ViewerKind } | null {
  const params = new URLSearchParams(location.search);
  const path = params.get("path");
  const kindRaw = params.get("kind");
  if (!path || !kindRaw) return null;
  if (!(VIEWER_KINDS as readonly string[]).includes(kindRaw)) return null;
  return { path, kind: kindRaw as ViewerKind };
}

export function ViewerPage({ taskId }: Props) {
  const q = parseQuery();

  if (!q) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground">
        <div className="space-y-2 text-center">
          <p>Missing or invalid viewer parameters.</p>
          <p className="text-xs">Expected: ?path=…&kind=…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border/60 bg-card/50 sticky top-0 z-10">
        <div className="max-w-screen-xl mx-auto px-4 sm:px-6 h-12 flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            className="h-8 gap-1.5 text-muted-foreground"
            onClick={() => window.close()}
            title="Close tab"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Close
          </Button>
          <span className="font-semibold text-sm tracking-tight text-foreground">
            Output
          </span>
          <span className="text-border/60">|</span>
          <span className="text-xs text-muted-foreground font-mono truncate">
            {q.path}
          </span>
          <span className="ml-auto text-[10px] uppercase text-muted-foreground tracking-wide">
            {q.kind}
          </span>
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-4 sm:px-6 py-6">
        {q.kind === "document" && <MarkdownViewer taskId={taskId} path={q.path} />}
        {q.kind === "code" && <CodeViewer taskId={taskId} path={q.path} />}
        {q.kind === "data" && <DataViewer taskId={taskId} path={q.path} />}
        {q.kind === "image" && <ImageViewer taskId={taskId} path={q.path} />}
      </main>
    </div>
  );
}
