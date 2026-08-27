import type { Task } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { TaskCard } from "./TaskCard";

type Props = {
  title: string;
  status: string;
  tasks: Task[];
  onSubmit?: (id: string) => void;
  onCancel?: (id: string) => void;
};

const columnHeaderColor: Record<string, string> = {
  draft: "text-zinc-400",
  pending: "text-amber-400",
  blocked: "text-orange-400",
  running: "text-blue-400",
  done: "text-emerald-400",
};

export function KanbanColumn({ title, status, tasks, onSubmit, onCancel }: Props) {
  const headerColor = columnHeaderColor[status] ?? "text-zinc-400";

  return (
    <div className="flex flex-col min-w-0 flex-1 min-w-[220px]">
      <div className="flex items-center gap-2 mb-3 px-1">
        <h2 className={`text-xs font-semibold uppercase tracking-wider ${headerColor}`}>
          {title}
        </h2>
        <Badge
          variant="secondary"
          className="h-4 text-xs px-1.5 font-mono bg-muted text-muted-foreground"
        >
          {tasks.length}
        </Badge>
      </div>

      <div className="flex flex-col gap-2 overflow-y-auto max-h-[calc(100vh-180px)] pr-0.5">
        {tasks.length === 0 && (
          <div className="text-xs text-muted-foreground/40 text-center py-8">
            empty
          </div>
        )}
        {tasks.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            onSubmit={onSubmit}
            onCancel={onCancel}
          />
        ))}
      </div>
    </div>
  );
}
