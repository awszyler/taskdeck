"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  ExternalLink,
  FileCode,
  FileText,
  Image as ImageIcon,
  Loader2,
  Package,
} from "lucide-react";
import { lazy, Suspense, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
// Viewers are split off the kanban critical path. Markdown + Code pull
// react-markdown / react-syntax-highlighter (the bulk of the old single
// chunk); the others are smaller but split for consistency. Loading
// only happens once the user expands an output in the drawer.
const ArchiveViewer = lazy(() =>
  import("./ArchiveViewer").then((m) => ({ default: m.ArchiveViewer })),
);
const CodeViewer = lazy(() =>
  import("./CodeViewer").then((m) => ({ default: m.CodeViewer })),
);
const DataViewer = lazy(() =>
  import("./DataViewer").then((m) => ({ default: m.DataViewer })),
);
const ImageViewer = lazy(() =>
  import("./ImageViewer").then((m) => ({ default: m.ImageViewer })),
);
const MarkdownViewer = lazy(() =>
  import("./MarkdownViewer").then((m) => ({ default: m.MarkdownViewer })),
);

function ViewerFallback() {
  return (
    <div className="flex items-center justify-center py-8 text-muted-foreground">
      <Loader2 className="h-5 w-5 animate-spin" />
    </div>
  );
}

type OutputKind = "interactive" | "document" | "code" | "data" | "image" | "archive";

type Output = {
  kind: OutputKind;
  entry: string;
  label: string;
  source: string;
  runtime: string | null;
  port: number | null;
};

type Props = {
  taskId: string;
};

const KIND_ICON: Record<OutputKind, typeof FileText> = {
  interactive: ExternalLink,
  document: FileText,
  code: FileCode,
  data: Database,
  image: ImageIcon,
  archive: Package,
};

/** Drawer section that shows the task's output manifest with
 *  per-output viewers. interactive kind is a button that launches
 *  the sandbox in a new tab; the others render inline. */
export function OutputsSection({ taskId }: Props) {
  const qc = useQueryClient();
  const manifestQ = useQuery({
    queryKey: ["task", taskId, "manifest"],
    queryFn: () => api.getSandboxManifest(taskId),
    staleTime: 30_000,
    retry: false,
  });

  const startMut = useMutation({
    mutationFn: async (idx: number) => {
      // Mobile Safari blocks window.open after an awaited fetch — the
      // user-activation token expires. Pre-open a placeholder tab on
      // the click gesture, navigate it once the API returns. `noopener`
      // would null out the handle, so we drop it and sever opener
      // ourselves after navigation.
      const placeholder = window.open("about:blank", "_blank");
      try {
        const data = await api.startSandbox(taskId, idx);
        if (placeholder) {
          placeholder.opener = null;
          placeholder.location.href = data.base_path;
        }
        return { data, placeholderOpened: !!placeholder };
      } catch (e) {
        if (placeholder) placeholder.close();
        throw e;
      }
    },
    onSuccess: ({ data, placeholderOpened }) => {
      if (!placeholderOpened) {
        // Popup blocked entirely — give the user a one-tap fallback.
        toast.success("Sandbox ready", {
          action: {
            label: "Open",
            onClick: () => window.open(data.base_path, "_blank", "noopener"),
          },
        });
      } else {
        toast.success("Sandbox ready");
      }
      qc.invalidateQueries({ queryKey: ["task", taskId, "sandbox-status"] });
    },
    onError: (err) => toast.error(`Start failed: ${String(err)}`),
  });

  const [activeIdx, setActiveIdx] = useState(0);

  if (manifestQ.isLoading) {
    return (
      <Section title="Outputs">
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading manifest…
        </div>
      </Section>
    );
  }

  if (manifestQ.isError) {
    // Most common: workspace pruned → 404. Surface gracefully.
    return (
      <Section title="Outputs">
        <div className="text-sm text-muted-foreground">
          No outputs available. The task workspace may have been pruned by the
          30-day LRU GC. Rerun the task to recreate it.
        </div>
      </Section>
    );
  }

  const outputs = (manifestQ.data?.outputs ?? []) as Output[];
  if (outputs.length === 0) {
    return (
      <Section title="Outputs">
        <div className="text-sm text-muted-foreground">
          The agent didn't produce anything to view.
        </div>
      </Section>
    );
  }

  const safeIdx = Math.min(activeIdx, outputs.length - 1);
  const active = outputs[safeIdx];

  return (
    <Section title="Outputs">
      {/* Tabs */}
      <div className="flex flex-wrap gap-1.5 mb-3">
        {outputs.map((o, i) => {
          const Icon = KIND_ICON[o.kind];
          const isActive = i === safeIdx;
          return (
            <button
              key={i}
              onClick={() => setActiveIdx(i)}
              className={cn(
                "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs",
                "border transition-colors",
                isActive
                  ? "bg-accent border-accent-foreground/20 text-foreground"
                  : "bg-background border-border text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="h-3 w-3" />
              {o.label || o.entry}
              <span className="text-[10px] text-muted-foreground/60">({o.kind})</span>
            </button>
          );
        })}
      </div>

      {/* Active viewer. Lazy chunks load on first switch into a kind;
          subsequent switches are cached. Wrap the whole panel once
          rather than per-kind so the spinner shows during whichever
          chunk is fetching. */}
      <div className="border rounded-lg p-3 bg-card">
        {active && active.kind === "interactive" && (
          <InteractiveActions
            taskId={taskId}
            output={active}
            outputIdx={safeIdx}
            onStart={() => startMut.mutate(safeIdx)}
            isStarting={startMut.isPending}
          />
        )}
        {active && active.kind !== "interactive" && (
          <Suspense fallback={<ViewerFallback />}>
            {active.kind === "document" && (
              <MarkdownViewer taskId={taskId} path={active.entry} />
            )}
            {active.kind === "code" && (
              <CodeViewer taskId={taskId} path={active.entry} />
            )}
            {active.kind === "image" && (
              <ImageViewer taskId={taskId} path={active.entry} />
            )}
            {active.kind === "data" && (
              <DataViewer taskId={taskId} path={active.entry} />
            )}
            {active.kind === "archive" && (
              <ArchiveViewer taskId={taskId} path={active.entry} />
            )}
          </Suspense>
        )}
      </div>
    </Section>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </div>
  );
}

function InteractiveActions({
  taskId,
  output,
  outputIdx,
  onStart,
  isStarting,
}: {
  taskId: string;
  output: Output;
  outputIdx: number;
  onStart: () => void;
  isStarting: boolean;
}) {
  // Suppress unused warnings; props kept for future per-output state.
  void taskId;
  void outputIdx;
  return (
    <div className="flex flex-col items-start gap-2 py-2">
      <div className="text-sm">
        <span className="font-medium">{output.label || output.entry}</span>
        <span className="ml-2 text-muted-foreground text-xs">
          ({output.runtime ?? "static"})
        </span>
      </div>
      <Button onClick={onStart} disabled={isStarting} size="sm">
        {isStarting ? (
          <>
            <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            Starting sandbox…
          </>
        ) : (
          <>
            <ExternalLink className="h-3.5 w-3.5 mr-1.5" />
            Open in new tab
          </>
        )}
      </Button>
    </div>
  );
}
