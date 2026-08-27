import type { LucideIcon } from "lucide-react";
import {
  Ban,
  CircleAlert,
  CircleCheck,
  CircleDot,
  CircleX,
  Inbox,
  Loader2,
  Sparkles,
} from "lucide-react";

export type TaskStatus =
  | "parsing"
  | "parse_failed"
  | "pending"
  | "blocked"
  | "running"
  | "awaiting_input"
  | "in_review"
  | "done"
  | "failed"
  | "cancelled";

// ─────────────────────────────────────────────────────────────────────────
// 4-column board model (parsing-UX rework — DRAFT removed)
//
// The board groups raw task statuses into 4 user-facing columns:
//   - todo    ← parsing, pending, blocked (queued; parsing = intent still
//               resolving; blocked = pending + unmet deps)
//   - running ← running (agent actively executing)
//   - needs_you ← parse_failed, awaiting_input, in_review (ball is in the
//               user's court — parse couldn't route, mid-task clarification,
//               or post-completion review)
//   - done    ← done (user has accepted the result)
//
// Failed and cancelled go to the archive drawer below the main board.
// ─────────────────────────────────────────────────────────────────────────

export type ColumnId = "todo" | "running" | "needs_you" | "done";

export const COLUMN_TO_STATUSES: Record<ColumnId, TaskStatus[]> = {
  todo: ["parsing", "pending", "blocked"],
  running: ["running"],
  needs_you: ["parse_failed", "awaiting_input", "in_review"],
  done: ["done"],
};

export const STATUS_TO_COLUMN: Record<TaskStatus, ColumnId | "archive"> = (() => {
  const map: Partial<Record<TaskStatus, ColumnId | "archive">> = {};
  for (const [col, statuses] of Object.entries(COLUMN_TO_STATUSES)) {
    for (const s of statuses) map[s] = col as ColumnId;
  }
  map.failed = "archive";
  map.cancelled = "archive";
  return map as Record<TaskStatus, ColumnId | "archive">;
})();

export const BOARD_COLUMNS: ColumnId[] = ["todo", "running", "needs_you", "done"];
export const ARCHIVE_STATUSES: TaskStatus[] = ["failed", "cancelled"];

export const COLUMN_CONFIG: Record<ColumnId, {
  label: string;
  iconColor: string;
  columnBg: string;
  Icon: LucideIcon;
}> = {
  todo:      { label: "Todo",      iconColor: "text-muted-foreground", columnBg: "bg-muted/40",      Icon: CircleDot },
  running:   { label: "Running",   iconColor: "text-warning",          columnBg: "bg-warning/5",     Icon: Loader2 },
  needs_you: { label: "Needs you", iconColor: "text-amber-500",        columnBg: "bg-amber-50/50 dark:bg-amber-950/20", Icon: Inbox },
  done:      { label: "Done",      iconColor: "text-info",             columnBg: "bg-info/5",        Icon: CircleCheck },
};

// Per-status visual config for cards + badges (NOT for column rendering).
export const STATUS_CONFIG: Record<TaskStatus, {
  label: string;
  iconColor: string;
  Icon: LucideIcon;
}> = {
  parsing:        { label: "Parsing",        iconColor: "text-info",             Icon: Sparkles },
  parse_failed:   { label: "Parse failed",   iconColor: "text-amber-500",        Icon: CircleAlert },
  pending:        { label: "Pending",        iconColor: "text-muted-foreground", Icon: CircleDot },
  blocked:        { label: "Blocked",        iconColor: "text-destructive",      Icon: CircleAlert },
  running:        { label: "Running",        iconColor: "text-warning",          Icon: Loader2 },
  awaiting_input: { label: "Awaiting input", iconColor: "text-amber-500",        Icon: CircleAlert },
  in_review:      { label: "Review",         iconColor: "text-info",             Icon: CircleCheck },
  done:           { label: "Done",           iconColor: "text-info",             Icon: CircleCheck },
  failed:         { label: "Failed",         iconColor: "text-destructive",      Icon: CircleX },
  cancelled:      { label: "Cancelled",      iconColor: "text-muted-foreground", Icon: Ban },
};

/** Sort comparator for the "Needs you" column.
 *  parse_failed (nothing has run yet — fastest to fix) appears first,
 *  then awaiting (mid-task block), then review (post-task review).
 *  Within each tier, newest first (using created_at since API doesn't
 *  yet expose updated_at). */
export function sortNeedsYou(a: { status: string; created_at?: string },
                              b: { status: string; created_at?: string }): number {
  const tier = (s: string) =>
    s === "parse_failed" ? 0 : s === "awaiting_input" ? 1 : s === "in_review" ? 2 : 3;
  const ta = tier(a.status);
  const tb = tier(b.status);
  if (ta !== tb) return ta - tb;
  return (b.created_at ?? "").localeCompare(a.created_at ?? "");
}
