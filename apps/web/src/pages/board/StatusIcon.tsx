import { cn } from "@/lib/utils";
import { STATUS_CONFIG, type TaskStatus } from "./status-config";

export function StatusIcon({ status, className }: { status: TaskStatus; className?: string }) {
  const cfg = STATUS_CONFIG[status];
  const Icon = cfg.Icon;
  return <Icon className={cn(cfg.iconColor, className, status === "running" && "animate-spin")} />;
}
