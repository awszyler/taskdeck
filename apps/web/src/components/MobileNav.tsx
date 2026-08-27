import { LayoutGrid, Plus, Settings } from "lucide-react";
import { cn } from "@/lib/utils";

type View = "kanban" | "new-task" | "admin";

type Props = {
  view: View;
  onView: (v: View) => void;
};

export function MobileNav({ view, onView }: Props) {
  return (
    <nav
      className="sm:hidden fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60"
      style={{ paddingBottom: "env(safe-area-inset-bottom, 0px)" }}
    >
      <div className="grid grid-cols-3 h-14">
        <button
          className={cn(
            "flex flex-col items-center justify-center gap-0.5 text-xs",
            view === "kanban" ? "text-primary" : "text-muted-foreground",
          )}
          onClick={() => onView("kanban")}
        >
          <LayoutGrid className="h-5 w-5" />
          Board
        </button>
        <button
          className={cn(
            "flex flex-col items-center justify-center gap-0.5 text-xs",
            view === "new-task" ? "text-primary" : "text-muted-foreground",
          )}
          onClick={() => onView("new-task")}
        >
          <Plus className="h-5 w-5" />
          New
        </button>
        <button
          className={cn(
            "flex flex-col items-center justify-center gap-0.5 text-xs",
            view === "admin" ? "text-primary" : "text-muted-foreground",
          )}
          onClick={() => onView("admin")}
        >
          <Settings className="h-5 w-5" />
          Admin
        </button>
      </div>
    </nav>
  );
}
