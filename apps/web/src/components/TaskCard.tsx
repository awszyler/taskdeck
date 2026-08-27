import { Link2, Terminal } from "lucide-react";
import type { Task } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Props = {
  task: Task;
  onSubmit?: (id: string) => void;
  onCancel?: (id: string) => void;
};

const agentColor: Record<string, string> = {
  "claude-code": "bg-violet-900/60 text-violet-200 border-violet-700/50",
  shell: "bg-zinc-800 text-zinc-300 border-zinc-700",
};

export function TaskCard({ task, onSubmit, onCancel }: Props) {
  const agentCls = agentColor[task.agent] ?? "bg-zinc-800 text-zinc-300 border-zinc-700";

  return (
    <Card className="bg-card border-border/60 hover:border-border transition-colors">
      <CardContent className="p-3 space-y-2">
        <div className="flex items-start justify-between gap-2">
          <p className="text-sm font-semibold text-foreground leading-tight line-clamp-2 flex-1">
            {task.dependencies_count > 0 && (
              <Link2
                className="inline-block h-3.5 w-3.5 mr-1 text-muted-foreground align-middle"
                aria-label={`Depends on ${task.dependencies_count} task(s)`}
              />
            )}
            {task.title}
          </p>
          {task.exit_code !== null && task.exit_code !== 0 && (
            <Badge variant="destructive" className="shrink-0 text-xs font-mono">
              exit {task.exit_code}
            </Badge>
          )}
        </div>

        <div className="flex items-center gap-1.5 flex-wrap">
          <span
            className={cn(
              "inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border font-mono",
              agentCls,
            )}
          >
            <Terminal className="h-3 w-3" />
            {task.agent}
          </span>

          {task.repo && (
            <span className="text-xs text-muted-foreground font-mono max-w-[120px] truncate">
              {task.repo.replace(/^.*\//, "").replace(/\.git$/, "")}
            </span>
          )}
        </div>

        {(task.status === "draft" || task.status === "pending" || task.status === "running") && (
          <div className="pt-0.5">
            {task.status === "draft" && onSubmit && (
              <Button
                size="sm"
                className="h-6 text-xs px-2"
                onClick={() => onSubmit(task.id)}
              >
                Submit
              </Button>
            )}
            {(task.status === "pending" || task.status === "running") && onCancel && (
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-xs px-2 text-destructive hover:text-destructive border-destructive/40 hover:bg-destructive/10"
                onClick={() => onCancel(task.id)}
              >
                Cancel
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
