export type ParsedIntent = {
  title: string;
  agent: string;
  repo: string | null;
  base_branch: string | null;
  priority: "low" | "normal" | "high";
  prompt: string;
  confidence: number;
  confidence_reasons: string[];
};

export type IntentContext = {
  recent_repos?: string[];
  known_agents?: string[];
  default_base_branch?: string;
  user_timezone?: string;
};

export type Workspace = {
  id: string;
  slug: string;
  name: string;
  created_at: string;
};

export type Task = {
  id: string;
  workspace_id: string;
  title: string;
  // One-or-two-sentence elaboration. Null for tasks created before the
  // description field existed (until the backfill runs).
  description?: string | null;
  prompt: string;
  origin: string;
  agent: string;
  repo: string | null;
  status: string;
  assigned_runner_id: string | null;
  exit_code: number | null;
  summary: string | null;
  created_at: string;
  dependencies_count: number;
  // Present on tasks that started in raw_input async mode. Survives even after
  // the parse loop hydrates agent/prompt — UI shows the original utterance on
  // skeleton/draft cards.
  raw_input?: string | null;
  intent_confidence?: number | null;
  // P6.3.7: non-null when the workspace was tar.gz'd to S3 by GC.
  // The Open Sandbox flow auto-restores; UI shows a banner.
  archived_at?: string | null;
  // P7 multimodal — count of files the user attached at submit time.
  attachments_count?: number;
};

export type Me = {
  id: string;
  login: string | null;
  name: string | null;
  avatar_url: string | null;
};

export type Member = {
  user_id: string;
  role: string;
  login: string | null;
  avatar_url: string | null;
};

export type CostSummary = {
  total_usd: string;
  by_operation: Record<string, string>;
  by_user: Record<string, string>;
  by_day: Array<{ date: string; usd: string }>;
};

export type CostEvent = {
  id: string;
  workspace_id: string | null;
  task_id: string | null;
  user_id: string | null;
  provider: string;
  operation: string;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: string | null;
  created_at: string;
};

export type AuditEvent = {
  id: string;
  workspace_id: string | null;
  user_id: string | null;
  kind: string;
  target_type: string | null;
  target_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
};

export type LoginResult =
  | { status: "ok" }
  | { status: "totp_required"; flow_id: string }
  | {
      status: "mfa_setup";
      flow_id: string;
      otpauth_uri: string;
      secret: string;
    }
  | {
      status: "new_password_required";
      flow_id: string;
      required_attributes?: string;
    };

export type MemoryChunk = {
  id: string;
  workspace_id: string;
  source_kind: string;
  source_task_id: string | null;
  source_artifact_id: string | null;
  text: string;
  meta: Record<string, unknown>;
  created_at: string;
};

export type TaskLogEntry = {
  seq: number;
  stream: "stdout" | "stderr";
  data: string;
  created_at: string;
};

export type TaskLogsResponse = {
  items: TaskLogEntry[];
  total: number;
  returned: number;
  truncated: boolean;
};

export type TaskTurn = {
  seq: number;
  role: "agent" | "user";
  content: string;
  created_at: string;
};

export type TaskTurnsResponse = {
  items: TaskTurn[];
};

