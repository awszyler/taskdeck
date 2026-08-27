"use client";
import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { Archive } from "lucide-react";
import { type Task } from "@/api/client";
import { cn } from "@/lib/utils";
import { BoardCard } from "./BoardCard";

type Props = {
  failedTasks: Task[];
  cancelledTasks: Task[];
  totalArchive: number;
  onCardClick?: (task: Task) => void;
  onDuplicateAndEdit?: (sourceText: string) => void;
};

/** Permanent archive strip at the bottom of the board.
 *
 *  Always visible so it doubles as a drag target: drop any card here
 *  to cancel/archive it. Old design (collapsed accordion) hid the
 *  drop zone behind a click, which made bulk-archive impossible.
 *
 *  failed + cancelled cards share the strip horizontally; they're
 *  small and read-only here. Click a card to open its drawer.
 */
export function ArchiveStrip({
  failedTasks,
  cancelledTasks,
  totalArchive,
  onCardClick,
  onDuplicateAndEdit,
}: Props) {
  const { setNodeRef, isOver } = useDroppable({ id: "archive" });
  const allArchived = [...failedTasks, ...cancelledTasks];
  const ids = allArchived.map((t) => t.id);

  return (
    <section
      ref={setNodeRef}
      className={cn(
        "mt-3 rounded-xl border border-border/60",
        "bg-muted/30 transition-colors",
        isOver && "bg-amber-500/10 border-amber-500/40 ring-2 ring-amber-500/30",
      )}
      aria-label="Archive — drag here to cancel"
    >
      <header className="flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground border-b border-border/40">
        <Archive className="h-3.5 w-3.5" />
        <span className="font-semibold uppercase tracking-wide">Archive</span>
        <span className="font-mono bg-muted px-1.5 py-0.5 rounded">
          {totalArchive}
        </span>
        <span className="ml-auto italic text-muted-foreground/70">
          drag a card here to cancel
        </span>
      </header>
      <div className="px-2 py-2">
        <SortableContext items={ids} strategy={verticalListSortingStrategy}>
          {allArchived.length === 0 ? (
            <div className="text-center text-xs text-muted-foreground/60 py-4">
              empty
            </div>
          ) : (
            <div className="flex gap-2 overflow-x-auto">
              {allArchived.map((t) => (
                <div key={t.id} className="w-[260px] shrink-0">
                  <BoardCard
                    task={t}
                    onCardClick={onCardClick}
                    onDuplicateAndEdit={onDuplicateAndEdit}
                  />
                </div>
              ))}
            </div>
          )}
        </SortableContext>
      </div>
    </section>
  );
}
