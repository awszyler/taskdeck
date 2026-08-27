"use client";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Copy,
  Database,
  ExternalLink,
  FileCode,
  FileText,
  Image,
  MoreHorizontal,
  Package,
  Pencil,
  RefreshCw,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { api, type Task } from "@/api/client";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

type OutputKind = "interactive" | "document" | "code" | "data" | "image" | "archive";

/** Verb appropriate for the kind. Single-output cards get a flat
 *  item like "Open counter.html" / "Download slides.pptx" rather
 *  than the generic "Open". */
function labelForOutput(o: { kind: OutputKind; label: string; entry: string }) {
  const name = o.label || o.entry;
  if (o.kind === "archive") return `Download ${name}`;
  return `Open ${name}`;
}

const KIND_ICON: Record<OutputKind, typeof FileText> = {
  interactive: ExternalLink,
  document: FileText,
  code: FileCode,
  data: Database,
  image: Image,
  archive: Package,
};

type Props = {
  task: Task;
  onDuplicateAndEdit: (sourceText: string) => void;
  className?: string;
};

/** Card-level actions menu. Sits in the top-right of every BoardCard
 *  variant. Visible at rest at 50% opacity; full opacity + soft bg on
 *  hover. Status-aware: only surfaces actions the backend will accept. */
