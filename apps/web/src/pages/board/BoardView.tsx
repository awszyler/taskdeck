"use client";
import { useState, useCallback } from "react";
import {
  DndContext,
  pointerWithin,
  closestCenter,
  type CollisionDetection,
  type DragEndEvent,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { toast } from "sonner";
import { api, type Task } from "@/api/client";
import { ArchiveStrip } from "./ArchiveStrip";
import { BoardColumn } from "./BoardColumn";
import { DragGhost } from "./BoardCard";
import { ReviewDraftDialog } from "./ReviewDraftDialog";
import { TaskDetailDrawer } from "./TaskDetailDrawer";
import {
  ARCHIVE_STATUSES,
  BOARD_COLUMNS,
  COLUMN_TO_STATUSES,
  STATUS_TO_COLUMN,
  type ColumnId,
  type TaskStatus,
} from "./status-config";

// Archive is a drop target but not a regular column; the dnd hooks
// treat it specially in handleDragEnd (drag-in = cancel).
const ARCHIVE_DROP_ID = "archive";
const COLUMN_IDS = new Set<string>([...BOARD_COLUMNS, ARCHIVE_DROP_ID]);

// Map a column-drop into the task status the user likely intended.
// Used when the user drags a card across columns. Illegal transitions
// fall through to the backend, which returns 409 → we toast and rollback.
const COLUMN_TARGET_STATUS: Record<ColumnId | typeof ARCHIVE_DROP_ID, TaskStatus> = {
  todo: "pending",
  running: "running",
  needs_you: "in_review",
  done: "done",
  archive: "cancelled",
};

const collision: CollisionDetection = (args) => {
  const pointer = pointerWithin(args);
  if (pointer.length > 0) {
    const cards = pointer.filter((c) => !COLUMN_IDS.has(c.id as string));
    if (cards.length > 0) return cards;
  }
  return closestCenter(args);
};

type Props = {
  tasks: Task[];
  onNewTask: (prefill?: string) => void;
};

export function BoardView({ tasks, onNewTask }: Props) {
  const qc = useQueryClient();
  // Map of task id → optimistic status override
  const [optimistic, setOptimistic] = useState<Map<string, string>>(() => new Map());
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [reviewTask, setReviewTask] = useState<Task | null>(null);
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
  );

  // Runner aggregate capacity for the Running column header. Polled —
  // not invalidated on WS task events, since "dispatcher inflight" is
  // a runner-hub concept that updates on dispatch/finish, not on the
  // task lifecycle events the WS hook surfaces. 5s is enough latency
  // for the "are we wedged?" indicator without burning the API.
  const capacityQ = useQuery({
    queryKey: ["runners", "capacity"],
    queryFn: api.getRunnerCapacity,
    refetchInterval: 5_000,
    staleTime: 5_000,
    retry: false,
  });

  const transitionMutation = useMutation({
    mutationFn: ({ id, to }: { id: string; to: string }) =>
      api.transitionTask(id, { to }),
    onSuccess: (_data, { id }) => {
      setOptimistic((prev) => {
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err, { id }) => {
      toast.error(`Transition failed: ${String(err)}`);
      setOptimistic((prev) => {
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  // Merge optimistic overrides into display tasks
  const displayTasks: Task[] = tasks.map((t) => {
    const override = optimistic.get(t.id);
    return override ? { ...t, status: override } : t;
  });

  const taskById = useCallback(
    (id: string) => displayTasks.find((t) => t.id === id),
    [displayTasks],
  );

  // Resolve a drop target id into the column it belongs to.
  // Returns "archive" when the drop is on the archive strip or on a
  // card already inside it. handleDragEnd then maps that to a
  // cancelled status.
  type DropTarget = ColumnId | typeof ARCHIVE_DROP_ID;
  const getColumnForId = useCallback(
    (id: string): DropTarget | null => {
      if (COLUMN_IDS.has(id)) return id as DropTarget;
      const task = taskById(id);
      if (!task) return null;
      const col = STATUS_TO_COLUMN[task.status as TaskStatus];
      return col;  // "archive" is a valid target now
    },
    [taskById],
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      setActiveTask(null);
      const { active, over } = event;
      if (!over) return;

      const fromTask = taskById(active.id as string);
      if (!fromTask) return;

      const toColumn = getColumnForId(over.id as string);
      if (!toColumn) return;

      const fromColumn = STATUS_TO_COLUMN[fromTask.status as TaskStatus];
      if (fromColumn === toColumn) return; // same column = no-op

      const targetStatus = COLUMN_TARGET_STATUS[toColumn];

      // Optimistic update
      setOptimistic((prev) => new Map(prev).set(fromTask.id, targetStatus));
      transitionMutation.mutate({ id: fromTask.id, to: targetStatus });
    },
    [taskById, getColumnForId, transitionMutation],
  );

  const handleDragStart = useCallback(
    (event: { active: { id: string | number } }) => {
      const task = taskById(event.active.id as string);
      setActiveTask(task ?? null);
    },
    [taskById],
  );

  const onCardClick = useCallback((task: Task) => {
    setOpenTaskId(task.id);
  }, []);

  const tasksForColumn = (col: ColumnId): Task[] => {
    const statuses = new Set<string>(COLUMN_TO_STATUSES[col]);
    return displayTasks.filter((t) => statuses.has(t.status));
  };

  const archiveTasks = (status: TaskStatus) =>
    displayTasks.filter((t) => t.status === status);

  const totalArchive = ARCHIVE_STATUSES.reduce(
    (sum, s) => sum + archiveTasks(s).length,
    0,
  );

  if (tasks.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center text-muted-foreground">
        <Inbox className="h-12 w-12 mb-3 opacity-40" />
        <p className="text-sm font-medium mb-1">No tasks yet</p>
        <p className="text-xs">Click + New task to get started.</p>
      </div>
    );
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collision}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      <div className="flex flex-col sm:flex-row gap-3 overflow-x-auto xl:overflow-x-visible pb-4">
        {BOARD_COLUMNS.map((col) => (
          <BoardColumn
            key={col}
            columnId={col}
            tasks={tasksForColumn(col)}
            onReviewClick={setReviewTask}
            onCardClick={onCardClick}
            onDuplicateAndEdit={onNewTask}
            capacity={col === "running" ? capacityQ.data : undefined}
          />
        ))}
      </div>

      {/* Archive: permanent strip across the bottom of the board.
          Always visible so it's a live drop target — drag any card
          here to cancel/archive it. failed and cancelled tasks live
          here horizontally, hidden behind a single scroll. */}
      <ArchiveStrip
        failedTasks={archiveTasks("failed")}
        cancelledTasks={archiveTasks("cancelled")}
        totalArchive={totalArchive}
        onCardClick={onCardClick}
        onDuplicateAndEdit={onNewTask}
      />

      <DragOverlay>
        {activeTask ? <DragGhost task={activeTask} /> : null}
      </DragOverlay>

      <ReviewDraftDialog
        task={reviewTask}
        onClose={() => setReviewTask(null)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["tasks"] })}
      />

      <TaskDetailDrawer
        taskId={openTaskId}
        onClose={() => setOpenTaskId(null)}
        onDuplicateAndEdit={onNewTask}
      />
    </DndContext>
  );
}