// On 401 from any authenticated path, redirect to /login. Skipping the
// redirect for /auth/* avoids self-loops when the login endpoints
// themselves return 401 on bad credentials — those callers deal with
// the error UI inline.
function shouldBounceTo401Login(path: string): boolean {
  return !path.startsWith("/api/v1/auth/");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    ...init,
    credentials: "include", // session cookie carries the auth
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (r.status === 401 && shouldBounceTo401Login(path)) {
    if (!window.location.pathname.startsWith("/login")) {
      window.location.assign("/login");
    }
    throw new Error("session expired");
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  if (r.status === 204) return undefined as unknown as T;
  return (await r.json()) as T;
}

export const api = {
  listTasks: () => request<{ items: Task[] }>("/api/v1/tasks"),
  getTask: (id: string) => request<Task>(`/api/v1/tasks/${id}`),
  submitTask: (id: string) =>
    request<Task>(`/api/v1/tasks/${id}/submit`, { method: "POST" }),
  cancelTask: (id: string) =>
    request<Task>(`/api/v1/tasks/${id}/cancel`, { method: "POST" }),
  listTaskLogs: (
    id: string,
    opts?: { stream?: "stdout" | "stderr" | "all"; limit?: number },
  ) => {
    const qs = new URLSearchParams();
    if (opts?.stream) qs.set("stream", opts.stream);
    if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<TaskLogsResponse>(`/api/v1/tasks/${id}/logs${suffix}`);
  },
  approveTask: (id: string) =>
    request<Task>(`/api/v1/tasks/${id}/approve`, { method: "POST" }),
  // Re-run an existing task in place (clean slate: drops prior logs/turns,
  // resets exit_code/summary, transitions back to pending). Replaces the
  // earlier /retry endpoint which created a *new* task — same user-visible
  // intent, simpler model.
  rerunTask: (id: string) =>
    request<Task>(`/api/v1/tasks/${id}/rerun`, { method: "POST" }),
  // Aggregate capacity across all connected runners. `running` is the
  // dispatcher-recorded inflight count, `capacity` is the sum of
  // max_parallel from every runner's CRP register frame.
  getRunnerCapacity: () =>
    request<{ running: number; capacity: number; runners: number }>(
      `/api/v1/runners/capacity`,
    ),
  // Parent tasks this task depends on. Used by the review dialog to
  // surface dependency context so the user can confirm "yes, the deps
  // I checked at create time are still wired up" before submitting.
  getTaskDependencies: (id: string) =>
    request<{
      parents: Array<{ id: string; title: string; status: string }>;
    }>(`/api/v1/tasks/${id}/dependencies`),
  // P7 multimodal: upload a file the user wants the agent to consume.
  // Multipart endpoint, NOT the JSON `request()` helper — fetch directly.
  uploadAttachment: async (params: {
    workspaceId: string;
    file: File;
  }): Promise<{
    id: string;
    workspace_id: string;
    original_filename: string;
    content_type: string;
    size_bytes: number;
    sha256: string;
    created_at: string;
  }> => {
    const fd = new FormData();
    fd.append("workspace_id", params.workspaceId);
    fd.append("file", params.file);
    const r = await fetch("/api/v1/attachments", {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
  attachmentDownloadUrl: (id: string) => `/api/v1/attachments/${id}/file`,
  // Attachments linked to a task. Used by drawer / cards to show what
  // the agent had access to.
  getTaskAttachments: (id: string) =>
    request<{
      items: Array<{
        id: string;
        original_filename: string;
        content_type: string;
        size_bytes: number;
        created_at: string;
      }>;
    }>(`/api/v1/tasks/${id}/attachments`),
  // P6.3 sandbox endpoints. start blocks until container is ready
  // (~3-5s static / 5-15s node). UI should show a loading state.
  startSandbox: (taskId: string, outputIdx?: number) => {
    const qs = outputIdx !== undefined ? `?output_idx=${outputIdx}` : "";
    return request<{ task_id: string; base_path: string; runtime: string }>(
      `/api/v1/sandbox/${taskId}/start${qs}`,
      { method: "POST" },
    );
  },
  // P6.4 viewers.
  getSandboxManifest: (taskId: string) =>
    request<{
      task_id: string;
      outputs: Array<{
        kind: "interactive" | "document" | "code" | "data" | "image" | "archive";
        entry: string;
        label: string;
        source: string;
        runtime: string | null;
        port: number | null;
      }>;
    }>(`/api/v1/sandbox/${taskId}/manifest`),
  /** URL string useful for <img src=...> where the browser fetches
   *  directly with the user's session cookie. */
  sandboxFileUrl: (taskId: string, path: string) =>
    `/api/v1/sandbox/${taskId}/file?path=${encodeURIComponent(path)}`,
  /** Fetch a file's text content. Returns null on 404 / non-text. */
  fetchSandboxFile: async (taskId: string, path: string): Promise<{ text: string; contentType: string }> => {
    const url = `/api/v1/sandbox/${taskId}/file?path=${encodeURIComponent(path)}`;
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) {
      throw new Error(`fetch ${path} failed: ${r.status}`);
    }
    return {
      text: await r.text(),
      contentType: r.headers.get("content-type") ?? "",
    };
  },
  getSandboxTree: (taskId: string) =>
    request<{
      task_id: string;
      entries: Array<{ path: string; kind: "file" | "dir"; size?: number }>;
    }>(`/api/v1/sandbox/${taskId}/tree`),
  stopSandbox: (taskId: string) =>
    request<{ found: boolean }>(
      `/api/v1/sandbox/${taskId}/stop`,
      { method: "POST" },
    ),
  getSandboxStatus: (taskId: string) =>
    request<{
      task_id: string;
      status: string;
      host_port: number | null;
      runtime: string | null;
      base_path: string | null;
      started_at: string | null;
      stopped_at: string | null;
      error_message: string | null;
    }>(`/api/v1/sandbox/${taskId}/status`),
  listTaskTurns: (id: string) =>
    request<TaskTurnsResponse>(`/api/v1/tasks/${id}/turns`),
  respondTask: (id: string, content: string) =>
    request<Task>(`/api/v1/tasks/${id}/respond`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  createTask: (body: Partial<Task> & {
    workspace_id: string; title: string; prompt: string; origin: string; agent: string;
    depends_on?: string[];
    idempotency_key?: string;
  }) =>
    request<Task>("/api/v1/tasks", { method: "POST", body: JSON.stringify(body) }),
  // Async raw-input mode (P5.0). Returns a task in `parsing` state; the
  // server runs intent parse in the background and pushes a `task.parsed`
  // WS event when it transitions to pending (or parse_failed on error).
  createTaskFromRawInput: (body: {
    workspace_id: string;
    raw_input: string;
    idempotency_key: string;
    origin?: "web" | "voice" | "im" | "text";
    hint?: "voice" | "text" | "im";
    depends_on?: string[];
    attachment_ids?: string[];
  }) =>
    request<Task>("/api/v1/tasks", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: body.workspace_id,
        raw_input: body.raw_input,
        idempotency_key: body.idempotency_key,
        origin: body.origin ?? "web",
        ...(body.depends_on && body.depends_on.length > 0
          ? { depends_on: body.depends_on }
          : {}),
        ...(body.attachment_ids && body.attachment_ids.length > 0
          ? { attachment_ids: body.attachment_ids }
          : {}),
      }),
    }),
  updateTask: (id: string, body: Partial<Pick<Task, "title" | "description" | "prompt" | "agent" | "repo">>) =>
    request<Task>(`/api/v1/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listWorkspaces: () => request<{ items: Workspace[] }>("/api/v1/workspaces"),
  createWorkspace: (body: { slug: string; name: string }) =>
    request<Workspace>("/api/v1/workspaces", { method: "POST", body: JSON.stringify(body) }),
  parseIntent: (body: { raw_input: string; hint?: "voice" | "text" | "im"; context?: IntentContext }) =>
    request<ParsedIntent>("/api/v1/intent/parse", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  transcribeAudio: async (blob: Blob): Promise<{ transcript: string }> => {
    const r = await fetch("/api/v1/stt", {
      method: "POST",
      headers: { "content-type": blob.type || "audio/webm" },
      body: blob,
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return (await r.json()) as { transcript: string };
  },
  getMe: () => request<Me>("/api/v1/auth/me"),
  authConfig: () =>
    request<{
      auth_mode: "disabled" | "cognito";
      allow_signup: boolean;
      cognito_pool_name: string | null;
    }>("/api/v1/auth/config"),
  loginInit: (body: { email: string; srp_a: string }) =>
    request<{
      flow_id: string;
      srp_b: string;
      salt: string;
      secret_block: string;
      username_internal: string;
    }>("/api/v1/auth/login/init", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  loginRespond: (body: {
    flow_id: string;
    password_proof: string;
    timestamp: string;
    secret_block: string;
  }) =>
    request<LoginResult>("/api/v1/auth/login/respond", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  loginTotp: (body: { flow_id: string; code: string }) =>
    request<LoginResult>("/api/v1/auth/login/totp", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  loginMfaSetup: (body: {
    flow_id: string;
    code: string;
    friendly_device_name: string;
  }) =>
    request<LoginResult>("/api/v1/auth/login/mfa-setup", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  loginNewPassword: (body: { flow_id: string; new_password: string }) =>
    request<LoginResult>("/api/v1/auth/login/new-password", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  signup: (body: { email: string; password: string }) =>
    request<{ status: string; user_sub?: string }>("/api/v1/auth/signup", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  signupConfirm: (body: { email: string; code: string }) =>
    request<{ status: string }>("/api/v1/auth/signup/confirm", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  signupResend: (body: { email: string }) =>
    request<{ status: string }>("/api/v1/auth/signup/resend", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  passwordForgot: (body: { email: string }) =>
    request<{ status: string }>("/api/v1/auth/password/forgot", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  passwordReset: (body: { email: string; code: string; new_password: string }) =>
    request<{ status: string }>("/api/v1/auth/password/reset", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  logout: () => request<void>("/api/v1/auth/logout", { method: "POST" }),
  bootstrapOwnership: () =>
    request<{ claimed: number }>("/api/v1/auth/bootstrap-ownership", { method: "POST" }),
  inviteMember: (workspaceId: string) =>
    request<{ code: string; expires_at: string }>(
      `/api/v1/workspaces/${workspaceId}/invites`,
      { method: "POST" },
    ),
  joinWorkspace: (code: string) =>
    request<void>("/api/v1/workspaces/join", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  listMembers: (workspaceId: string) =>
    request<{ items: Member[] }>(`/api/v1/workspaces/${workspaceId}/members`),
  costsSummary: (workspace_id: string, from?: string, to?: string) => {
    const qs = new URLSearchParams({ workspace_id });
    if (from) qs.set("from", from);
    if (to) qs.set("to", to);
    return request<CostSummary>(`/api/v1/costs/summary?${qs}`);
  },
  costsEvents: (workspace_id: string, limit = 50) =>
    request<{ items: CostEvent[] }>(`/api/v1/costs/events?workspace_id=${workspace_id}&limit=${limit}`),
  auditEvents: (workspace_id: string, kind?: string, limit = 50) => {
    const qs = new URLSearchParams({ workspace_id, limit: String(limit) });
    if (kind) qs.set("kind", kind);
    return request<{ items: AuditEvent[] }>(`/api/v1/audit?${qs}`);
  },
  transitionTask: (id: string, body: { to: string; reason?: string }) =>
    request<Task>(`/api/v1/tasks/${id}/transition`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listMemory: (workspace_id: string, opts?: { q?: string; limit?: number }) => {
    const qs = new URLSearchParams({ workspace_id });
    if (opts?.q) qs.set("q", opts.q);
    if (opts?.limit) qs.set("limit", String(opts.limit));
    return request<{ items: MemoryChunk[] }>(`/api/v1/memory?${qs}`);
  },
  addMemory: (body: { workspace_id: string; text: string; source_kind?: string; meta?: Record<string, unknown> }) =>
    request<MemoryChunk>(`/api/v1/memory`, { method: "POST", body: JSON.stringify(body) }),
  deleteMemory: (chunk_id: string) =>
    request<void>(`/api/v1/memory/${chunk_id}`, { method: "DELETE" }),
};