export function BoardCardActionsMenu({ task, onDuplicateAndEdit, className }: Props) {
  const qc = useQueryClient();

  const rerunMut = useMutation({
    mutationFn: () => api.rerunTask(task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("Re-running");
    },
    onError: (err) => toast.error(`Rerun failed: ${String(err)}`),
  });

  const cancelMut = useMutation({
    mutationFn: () => api.cancelTask(task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("Cancelled");
    },
    onError: (err) => toast.error(`Cancel failed: ${String(err)}`),
  });

  // P6.4 manifest: only fetch when the user actually opens the menu.
  // Without this, every done card on the board fetches its manifest
  // on render — 10 done cards = 10 requests, multiplied every time
  // the page invalidates ["task"] (which it does on every WS poll
  // tick). Lazy fetch + 5min staleTime keeps it light.
  const [menuOpen, setMenuOpen] = useState(false);
  const status = task.status;
  const canSandboxBase = status === "in_review" || status === "done"
    || status === "failed" || status === "cancelled";
  const manifestQ = useQuery({
    queryKey: ["task", task.id, "manifest"],
    queryFn: () => api.getSandboxManifest(task.id),
    enabled: canSandboxBase && menuOpen,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const isArchived = !!task.archived_at;
  const startMut = useMutation({
    mutationFn: async (outputIdx: number) => {
      // Archived tasks: surface a longer-running pending toast so the
      // user knows the wait can hit ~30s while we pull the workspace
      // back from S3 and re-provision the container.
      if (isArchived) {
        toast.loading("Restoring sandbox from archive — this may take 30s…", {
          id: `sandbox-restore-${task.id}`,
        });
      }
      // Pre-open the tab synchronously so iOS/mobile browsers don't
      // block it after the awaited API call (popup-blocker eats any
      // window.open issued past a microtask boundary).
      const placeholder = window.open("about:blank", "_blank");
      try {
        const data = await api.startSandbox(task.id, outputIdx);
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
      if (isArchived) {
        toast.dismiss(`sandbox-restore-${task.id}`);
      }
      if (!placeholderOpened) {
        toast.success("Sandbox ready", {
          action: {
            label: "Open",
            onClick: () => window.open(data.base_path, "_blank", "noopener"),
          },
        });
      } else {
        toast.success("Sandbox ready");
      }
    },
    onError: (err) => {
      if (isArchived) {
        toast.dismiss(`sandbox-restore-${task.id}`);
      }
      toast.error(`Sandbox start failed: ${String(err)}`);
    },
  });

  const outputs = manifestQ.data?.outputs ?? [];
  const interactiveOutputs = outputs.filter((o) => o.kind === "interactive");
  const nonInteractiveOutputs = outputs.filter((o) => o.kind !== "interactive");

  /** Per-kind click action.
   *  - interactive: existing sandbox start path
   *  - archive: trigger download (binary, nothing to render)
   *  - everything else: open dedicated viewer in a new tab so it
   *    has full width (drawer is too narrow for code / md / csv)
   */
  type Output = (typeof outputs)[number];
  const openOutput = (o: Output, interactiveIdx: number) => {
    if (o.kind === "interactive") {
      startMut.mutate(interactiveIdx);
      return;
    }
    if (o.kind === "archive") {
      // Force-download via a transient <a download>. The server now
      // sends Content-Disposition with the real filename so the saved
      // file has the correct name and extension.
      const a = document.createElement("a");
      a.href = api.sandboxFileUrl(task.id, o.entry);
      a.download = o.entry.split("/").pop() || "download";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      return;
    }
    // document / code / data / image → full-page viewer in new tab.
    const url = `/viewer/${task.id}?path=${encodeURIComponent(o.entry)}&kind=${o.kind}`;
    window.open(url, "_blank", "noopener");
  };

  // Prefer prompt (richer) for the duplicate seed; fall back to title
  // for tasks where the parser hasn't filled in a prompt yet.
  const duplicateSource = (task.prompt && task.prompt.trim()) || task.title;

  const canRerun = canSandboxBase;
  const canDuplicate = duplicateSource.length > 0 && status !== "parsing";
  const canCancel = status === "parse_failed" || status === "pending"
    || status === "blocked" || status === "running"
    || status === "awaiting_input" || status === "in_review";
  const canSandbox = canSandboxBase && interactiveOutputs.length > 0;
  const hasAnyOpenable = interactiveOutputs.length + nonInteractiveOutputs.length > 0;

  if (!canRerun && !canDuplicate && !canCancel && !canSandbox && !hasAnyOpenable) return null;

  return (
    <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          // Ghost button: muted-foreground gets darker on hover, with a
          // subtle accent bg appearing. Uses inline style for opacity so
          // it survives any theme variable issues that bit us before.
          style={{
            width: 24,
            height: 24,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            borderRadius: 4,
            cursor: "pointer",
            opacity: 0.5,
            transition: "opacity 120ms, background-color 120ms",
          }}
          className={cn("hover:bg-black/5 dark:hover:bg-white/10", className)}
          onMouseEnter={(e) => { e.currentTarget.style.opacity = "1"; }}
          onMouseLeave={(e) => { e.currentTarget.style.opacity = "0.5"; }}
          onClick={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          aria-label="Task actions"
        >
          <MoreHorizontal size={16} strokeWidth={2} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        {/* P6.4: Open menu — varies by manifest contents */}
        {canSandboxBase && manifestQ.isLoading && (
          <DropdownMenuItem disabled>
            <ExternalLink className="h-3.5 w-3.5 mr-2" />
            Loading outputs…
          </DropdownMenuItem>
        )}
        {canSandboxBase && manifestQ.isError && (
          <DropdownMenuItem disabled className="text-muted-foreground">
            <ExternalLink className="h-3.5 w-3.5 mr-2" />
            (no output to view)
          </DropdownMenuItem>
        )}
        {/* Single output → flat menu item with kind-specific action.
         *  Multiple outputs → Open ▸ submenu (one entry per output).
         *  Each item dispatches via openOutput(): interactive starts
         *  the sandbox, archive triggers download, others open a new
         *  full-page tab. */}
        {canSandboxBase && manifestQ.isSuccess && outputs.length === 1 && (() => {
          const o = outputs[0]!;
          const Icon = KIND_ICON[o.kind];
          const interactiveIdx = o.kind === "interactive" ? 0 : -1;
          return (
            <DropdownMenuItem
              onClick={() => openOutput(o, interactiveIdx)}
              disabled={startMut.isPending && o.kind === "interactive"}
            >
              <Icon className="h-3.5 w-3.5 mr-2" />
              {o.kind === "interactive" && startMut.isPending
                ? "Starting…"
                : labelForOutput(o)}
            </DropdownMenuItem>
          );
        })()}
        {canSandboxBase && manifestQ.isSuccess && outputs.length > 1 && (
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <ExternalLink className="h-3.5 w-3.5 mr-2" />
              Open
            </DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              {interactiveOutputs.map((o) => {
                const interactiveIdx = interactiveOutputs.indexOf(o);
                const Icon = KIND_ICON[o.kind];
                return (
                  <DropdownMenuItem
                    key={`int-${o.entry}`}
                    onClick={() => openOutput(o, interactiveIdx)}
                    disabled={startMut.isPending}
                  >
                    <Icon className="h-3.5 w-3.5 mr-2" />
                    {o.label || o.entry}
                  </DropdownMenuItem>
                );
              })}
              {nonInteractiveOutputs.length > 0 && interactiveOutputs.length > 0 && (
                <DropdownMenuSeparator />
              )}
              {nonInteractiveOutputs.map((o) => {
                const Icon = KIND_ICON[o.kind];
                return (
                  <DropdownMenuItem
                    key={`other-${o.entry}`}
                    onClick={() => openOutput(o, -1)}
                  >
                    <Icon className="h-3.5 w-3.5 mr-2" />
                    {o.label || o.entry}
                    <span className="ml-2 text-xs text-muted-foreground">
                      ({o.kind})
                    </span>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        )}
        {(canSandboxBase && manifestQ.isSuccess && (interactiveOutputs.length + nonInteractiveOutputs.length) > 0) && (canRerun || canDuplicate || canCancel) && <DropdownMenuSeparator />}
        {canRerun && (
          <DropdownMenuItem
            onClick={() => rerunMut.mutate()}
            disabled={rerunMut.isPending}
          >
            <RefreshCw className="h-3.5 w-3.5 mr-2" />
            Rerun
          </DropdownMenuItem>
        )}
        {canDuplicate && (
          <DropdownMenuItem onClick={() => onDuplicateAndEdit(duplicateSource)}>
            {status === "done" ? (
              <>
                <Copy className="h-3.5 w-3.5 mr-2" />
                Duplicate &amp; edit
              </>
            ) : (
              <>
                <Pencil className="h-3.5 w-3.5 mr-2" />
                Edit as new
              </>
            )}
          </DropdownMenuItem>
        )}
        {canCancel && (
          <>
            {(canRerun || canDuplicate) && <DropdownMenuSeparator />}
            <DropdownMenuItem
              onClick={() => cancelMut.mutate()}
              disabled={cancelMut.isPending}
              className="text-destructive focus:text-destructive"
            >
              <X className="h-3.5 w-3.5 mr-2" />
              Cancel
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
