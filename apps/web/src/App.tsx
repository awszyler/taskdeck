import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/sonner";
import { TopNav } from "@/components/TopNav";
import { MobileNav } from "@/components/MobileNav";
import { LoginPage } from "./pages/LoginPage";
import { SignupPage } from "./pages/SignupPage";
import { NewTaskPage } from "./pages/NewTaskPage";
import { TasksPage } from "./pages/TasksPage";
import { AdminPage } from "./pages/AdminPage";
// ViewerPage owns the /viewer/<id>/... full-page route. Loading it
// pulls react-markdown + react-syntax-highlighter (the heaviest deps
// in the tree). Kanban-first users never need it, so split it into
// its own chunk that downloads only when the route is hit.
const ViewerPage = lazy(() =>
  import("./pages/ViewerPage").then((m) => ({ default: m.ViewerPage })),
);
import { api } from "@/api/client";
import { useAuthConfig } from "@/hooks/useAuthConfig";

// Tiny fallback used while a lazy chunk is loading. Inline so it lives
// in the eager critical-path bundle and never itself shows a flash.
function LazyFallback() {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center">
      <span className="text-muted-foreground text-sm">Loading…</span>
    </div>
  );
}

type View = "kanban" | "new-task" | "admin";
type AuthView = "login" | "signup";

export function App() {
  const [view, setView] = useState<View>("kanban");
  const [authView, setAuthView] = useState<AuthView>("login");
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(
    () => localStorage.getItem("ccpt.activeWorkspaceId"),
  );
  const [newTaskPrefill, setNewTaskPrefill] = useState<string | undefined>(undefined);

  const qc = useQueryClient();
  const config = useAuthConfig();
  const meQuery = useQuery({
    queryKey: ["auth", "me"],
    queryFn: api.getMe,
    retry: false,
    enabled: config.data !== undefined,
  });

  useEffect(() => {
    if (activeWorkspaceId) localStorage.setItem("ccpt.activeWorkspaceId", activeWorkspaceId);
  }, [activeWorkspaceId]);

  // First Cognito user inherits any pre-Cognito workspaces. The endpoint
  // 400s if memberships already exist, so we swallow errors and re-fetch
  // workspaces on success.
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    if (config.data?.auth_mode !== "cognito") return;
    if (!meQuery.data) return;
    bootstrapped.current = true;
    api.bootstrapOwnership()
      .then((r) => {
        if (r.claimed > 0) qc.invalidateQueries({ queryKey: ["workspaces"] });
      })
      .catch(() => {});
  }, [config.data?.auth_mode, meQuery.data, qc]);

  const goNewTask = useCallback((prefill?: string) => {
    setNewTaskPrefill(prefill);
    setView("new-task");
  }, []);
  const goKanban = useCallback(() => {
    setNewTaskPrefill(undefined);
    setView("kanban");
  }, []);
  const goAdmin = useCallback(() => setView("admin"), []);

  const onLoginSuccess = useCallback(() => {
    qc.invalidateQueries({ queryKey: ["auth", "me"] });
  }, [qc]);

  // Loading state
  if (config.isLoading || meQuery.isLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <span className="text-muted-foreground text-sm">Loading…</span>
      </div>
    );
  }

  // No /me — show auth views (login or signup)
  if (meQuery.isError || !meQuery.data) {
    const allowSignup = !!config.data?.allow_signup;
    const poolName = config.data?.cognito_pool_name ?? "";
    return (
      <>
        <Toaster />
        {authView === "signup" && allowSignup ? (
          <SignupPage onBackToLogin={() => setAuthView("login")} />
        ) : (
          <LoginPage
            allowSignup={allowSignup}
            poolName={poolName}
            onSignupClick={() => setAuthView("signup")}
            onSuccess={onLoginSuccess}
          />
        )}
      </>
    );
  }

  const me = meQuery.data;

  // /viewer/<task_id>?path=...&kind=... — full-page output viewer.
  // Static URL routing (we don't have react-router); App reads the
  // pathname once on mount and decides whether to render the
  // viewer or the kanban.
  const viewerMatch = location.pathname.match(/^\/viewer\/([0-9a-f-]{8,})\/?$/);
  if (viewerMatch) {
    const taskId = viewerMatch[1]!;
    return (
      <>
        <Toaster />
        <Suspense fallback={<LazyFallback />}>
          <ViewerPage taskId={taskId} />
        </Suspense>
      </>
    );
  }

  return (
    <>
      <Toaster />
      {view === "admin" ? (
        <AdminPage
          activeWorkspaceId={activeWorkspaceId}
          onBack={goKanban}
          topSlot={<TopNav user={me} onAdmin={goAdmin} />}
        />
      ) : view === "new-task" ? (
        <NewTaskPage
          activeWorkspaceId={activeWorkspaceId}
          onDone={goKanban}
          onCancel={goKanban}
          topSlot={<TopNav user={me} onAdmin={goAdmin} />}
          initialRawInput={newTaskPrefill}
        />
      ) : (
        <TasksPage
          activeWorkspaceId={activeWorkspaceId}
          setActiveWorkspaceId={setActiveWorkspaceId}
          onNewTask={goNewTask}
          topSlot={<TopNav user={me} onAdmin={goAdmin} />}
        />
      )}
      <MobileNav
        view={view}
        onView={(v) => v === "kanban" ? goKanban() : v === "new-task" ? goNewTask() : goAdmin()}
      />
    </>
  );
}
