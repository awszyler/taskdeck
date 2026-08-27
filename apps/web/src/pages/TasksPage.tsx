import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type Task } from "@/api/client";
import { Button } from "@/components/ui/button";
import { WorkspacePicker } from "@/components/WorkspacePicker";
import { useEventStream } from "../api/ws";
import { BoardView } from "./board/BoardView";
import { BoardSkeleton } from "./board/BoardSkeleton";

type TasksPageProps = {
  activeWorkspaceId: string | null;
  setActiveWorkspaceId: (id: string) => void;
  onNewTask: (prefill?: string) => void;
  topSlot?: ReactNode;
};

export function TasksPage({ activeWorkspaceId, setActiveWorkspaceId, onNewTask, topSlot }: TasksPageProps) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["tasks"], queryFn: api.listTasks });

  const onEvent = useCallback(
    (ev: unknown) => {
      const e = ev as { type?: string; task_id?: string };
      // Invalidate on: real lifecycle events, plus the WS hook's
      // synthetic reconnect / polling tick signals (both indicate
      // we may have missed updates while disconnected).
      const listTriggers = new Set([
        "task.event",
        "task.parsed",
        "task.parse_started",
        "ws.reconnected",
        "ws.polling.tick",
      ]);
      if (e?.type && listTriggers.has(e.type)) {
        qc.invalidateQueries({ queryKey: ["tasks"] });
      }
      // For lifecycle events targeting a specific task, also
      // invalidate THAT task's per-task query so an open drawer
      // refreshes. Critically NOT a wildcard ["task"] invalidate —
      // that matches every per-task query (manifest/tree/logs/
      // turns) on every visible card, causing a fetch storm.
      if (
        e?.task_id
        && (e.type === "task.event" || e.type === "task.parsed")
      ) {
        qc.invalidateQueries({ queryKey: ["task", e.task_id] });
      }
    },
    [qc],
  );
  useEventStream(onEvent);

  // ── Search ──────────────────────────────────────────────
  // Lives at this level so the input keeps its value across
  // re-renders driven by react-query. Cmd/Ctrl+K focuses it.
  const [searchInput, setSearchInput] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
      }
      if (e.key === "Escape" && document.activeElement === searchRef.current) {
        setSearchInput("");
        searchRef.current?.blur();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const allItems: Task[] = q.data?.items ?? [];
  const wsScoped = activeWorkspaceId
    ? allItems.filter((t) => t.workspace_id === activeWorkspaceId)
    : allItems;
  const items = useMemo(() => {
    const q = searchInput.trim().toLowerCase();
    if (!q) return wsScoped;
    // Match across the user-visible fields. Substring is enough at
    // the current task volumes; can swap for fuse.js if it grows.
    return wsScoped.filter((t) => {
      const hay = [
        t.title, t.prompt, t.agent, t.status, t.repo ?? "",
        t.summary ?? "", t.raw_input ?? "",
      ].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [wsScoped, searchInput]);

  return (
    <div className="min-h-screen bg-background">
      {/* Top nav */}
      <header className="border-b border-border/60 bg-card/50 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 h-12 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <span className="font-semibold text-sm tracking-tight text-foreground shrink-0">
              Taskdeck
            </span>
            <span className="text-border/60 shrink-0">|</span>
            <WorkspacePicker activeId={activeWorkspaceId} onChange={setActiveWorkspaceId} />
          </div>

          {/* Search — center column, expands to fill on wide screens */}
          <div className="hidden sm:flex flex-1 max-w-md mx-3 relative">
            <Search className="h-3.5 w-3.5 text-muted-foreground absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              ref={searchRef}
              type="text"
              placeholder="Search tasks…  ⌘K"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="w-full h-8 pl-8 pr-7 text-sm bg-muted/40 border border-border/40 rounded-md focus:outline-none focus:ring-1 focus:ring-ring focus:border-ring placeholder:text-muted-foreground/60"
            />
            {searchInput && (
              <button
                onClick={() => setSearchInput("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                aria-label="Clear search"
                type="button"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          {/* "New task" only on sm+ screens; on phones it would push
              the WorkspacePicker off-screen so we rely on MobileNav's
              bottom-bar plus button instead (see MobileNav.tsx). */}
          <div className="flex items-center gap-2 shrink-0">
            <Button
              size="sm"
              className="hidden sm:inline-flex h-8 text-sm gap-1.5"
              onClick={() => onNewTask()}
            >
              <Plus className="h-4 w-4" />
              New task
            </Button>
            {topSlot}
          </div>
        </div>
      </header>

      <main className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-6 pb-16 sm:pb-6">
        {q.isLoading && <BoardSkeleton />}
        {q.isError && (
          <p className="text-destructive text-sm">Error: {String(q.error)}</p>
        )}
        {!q.isLoading && !q.isError && (
          <>
            {searchInput && (
              <div className="mb-3 text-xs text-muted-foreground flex items-center gap-2">
                <Search className="h-3 w-3" />
                <span>
                  {items.length} match{items.length === 1 ? "" : "es"} for{" "}
                  <span className="font-mono bg-muted px-1.5 py-0.5 rounded text-foreground">
                    {searchInput}
                  </span>
                </span>
                <button
                  onClick={() => setSearchInput("")}
                  className="text-muted-foreground/70 hover:text-foreground underline"
                  type="button"
                >
                  clear
                </button>
              </div>
            )}
            <BoardView tasks={items} onNewTask={onNewTask} />
          </>
        )}
      </main>
    </div>
  );
}
