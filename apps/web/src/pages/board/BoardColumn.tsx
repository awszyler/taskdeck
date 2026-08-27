"use client";
import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { cn } from "@/lib/utils";
import type { Task } from "@/api/client";
import {
  COLUMN_CONFIG,
  COLUMN_TO_STATUSES,
  sortNeedsYou,
  STATUS_CONFIG,
  type ColumnId,
  type TaskStatus,
} from "./status-config";
import { BoardCard } from "./BoardCard";

type Props = {
  columnId: ColumnId;
  tasks: Task[];
  onReviewClick?: (task: Task) => void;
  onCardClick?: (task: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
  // Optional `n / N` capacity badge for the Running column. Other
  // columns ignore this. Shown as `inflight / capacity`. When the
  // count would mismatch task list (which is workspace-scoped + WS-
  // delayed), we trust the runner-hub aggregate over the list.
  capacity?: { running: number; capacity: number; runners: number };
};

export function BoardColumn({ columnId, tasks, onReviewClick, onCardClick, onDuplicateAndEdit, capacity }: Props) {
  const cfg = COLUMN_CONFIG[columnId];
  const { setNodeRef, isOver } = useDroppable({ id: columnId });
  const Icon = cfg.Icon;

  const isNeedsYou = columnId === "needs_you";
  const sorted = isNeedsYou ? [...tasks].sort(sortNeedsYou) : tasks;

  // For needs_you, find the boundary between awaiting and review tiers.
  const awaitingCount = isNeedsYou
    ? sorted.filter((t) => t.status === "awaiting_input").length
    : 0;
  const reviewCount = isNeedsYou
    ? sorted.filter((t) => t.status === "in_review").length
    : 0;
  const showSeparator = isNeedsYou && awaitingCount > 0 && reviewCount > 0;

  return (
    <div className={cn(
      "flex flex-col rounded-xl p-2",
      // Responsive width:
      // - mobile (<sm): full width, stacks vertically (parent uses flex-col)
      // - sm/md (640-1279px): fixed 260px, parent allows horizontal scroll
      // - xl+ (≥1280px): flex-1 fills available width evenly across columns
      //   (no max cap — the 4-column board should always span the full
      //   width; capping left empty space on wide screens after the
      //   Draft column was removed).
      "w-full sm:w-[260px] sm:shrink-0 xl:w-auto xl:flex-1 xl:min-w-[240px]",
      cfg.columnBg,
    )}>
      <div className={cn("mb-2 flex flex-col gap-0.5 px-1.5 sticky top-0 z-10 rounded-lg py-1", cfg.columnBg)}>
        <div className="flex items-center gap-2">
          <Icon className={cn("h-3.5 w-3.5", cfg.iconColor)} />
          <span className="text-xs font-semibold">{cfg.label}</span>
          {/* Running column gets a runner-aggregate "n / N" badge so the
              user can tell at a glance whether the dispatcher is wedged.
              Other columns just show the task count. capacity.running may
              briefly disagree with tasks.length during WS gaps — the
              runner hub is the authority for "currently dispatched". */}
          {columnId === "running" && capacity ? (
            <span
              className={cn(
                "text-xs ml-auto font-mono",
                capacity.capacity === 0
                  ? "text-muted-foreground/60"
                  : capacity.running >= capacity.capacity
                    ? "text-warning"
                    : "text-muted-foreground",
              )}
              title={
                capacity.runners === 0
                  ? "No runners connected"
                  : `${capacity.running} dispatched / ${capacity.capacity} capacity across ${capacity.runners} runner${capacity.runners === 1 ? "" : "s"}`
              }
            >
              {capacity.running} / {capacity.capacity}
            </span>
          ) : (
            <span className={cn("text-xs ml-auto", tasks.length === 0 ? "text-muted-foreground/40" : "text-muted-foreground")}>
              {tasks.length}
            </span>
          )}
        </div>
        {isNeedsYou && awaitingCount > 0 && (
          <div className="text-[10px] text-amber-600 dark:text-amber-400 font-medium pl-[22px]">
            {awaitingCount} {awaitingCount === 1 ? "needs reply" : "need reply"}
          </div>
        )}
      </div>
      <div
        ref={setNodeRef}
        className={cn(
          "flex-1 min-h-[100px] rounded-lg transition-colors",
          isOver && "bg-accent/30",
        )}
      >
        <SortableContext items={sorted.map((t) => t.id)} strategy={verticalListSortingStrategy}>
          {sorted.length === 0 ? (
            <div className="flex items-center justify-center h-[80px] text-muted-foreground/40">
              <Icon className="h-5 w-5" />
            </div>
          ) : (
            sorted.map((t, idx) => {
              const prev = idx > 0 ? sorted[idx - 1] : undefined;
              const renderSeparatorBefore =
                showSeparator
                && t.status === "in_review"
                && (idx === 0 || prev?.status !== "in_review");
              return (
                <div key={t.id}>
                  {renderSeparatorBefore && (
                    <div className="my-3 flex items-center gap-2 px-1 text-[10px] text-muted-foreground">
                      <div className="h-px flex-1 bg-border" />
                      <span>{awaitingCount} awaiting · {reviewCount} review</span>
                      <div className="h-px flex-1 bg-border" />
                    </div>
                  )}
                  <BoardCard task={t} onReviewClick={onReviewClick} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
                </div>
              );
            })
          )}
        </SortableContext>
      </div>
    </div>
  );
}

// Helper for archive (still uses raw status, not column).
type ArchiveColumnProps = {
  status: TaskStatus;
  tasks: Task[];
  onCardClick?: (task: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
};

export function ArchiveColumn({ status, tasks, onCardClick, onDuplicateAndEdit }: ArchiveColumnProps) {
  const cfg = STATUS_CONFIG[status];
  const Icon = cfg.Icon;
  return (
    <div className="flex flex-col rounded-xl p-2 bg-muted/40 w-full sm:w-[260px] sm:shrink-0">
      <div className="mb-2 flex items-center gap-2 px-1.5 sticky top-0 z-10 rounded-lg py-1 bg-muted/40">
        <Icon className={cn("h-3.5 w-3.5", cfg.iconColor)} />
        <span className="text-xs font-semibold">{cfg.label}</span>
        <span className={cn("text-xs ml-auto", tasks.length === 0 ? "text-muted-foreground/40" : "text-muted-foreground")}>
          {tasks.length}
        </span>
      </div>
      <div className="flex-1 min-h-[100px] rounded-lg">
        <SortableContext items={tasks.map((t) => t.id)} strategy={verticalListSortingStrategy}>
          {tasks.length === 0 ? (
            <div className="flex items-center justify-center h-[80px] text-muted-foreground/40">
              <Icon className="h-5 w-5" />
            </div>
          ) : (
            tasks.map((t) => (
              <BoardCard key={t.id} task={t} onCardClick={onCardClick} onDuplicateAndEdit={onDuplicateAndEdit} />
            ))
          )}
        </SortableContext>
      </div>
    </div>
  );
}

// Re-export so BoardView's COLUMN_TO_STATUSES uses don't need a separate import.
export { COLUMN_TO_STATUSES };
