"use client";
import { memo, useState, useEffect, useRef } from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  Check,
  Link2,
  Loader2,
  Paperclip,
  Send,
  X,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { api, type Task, type TaskTurnsResponse } from "@/api/client";
import { BoardCardActionsMenu } from "./BoardCardActionsMenu";

function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

function isParseFailed(task: Task): boolean {
  return task.status === "parse_failed";
}

function isFailedReview(task: Task): boolean {
  return task.status === "in_review" && task.exit_code !== null && task.exit_code !== 0;
}

/** Skeleton body for parsing-state tasks. */
function ParsingSkeletonBody({ task }: { task: Task }) {
  const echo = task.raw_input ?? task.prompt ?? task.title;
  return (
    <>
      <div className="text-sm text-muted-foreground/90 line-clamp-3 leading-snug">
        {echo}
      </div>
      <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin shrink-0" />
        <span>Parsing intent…</span>
      </div>
    </>
  );
}

/** Standard card body — shared across most variants. */
export function CardBody({ task }: { task: Task }) {
  if (task.status === "parsing") return <ParsingSkeletonBody task={task} />;

  const createdAt = task.created_at;
  const needsReview = isParseFailed(task);
  return (
    <>
      <div className="text-sm font-medium leading-snug flex items-start gap-1.5">
        {needsReview && (
          <AlertTriangle className="h-3.5 w-3.5 text-warning shrink-0 mt-0.5" aria-label="Parse failed" />
        )}
        {task.dependencies_count > 0 && (
          <Link2 className="h-3.5 w-3.5 text-muted-foreground shrink-0 mt-0.5" />
        )}
        {(task.attachments_count ?? 0) > 0 && (
          <Paperclip
            className="h-3.5 w-3.5 text-muted-foreground shrink-0 mt-0.5"
            aria-label={`${task.attachments_count} attached file${task.attachments_count === 1 ? "" : "s"}`}
          />
        )}
        <span className="line-clamp-2" title={task.description ?? undefined}>
          {task.title}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-1.5 flex-wrap">
        {task.agent && (
          <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
            {task.agent}
          </Badge>
        )}
        {task.repo && (
          <span className="text-[10px] font-mono text-muted-foreground truncate max-w-[160px]">
            {task.repo.replace(/^https?:\/\/(www\.)?github\.com\//, "")}
          </span>
        )}
        {task.exit_code !== null && task.exit_code !== 0 && (
          <Badge variant="destructive" className="text-[10px] px-1.5 py-0">
            exit {task.exit_code}
          </Badge>
        )}
      </div>
      {needsReview && (
        <div className="mt-1.5 text-[10px] text-warning/90 font-medium">
          Low parser confidence — click to review
        </div>
      )}
      {createdAt && (
        <div className="mt-1.5 text-[10px] text-muted-foreground">
          {timeAgo(createdAt)}
        </div>
      )}
    </>
  );
}

/** Awaiting-input variant: shows latest agent question + inline reply box. */
function AwaitingCard({ task, onCardClick, onDuplicateAndEdit }: {
  task: Task;
  onCardClick?: (t: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
}) {
  const qc = useQueryClient();
  const [reply, setReply] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  const turnsQ = useQuery<TaskTurnsResponse>({
    queryKey: ["task", task.id, "turns"],
    queryFn: () => api.listTaskTurns(task.id),
  });

  const latestAgentQuestion = (() => {
    const items = turnsQ.data?.items ?? [];
    for (let i = items.length - 1; i >= 0; i--) {
      const turn = items[i];
      if (turn && turn.role === "agent") return turn.content;
    }
    return null;
  })();

  const respondMut = useMutation({
    mutationFn: (content: string) => api.respondTask(task.id, content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["task", task.id, "turns"] });
      setReply("");
      toast.success("Sent — agent resumed");
    },
    onError: (err) => toast.error(`Send failed: ${String(err)}`),
  });

  // Flash on first mount (newly transitioned to awaiting).
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.classList.add("animate-flash-once");
    const t = window.setTimeout(() => el.classList.remove("animate-flash-once"), 700);
    return () => window.clearTimeout(t);
  }, []);

  const handleSend = () => {
    const content = reply.trim();
    if (!content) return;
    respondMut.mutate(content);
  };

  return (
    <div ref={ref} className="relative mb-2 animate-pulse-amber rounded-lg">
      <Card
        className={cn(
          "relative p-2.5 select-none",
          "bg-amber-50 dark:bg-amber-950/30",
          "border-amber-200 dark:border-amber-900",
          "border-l-4 border-l-amber-500",
        )}
      >
        {onDuplicateAndEdit && (
          <div className="absolute top-1 right-1 z-10" onPointerDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
            <BoardCardActionsMenu task={task} onDuplicateAndEdit={onDuplicateAndEdit} />
          </div>
        )}
        <div
          className="cursor-pointer pr-7"
          onClick={(e) => {
            // Don't open drawer when clicking inside the reply form.
            const t = e.target as HTMLElement;
            if (t.closest(".inline-reply")) return;
            if (onCardClick) onCardClick(task);
          }}
        >
          <Badge className="bg-amber-500 text-white hover:bg-amber-500 mb-1.5 text-[10px] px-1.5 py-0 gap-1">
            <AlertTriangle className="h-2.5 w-2.5" />
            Awaiting input · {task.created_at ? timeAgo(task.created_at) : ""}
          </Badge>
          <div className="text-sm font-medium line-clamp-2 leading-snug mb-1">
            {task.title}
          </div>
          {latestAgentQuestion && (
            <div className="text-xs text-amber-900 dark:text-amber-200 line-clamp-3 italic mb-2">
              "{latestAgentQuestion}"
            </div>
          )}
        </div>
        <div className="inline-reply mt-2">
          <textarea
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Reply…"
            disabled={respondMut.isPending}
            className={cn(
              "w-full text-xs px-2 py-1.5 rounded border resize-none",
              "bg-white dark:bg-neutral-900",
              "border-amber-200 dark:border-amber-900",
              "focus:outline-none focus:ring-1 focus:ring-amber-400",
              "disabled:opacity-50",
            )}
            rows={2}
          />
          <div className="mt-1 flex items-center justify-between">
            <span className="text-[10px] text-muted-foreground">⌘↵ to send</span>
            <Button
              size="sm"
              onClick={handleSend}
              disabled={!reply.trim() || respondMut.isPending}
              className="h-6 text-xs px-2 bg-amber-500 hover:bg-amber-600 text-white"
            >
              {respondMut.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <>
                  <Send className="h-3 w-3 mr-1" />
                  Send
                </>
              )}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

