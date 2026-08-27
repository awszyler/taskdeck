"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2, Paperclip, Pencil, RotateCcw, Send, X } from "lucide-react";
import { toast } from "sonner";
import { api, type Task, type TaskLogEntry, type TaskLogsResponse, type TaskTurn } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { OutputsSection } from "./output/OutputsSection";

type Props = {
  taskId: string | null;
  onClose: () => void;
  /** Reopen the New-task page seeded with the given text. Used by the
   *  parse_failed "Duplicate & edit" action. */
  onDuplicateAndEdit?: (sourceText: string) => void;
};

const POLL_MS = 2500;
const LOG_LIMIT = 2000;
const LOGS_VISIBLE_STATUSES = new Set(["running", "done", "failed", "cancelled"]);
// Outputs panel is meaningful only after the agent has had a chance
// to write artifacts. We mirror sandbox /start's allowlist to keep
// the UI consistent with what the backend will accept.
const OUTPUTS_VISIBLE_STATUSES = new Set([
  "in_review", "done", "failed", "cancelled",
]);

export function TaskDetailDrawer({ taskId, onClose, onDuplicateAndEdit }: Props) {
  const open = taskId !== null;
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const engagedRef = useRef(false);
  const [dragX, setDragX] = useState(0);

  const onTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0];
    if (!t) return;
    startRef.current = { x: t.clientX, y: t.clientY };
    engagedRef.current = false;
    setDragX(0);
  };

  const onTouchMove = (e: React.TouchEvent) => {
    if (!startRef.current) return;
    const t = e.touches[0];
    if (!t) return;
    const dx = t.clientX - startRef.current.x;
    const dy = t.clientY - startRef.current.y;
    if (!engagedRef.current) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      // Engage swipe only when the gesture is dominantly horizontal
      // (~33° cone). Otherwise let the inner scroll container own the
      // vertical movement — hijacking it would break log/output scroll.
      if (Math.abs(dx) > Math.abs(dy) * 1.5) {
        engagedRef.current = true;
      } else {
        startRef.current = null;
        return;
      }
    }
    // Drawer enters from the right; only rightward drag dismisses.
    setDragX(Math.max(0, dx));
  };

  const onTouchEnd = () => {
    if (engagedRef.current && dragX > 80) {
      onClose();
    }
    setDragX(0);
    engagedRef.current = false;
    startRef.current = null;
  };

  return (
    <Sheet open={open} onOpenChange={(v) => !v && onClose()}>
      <SheetContent
        className="sm:max-w-2xl w-full overflow-y-auto"
        style={dragX > 0 ? {
          transform: `translateX(${dragX}px)`,
          transition: "none",
        } : undefined}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {taskId && (
          <DrawerBody
            key={taskId}
            taskId={taskId}
            onClose={onClose}
            onDuplicateAndEdit={onDuplicateAndEdit}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function DrawerBody({ taskId, onClose, onDuplicateAndEdit }: {
  taskId: string;
  onClose: () => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
}) {
  const qc = useQueryClient();

  const taskQ = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.getTask(taskId),
    refetchInterval: (q) =>
      q.state.data?.status === "running" ? POLL_MS : false,
  });
  const task = taskQ.data;

  const logsEnabled = task ? LOGS_VISIBLE_STATUSES.has(task.status) : false;
  const logsQ = useQuery({
    queryKey: ["task", taskId, "logs"],
    queryFn: () => api.listTaskLogs(taskId, { limit: LOG_LIMIT }),
    enabled: logsEnabled,
    refetchInterval: () => (task?.status === "running" ? POLL_MS : false),
  });

  const turnsQ = useQuery({
    queryKey: ["task", taskId, "turns"],
    queryFn: () => api.listTaskTurns(taskId),
    enabled: !!task,  // fetch once the task is known; cheap point-read
  });

  const respondMut = useMutation({
    mutationFn: (content: string) => api.respondTask(taskId, content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      qc.invalidateQueries({ queryKey: ["task", taskId, "turns"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("Sent — agent resumed");
    },
    onError: (err) => toast.error(`Send failed: ${String(err)}`),
  });

  const cancelMut = useMutation({
    mutationFn: () => api.cancelTask(taskId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["task", taskId] }),
    onError: (err) => toast.error(`Cancel failed: ${String(err)}`),
  });
  // Single "re-run this task" path. Resets the task in place (clean
  // slate: prior logs and turns dropped, exit_code/summary cleared,
  // status → pending). The earlier /retry endpoint created a new task
  // with retry_of=src.id but no consumer ever read that link, so the
  // two flows were collapsed into /rerun. Drawer stays open so the
  // user sees the task transition back through running/done.
  const rerunMut = useMutation({
    mutationFn: () => api.rerunTask(taskId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task", taskId] });
      toast.success("Re-running");
    },
    onError: (err) => toast.error(`Rerun failed: ${String(err)}`),
  });

  if (taskQ.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }
  if (taskQ.isError || !task) {
    return (
      <div className="py-8 text-sm text-destructive">
        Task not found.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SheetHeader className="space-y-2">
        <SheetTitle className="text-base font-semibold leading-snug pr-8">
          {task.title}
        </SheetTitle>
        {task.description ? (
          <SheetDescription className="text-xs text-muted-foreground leading-snug">
            {task.description}
          </SheetDescription>
        ) : (
          <SheetDescription className="sr-only">
            Task details, summary, and output logs
          </SheetDescription>
        )}
        <div className="flex items-center gap-1.5 flex-wrap">
          {task.agent && (
            <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
              {task.agent}
            </Badge>
          )}
          {task.repo && (
            <span className="text-[10px] font-mono text-muted-foreground">
              {task.repo}
            </span>
          )}
          <Badge variant="outline" className="text-[10px] px-1.5 py-0">
            {task.status}
          </Badge>
          {task.exit_code !== null && task.exit_code !== 0 && (
            <Badge variant="destructive" className="text-[10px] px-1.5 py-0">
              exit {task.exit_code}
            </Badge>
          )}
        </div>
      </SheetHeader>

      {turnsQ.data && turnsQ.data.items.length > 0 && (
        <ConversationSection
          turns={turnsQ.data.items}
          isAwaiting={task.status === "awaiting_input"}
          onSend={(content) => respondMut.mutate(content)}
          sending={respondMut.isPending}
        />
      )}

      <AttachmentsSection taskId={task.id} />

      <SummarySection task={task} />

      {/* P6.4: per-output viewers — markdown / code / image / data /
          interactive (sandbox launcher). Hidden for tasks that
          haven't reached a viewable state. */}
      {OUTPUTS_VISIBLE_STATUSES.has(task.status) && (
        <OutputsSection taskId={task.id} />
      )}

      {logsEnabled && (
        <LogsSection
          data={logsQ.data}
          isLoading={logsQ.isLoading}
          isError={logsQ.isError}
        />
      )}

      <ActionsSection
        task={task}
        onCancel={() => cancelMut.mutate()}
        onRetry={() => rerunMut.mutate()}
        cancelling={cancelMut.isPending}
        retrying={rerunMut.isPending}
        onDuplicateAndEdit={
          onDuplicateAndEdit
            ? () => {
                onDuplicateAndEdit(task.raw_input || task.prompt || task.title);
                onClose();
              }
            : undefined
        }
      />
    </div>
  );
}

function AttachmentsSection({ taskId }: { taskId: string }) {
  // P7: files the user uploaded with the task. Empty section is hidden
  // entirely so tasks created the old way don't get a stray label.
  const q = useQuery({
    queryKey: ["task", taskId, "attachments"],
    queryFn: () => api.getTaskAttachments(taskId),
    staleTime: 60_000,
    retry: false,
  });
  const items = q.data?.items ?? [];
  if (q.isLoading || items.length === 0) return null;
  return (
    <section className="space-y-1.5">
      <p className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
        Attached files
      </p>
      <ul className="space-y-1">
        {items.map((a) => (
          <li
            key={a.id}
            className="flex items-center gap-2 text-xs bg-muted/40 rounded px-2 py-1.5"
          >
            <Paperclip className="h-3 w-3 shrink-0 text-muted-foreground" />
            <span className="truncate flex-1">{a.original_filename}</span>
            <span className="text-muted-foreground shrink-0">
              {a.size_bytes < 1024
                ? `${a.size_bytes} B`
                : a.size_bytes < 1024 * 1024
                  ? `${(a.size_bytes / 1024).toFixed(0)} KB`
                  : `${(a.size_bytes / 1024 / 1024).toFixed(1)} MB`}
            </span>
            <a
              href={api.attachmentDownloadUrl(a.id)}
              download={a.original_filename}
              className="text-muted-foreground hover:text-foreground"
              title={`Download ${a.original_filename}`}
            >
              <Download className="h-3.5 w-3.5" />
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}


function SummarySection({ task }: { task: Task }) {
  let body: React.ReactNode;
  switch (task.status) {
    case "parsing":
      body = (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Parsing intent…
        </div>
      );
      break;
    case "parse_failed":
      body = (
        <div className="space-y-1.5">
          <p className="text-sm text-amber-600 dark:text-amber-400">
            Couldn't parse this into a task.
          </p>
          {task.summary && (
            <pre className="whitespace-pre-wrap text-xs text-muted-foreground">
              {task.summary}
            </pre>
          )}
          <pre className="whitespace-pre-wrap text-sm text-muted-foreground">
            {task.raw_input || task.prompt || "(no input)"}
          </pre>
          <p className="text-xs text-muted-foreground">
            Use “Duplicate &amp; edit” below to tweak the wording and resubmit.
          </p>
        </div>
      );
      break;
    case "pending":
      body = <p className="text-sm text-muted-foreground">Waiting for runner…</p>;
      break;
    case "blocked":
      body = (
        <p className="text-sm text-muted-foreground">
          Blocked by {task.dependencies_count} parent
          {task.dependencies_count === 1 ? "" : "s"}.
        </p>
      );
      break;
    case "running":
      body = (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Running…
        </div>
      );
      break;
    case "done":
    case "failed":
      body = task.summary ? (
        <pre className="whitespace-pre-wrap text-sm">{task.summary}</pre>
      ) : (
        <p className="text-sm text-muted-foreground">No summary yet.</p>
      );
      break;
    case "cancelled":
      body = <p className="text-sm text-muted-foreground">Cancelled.</p>;
      break;
    default:
      body = null;
  }
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
        Summary
      </h3>
      {body}
    </section>
  );
}

function LogsSection({
  data,
  isLoading,
  isError,
}: {
  data: TaskLogsResponse | undefined;
  isLoading: boolean;
  isError: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);

  useEffect(() => {
    if (pinnedToBottom && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [data, pinnedToBottom]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 16;
    setPinnedToBottom(atBottom);
  };

  const text = useMemo(() => {
    if (!data) return "";
    return data.items.map((l: TaskLogEntry) => l.data).join("");
  }, [data]);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success("Output copied to clipboard");
    } catch {
      toast.error("Copy failed");
    }
  };

  if (isLoading) {
    return (
      <section className="space-y-2">
        <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Output
        </h3>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading logs…
        </div>
      </section>
    );
  }
  if (isError || !data) {
    return (
      <section className="space-y-2">
        <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Output
        </h3>
        <p className="text-sm text-destructive">Failed to load logs.</p>
      </section>
    );
  }

  const { items, total, returned, truncated } = data;
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Output{truncated ? ` (last ${returned} lines)` : ""}
        </h3>
        <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={onCopy}>
          Copy
        </Button>
      </div>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="bg-muted/30 border border-border/40 rounded-md p-3 text-xs font-mono leading-snug max-h-[60vh] overflow-y-auto"
      >
        {items.length === 0 ? (
          <p className="text-muted-foreground">No output yet.</p>
        ) : (
          items.map((entry) => (
            <div
              key={entry.seq}
              className={cn(
                "whitespace-pre-wrap break-words",
                entry.stream === "stderr" && "text-destructive",
              )}
            >
              {entry.data}
            </div>
          ))
        )}
      </div>

      {truncated && (
        <p className="text-xs text-muted-foreground">
          Showing last {returned} of {total} lines.
        </p>
      )}
    </section>
  );
}

function ConversationSection({
  turns,
  isAwaiting,
  onSend,
  sending,
}: {
  turns: TaskTurn[];
  isAwaiting: boolean;
  onSend: (content: string) => void;
  sending: boolean;
}) {
  const [draft, setDraft] = useState("");

  function trySend() {
    const trimmed = draft.trim();
    if (!trimmed || sending) return;
    onSend(trimmed);
    setDraft("");
  }

  return (
    <section className="space-y-2">
      <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
        Conversation
      </h3>
      <div className="space-y-2">
        {turns.map((t) => (
          <div
            key={t.seq}
            className={cn(
              "rounded-md p-2.5 text-sm whitespace-pre-wrap break-words leading-snug",
              t.role === "agent"
                ? "bg-warning/10 border border-warning/30 text-foreground"
                : "bg-muted/40 border border-border/40 text-foreground ml-8",
            )}
          >
            <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-1">
              {t.role === "agent" ? "Agent" : "You"}
            </div>
            {t.content}
          </div>
        ))}
      </div>

      {isAwaiting && (
        <div className="space-y-2 pt-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                trySend();
              }
            }}
            placeholder="Reply to the agent…"
            rows={3}
            className="w-full rounded-md border border-border/60 bg-background p-2 text-sm font-mono leading-snug focus:outline-none focus:ring-2 focus:ring-warning/30"
            disabled={sending}
          />
          <div className="flex items-center justify-between">
            <span className="text-[10px] text-muted-foreground">
              ⌘/Ctrl + Enter to send
            </span>
            <Button
              size="sm"
              variant="default"
              onClick={trySend}
              disabled={!draft.trim() || sending}
            >
              <Send className="h-4 w-4 mr-1.5" />
              {sending ? "Sending…" : "Send"}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function ActionsSection({
  task,
  onCancel,
  onRetry,
  cancelling,
  retrying,
  onDuplicateAndEdit,
}: {
  task: Task;
  onCancel: () => void;
  onRetry: () => void;
  cancelling: boolean;
  retrying: boolean;
  onDuplicateAndEdit?: () => void;
}) {
  if (task.status === "running" || task.status === "awaiting_input") {
    return (
      <section className="pt-2 border-t border-border/40">
        <Button variant="destructive" size="sm" onClick={onCancel} disabled={cancelling}>
          <X className="h-4 w-4 mr-1.5" />
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      </section>
    );
  }
  if (task.status === "parse_failed") {
    return (
      <section className="pt-2 border-t border-border/40 flex items-center gap-2">
        {onDuplicateAndEdit && (
          <Button variant="default" size="sm" onClick={onDuplicateAndEdit}>
            <Pencil className="h-4 w-4 mr-1.5" />
            Duplicate &amp; edit
          </Button>
        )}
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={cancelling}>
          <X className="h-4 w-4 mr-1.5" />
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      </section>
    );
  }
  if (task.status === "failed" || task.status === "cancelled") {
    return (
      <section className="pt-2 border-t border-border/40">
        <Button variant="default" size="sm" onClick={onRetry} disabled={retrying}>
          <RotateCcw className="h-4 w-4 mr-1.5" />
          {retrying ? "Retrying…" : "Retry"}
        </Button>
      </section>
    );
  }
  return null;
}