/** In-review variant: success or failure. Approve is the inline primary
 *  action (when applicable); Rerun / Duplicate&edit / Cancel live in the
 *  ⋯ menu so the card stays compact. */
function ReviewCard({ task, onCardClick, onDuplicateAndEdit }: {
  task: Task;
  onCardClick?: (t: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
}) {
  const qc = useQueryClient();
  const failed = isFailedReview(task);

  const onMutSuccess = () => {
    qc.invalidateQueries({ queryKey: ["tasks"] });
  };

  const approveMut = useMutation({
    mutationFn: () => api.approveTask(task.id),
    onSuccess: () => { onMutSuccess(); toast.success("Approved"); },
    onError: (err) => toast.error(`Approve failed: ${String(err)}`),
  });

  const busy = approveMut.isPending;

  return (
    <Card
      className={cn(
        "relative p-2.5 select-none mb-2 cursor-pointer transition-shadow hover:shadow-md",
        failed
          ? "bg-amber-50/50 dark:bg-amber-950/20 border-l-4 border-l-red-500"
          : "border-l-4 border-l-blue-500",
      )}
      onClick={(e) => {
        const t = e.target as HTMLElement;
        if (t.closest(".review-actions") || t.closest("[role=menu]")) return;
        if (onCardClick) onCardClick(task);
      }}
    >
      {onDuplicateAndEdit && (
        <div className="absolute top-1 right-1 z-10" onPointerDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
          <BoardCardActionsMenu task={task} onDuplicateAndEdit={onDuplicateAndEdit} />
        </div>
      )}
      <div className="pr-7">
      <Badge
        className={cn(
          "mb-1.5 text-[10px] px-1.5 py-0 gap-1",
          failed
            ? "bg-red-500 text-white hover:bg-red-500"
            : "bg-blue-500 text-white hover:bg-blue-500",
        )}
      >
        {failed ? <X className="h-2.5 w-2.5" /> : <Check className="h-2.5 w-2.5" />}
        Review {failed ? "(failed)" : ""} · {task.created_at ? timeAgo(task.created_at) : ""}
      </Badge>
      <div className="text-sm font-medium line-clamp-2 leading-snug mb-1">
        {task.title}
      </div>
      {task.summary && (
        <div className="text-xs text-muted-foreground line-clamp-3 mb-2">
          {task.summary}
        </div>
      )}
      {!failed && (
        <div className="review-actions flex items-center gap-1 mt-2">
          <Button
            size="sm"
            onClick={() => approveMut.mutate()}
            disabled={busy}
            className="h-6 text-xs px-2 bg-blue-500 hover:bg-blue-600 text-white"
          >
            <Check className="h-3 w-3 mr-1" /> Approve
          </Button>
        </div>
      )}
      </div>
    </Card>
  );
}

/** Running variant: spinner badge + bottom shimmer. */
function RunningCard({ task, onCardClick, onDuplicateAndEdit }: {
  task: Task;
  onCardClick?: (t: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
}) {
  return (
    <Card
      className={cn(
        "relative p-2.5 select-none mb-2 cursor-pointer transition-shadow hover:shadow-md",
        "border-l-4 border-l-blue-500 overflow-hidden",
      )}
      onClick={(e) => {
        const t = e.target as HTMLElement;
        if (t.closest("[role=menu]")) return;
        onCardClick?.(task);
      }}
    >
      {onDuplicateAndEdit && (
        <div className="absolute top-1 right-1 z-10" onPointerDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
          <BoardCardActionsMenu task={task} onDuplicateAndEdit={onDuplicateAndEdit} />
        </div>
      )}
      <div className="pr-7">
        <Badge className="bg-blue-500/10 text-blue-700 dark:text-blue-400 hover:bg-blue-500/10 mb-1.5 text-[10px] px-1.5 py-0 gap-1">
          <Loader2 className="h-2.5 w-2.5 animate-spin" />
          Running · {task.created_at ? timeAgo(task.created_at) : ""}
        </Badge>
        <CardBody task={task} />
      </div>
      <div
        className={cn(
          "absolute bottom-0 left-0 right-0 h-px",
          "bg-gradient-to-r from-transparent via-blue-400/60 to-transparent",
          "animate-shimmer-slow bg-[length:200%_100%]",
        )}
      />
    </Card>
  );
}

/** Done variant: standard look + ⋯ menu (Rerun / Duplicate & edit). */
function DoneCard({ task, onCardClick, onDuplicateAndEdit }: {
  task: Task;
  onCardClick?: (t: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
}) {
  return (
    <Card
      className="relative p-2.5 select-none mb-2 cursor-pointer hover:bg-accent/50 transition-colors"
      onClick={(e) => {
        const t = e.target as HTMLElement;
        if (t.closest("[role=menu]")) return;
        onCardClick?.(task);
      }}
    >
      {onDuplicateAndEdit && (
        <div className="absolute top-1 right-1 z-10" onPointerDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
          <BoardCardActionsMenu task={task} onDuplicateAndEdit={onDuplicateAndEdit} />
        </div>
      )}
      <div className="pr-7">
        <CardBody task={task} />
      </div>
    </Card>
  );
}

type BoardCardProps = {
  task: Task;
  onReviewClick?: (task: Task) => void;
  onCardClick?: (task: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
};

/** Sortable board card. Variant is chosen per task status. */
export const BoardCard = memo(function BoardCard({ task, onReviewClick, onCardClick, onDuplicateAndEdit }: BoardCardProps) {
  const isParsing = task.status === "parsing";
  const needsReview = isParseFailed(task);
  const isAwaiting = task.status === "awaiting_input";
  const isInReview = task.status === "in_review";
  const isRunning = task.status === "running";
  const isDone = task.status === "done";

  const sortable = useSortable({
    id: task.id,
    // Disable drag for cards with rich interactive bodies or that the
    // user can't meaningfully move — accidental drag would lose input
    // or feel janky.
    disabled: isParsing || isAwaiting || isInReview || needsReview,
  });
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = sortable;

  const style = {
    transform: CSS.Translate.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };

  // Variant: parsing
  if (isParsing) {
    return (
      <Card
        ref={setNodeRef}
        style={style}
        className="p-2.5 select-none mb-2 border-info/30 bg-info/5 animate-pulse"
      >
        <CardBody task={task} />
      </Card>
    );
  }

  // Variant: awaiting_input (interactive — no drag)
  if (isAwaiting) {
    return (
      <div ref={setNodeRef} style={style}>
        <AwaitingCard task={task} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
      </div>
    );
  }

  // Variant: in_review (interactive — no drag)
  if (isInReview) {
    return (
      <div ref={setNodeRef} style={style}>
        <ReviewCard task={task} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
      </div>
    );
  }

  // Variant: running (draggable to other columns, but discouraged)
  if (isRunning) {
    return (
      <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
        <RunningCard task={task} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
      </div>
    );
  }

  // Variant: done (⋯ menu replaces hover Rerun)
  if (isDone) {
    return (
      <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
        <DoneCard task={task} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
      </div>
    );
  }

  // Default: parse_failed / pending / blocked / failed / cancelled
  return (
    <Card
      ref={setNodeRef}
      style={style}
      className={cn(
        "relative p-2.5 cursor-grab active:cursor-grabbing select-none mb-2 hover:bg-accent/50 transition-colors",
        needsReview && "border-warning/40",
      )}
      onClick={(e) => {
        if (isDragging) return;
        const t = e.target as HTMLElement;
        if (t.closest("[role=menu]") || t.closest(".card-actions")) return;
        if (needsReview && onReviewClick) {
          e.preventDefault();
          onReviewClick(task);
          return;
        }
        if (onCardClick) {
          e.preventDefault();
          onCardClick(task);
        }
      }}
      {...attributes}
      {...listeners}
    >
      {onDuplicateAndEdit && (
        <div className="card-actions absolute top-1 right-1 z-10" onClick={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
          <BoardCardActionsMenu task={task} onDuplicateAndEdit={onDuplicateAndEdit} />
        </div>
      )}
      <div className="pr-7">
        <CardBody task={task} />
      </div>
    </Card>
  );
});

/** Non-interactive ghost card for the DragOverlay. */
export function DragGhost({ task }: { task: Task }) {
  return (
    <Card className="p-2.5 select-none shadow-xl ring-1 ring-primary/20">
      <CardBody task={task} />
    </Card>
  );
}
