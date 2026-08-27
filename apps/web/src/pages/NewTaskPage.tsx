import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MapPin, Mic, Paperclip, Send, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Textarea } from "@/components/ui/textarea";
import { useVoiceInput } from "@/voice/useVoiceInput";
import { cn } from "@/lib/utils";

type Props = {
  activeWorkspaceId: string | null;
  onDone: () => void;
  onCancel: () => void;
  topSlot?: ReactNode;
  /** Prefill the raw_input textarea (used by the "Duplicate & edit" flow
   *  on done cards — we seed it with the source task's prompt so the user
   *  can tweak the wording and re-submit). */
  initialRawInput?: string;
};

/**
 * Single-step task creation. Phase 5.0 collapsed the old
 * "Parse → review → Submit" flow into "Type → Submit". The intent parse
 * loop runs server-side asynchronously; the kanban shows a skeleton card
 * while the parser works and animates it into the right column on
 * `task.parsed`.
 *
 * Optional dependency picker stays — it's a separate user choice and
 * doesn't depend on the parser output.
 */
// Per-file ceiling — keep in sync with backend TD_ATTACHMENT_MAX_BYTES.
const MAX_ATTACHMENT_BYTES = 300 * 1024 * 1024;

type UploadedAttachment = {
  id: string;
  filename: string;
  size: number;
};

export function NewTaskPage({ activeWorkspaceId, onDone, onCancel, topSlot, initialRawInput }: Props) {
  const qc = useQueryClient();
  const [rawInput, setRawInput] = useState(initialRawInput ?? "");
  const [selectedDeps, setSelectedDeps] = useState<string[]>([]);
  // Files the user has added. State holds both already-uploaded (id set)
  // and in-flight (id null + uploading flag) entries so the UI can show
  // progress without a separate map.
  const [attachments, setAttachments] = useState<UploadedAttachment[]>([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const voice = useVoiceInput();

  // One idempotency key per page mount. If the user double-clicks Submit or
  // the network retries, the same key returns the same task — no dupes.
  const idempotencyKey = useMemo(() => crypto.randomUUID(), []);

  // Esc closes the page like the Back button. Skip when an inner dialog
  // (e.g. dependency picker, voice prompt) is open — those mount their
  // own data-state="open" element which Radix and shadcn dialogs use,
  // and they intercept Esc themselves first; if any survives to here
  // we still want a back-out as a fallback.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // If a modal dialog is open, let it handle its own close first.
      if (document.querySelector('[role="dialog"][data-state="open"]')) return;
      e.preventDefault();
      onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Whenever the voice hook produces a transcript, drop it into the
  // textarea. The previous condition gated on phase === "recording"
  // (live preview from Web Speech). After we removed that path the
  // transcript only arrives once AWS Transcribe finishes — phase is
  // already back to "idle" — so the condition never fired and the
  // user saw an empty textarea after recording. Now we sync any
  // non-empty transcript that differs from current input.
  useEffect(() => {
    if (voice.transcript && voice.transcript !== rawInput) {
      setRawInput(voice.transcript);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.transcript]);

  const handleFiles = async (files: FileList | File[]) => {
    if (!activeWorkspaceId) {
      toast.error("Pick a workspace before adding files");
      return;
    }
    const list = Array.from(files);
    for (const f of list) {
      if (f.size > MAX_ATTACHMENT_BYTES) {
        toast.error(
          `${f.name}: too large ` +
          `(${(f.size / 1024 / 1024).toFixed(1)} MB > 300 MB)`,
        );
        continue;
      }
      setUploadingCount((n) => n + 1);
      try {
        const r = await api.uploadAttachment({
          workspaceId: activeWorkspaceId,
          file: f,
        });
        setAttachments((prev) => [
          ...prev,
          { id: r.id, filename: r.original_filename, size: r.size_bytes },
        ]);
      } catch (e) {
        toast.error(`${f.name}: upload failed — ${String(e)}`);
      } finally {
        setUploadingCount((n) => n - 1);
      }
    }
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (!activeWorkspaceId) throw new Error("no active workspace");
      const trimmed = rawInput.trim();
      if (!trimmed) throw new Error("input is empty");
      // Async path now carries depends_on through to the backend. The
      // backend records TaskDependency rows alongside the parsing task,
      // and the parse loop transitions to BLOCKED (not PENDING) when
      // any parent is still unfinished — DependencyResolver flips it
      // to PENDING when the last parent completes.
      return api.createTaskFromRawInput({
        workspace_id: activeWorkspaceId,
        raw_input: trimmed,
        idempotency_key: idempotencyKey,
        origin: voice.transcript ? "voice" : "web",
        depends_on: selectedDeps.length > 0 ? selectedDeps : undefined,
        attachment_ids: attachments.length > 0 ? attachments.map((a) => a.id) : undefined,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
      toast.success("Task submitted — parsing intent…");
      onDone();
    },
    onError: (err) => toast.error(String(err)),
  });

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border/60 bg-card/50 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-2xl mx-auto px-4 sm:px-6 h-12 flex items-center gap-3">
          <Button variant="ghost" size="sm" className="h-8 gap-1.5 text-muted-foreground" onClick={onCancel}>
            <ArrowLeft className="h-4 w-4" />
            Back
          </Button>
          <span className="text-border/60">|</span>
          <span className="font-semibold text-sm text-foreground">Taskdeck</span>
          {topSlot && <div className="ml-auto">{topSlot}</div>}
        </div>
      </header>

      <main className="max-w-2xl mx-auto px-4 sm:px-6 py-8 pb-16 sm:pb-8 space-y-6">
        {!activeWorkspaceId ? (
          <div className="flex flex-col items-center justify-center py-16 text-center text-muted-foreground">
            <MapPin className="h-12 w-12 mb-3 opacity-40" />
            <p className="text-sm font-medium mb-1">Pick a workspace</p>
            <p className="text-xs">Use the workspace picker in the top nav.</p>
          </div>
        ) : null}

        <div>
          <h1 className="text-2xl font-bold text-foreground tracking-tight">New task</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Describe what you need. The parser fills in the details after you submit —
            the kanban will show progress.
          </p>
        </div>

        <Card className="border-border/60">
          <CardContent className="pt-6 space-y-3">
            <Textarea
              rows={5}
              placeholder="Describe a task, or hold the mic to speak…"
              value={rawInput}
              onChange={(e) => setRawInput(e.target.value)}
              className="text-sm resize-none"
              onKeyDown={(e) => {
                // Cmd/Ctrl+Enter submits.
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && rawInput.trim() && activeWorkspaceId) {
                  submitMutation.mutate();
                }
              }}
            />

            <div className="flex items-center gap-2">
              <Button
                variant={voice.phase === "recording" ? "destructive" : "secondary"}
                size="sm"
                className={cn(
                  "h-8 gap-1.5 text-xs",
                  voice.phase === "recording" && "animate-pulse",
                )}
                onMouseDown={voice.start}
                onMouseUp={voice.stop}
                onTouchStart={voice.start}
                onTouchEnd={voice.stop}
                disabled={voice.phase === "transcribing"}
              >
                <Mic className="h-3.5 w-3.5" />
                {voice.phase === "recording"
                  ? "Recording… (release to stop)"
                  : voice.phase === "transcribing"
                  ? "Transcribing…"
                  : "Hold to speak"}
              </Button>

              {voice.error && <span className="text-destructive text-xs">{voice.error}</span>}

              <span className="ml-auto text-xs text-muted-foreground">
                ⌘/Ctrl + Enter to submit
              </span>
            </div>
          </CardContent>
        </Card>

        {activeWorkspaceId && (
          <Card className="border-border/60">
            <CardContent className="pt-6 space-y-2">
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Files (optional)
              </p>
              <p className="text-xs text-muted-foreground">
                Drop files for the agent to read or modify. Up to 300 MB each.
              </p>

              {/* Drop zone — clickable + drag target. Stays compact when
                  empty, expands inline as the user adds files. */}
              <div
                onClick={() => fileInputRef.current?.click()}
                onDragEnter={(e) => {
                  e.preventDefault();
                  setDragOver(true);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                  if (e.dataTransfer.files.length) {
                    void handleFiles(e.dataTransfer.files);
                  }
                }}
                className={cn(
                  "border-2 border-dashed rounded-md p-3 text-center text-xs cursor-pointer transition-colors",
                  dragOver
                    ? "border-primary bg-primary/5 text-primary"
                    : "border-border/60 text-muted-foreground hover:bg-muted/40",
                )}
              >
                <Paperclip className="h-4 w-4 inline mr-1.5" />
                {dragOver ? "Drop to upload" : "Click or drop files here"}
                {uploadingCount > 0 && (
                  <span className="ml-2 text-primary">uploading {uploadingCount}…</span>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  if (e.target.files?.length) {
                    void handleFiles(e.target.files);
                    e.target.value = "";
                  }
                }}
              />

              {attachments.length > 0 && (
                <ul className="space-y-1 pt-1">
                  {attachments.map((a) => (
                    <li
                      key={a.id}
                      className="flex items-center gap-2 text-xs bg-muted/40 rounded px-2 py-1"
                    >
                      <Paperclip className="h-3 w-3 shrink-0 text-muted-foreground" />
                      <span className="truncate flex-1">{a.filename}</span>
                      <span className="text-muted-foreground shrink-0">
                        {a.size < 1024
                          ? `${a.size} B`
                          : a.size < 1024 * 1024
                            ? `${(a.size / 1024).toFixed(0)} KB`
                            : `${(a.size / 1024 / 1024).toFixed(1)} MB`}
                      </span>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          removeAttachment(a.id);
                        }}
                        className="text-muted-foreground hover:text-destructive"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        )}

        {activeWorkspaceId && (
          <Card className="border-border/60">
            <CardContent className="pt-6 space-y-2">
              <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                Dependencies (optional)
              </p>
              <p className="text-xs text-muted-foreground">
                Block this task until selected tasks reach "done". Leave empty for none.
              </p>
              <DependencyPicker
                workspaceId={activeWorkspaceId}
                selected={selectedDeps}
                onChange={setSelectedDeps}
              />
            </CardContent>
          </Card>
        )}

        <div className="flex items-center gap-3 pb-8">
          <Button
            size="lg"
            className="min-w-[140px] gap-2"
            onClick={() => submitMutation.mutate()}
            disabled={!rawInput.trim() || !activeWorkspaceId || submitMutation.isPending}
          >
            <Send className="h-4 w-4" />
            {submitMutation.isPending ? "Submitting…" : "Submit"}
          </Button>
          {!activeWorkspaceId && (
            <span className="text-destructive text-sm">No active workspace</span>
          )}
          {submitMutation.isError && (
            <span className="text-destructive text-sm">{String(submitMutation.error)}</span>
          )}
        </div>
      </main>
    </div>
  );
}

function DependencyPicker({
  workspaceId,
  selected,
  onChange,
}: {
  workspaceId: string;
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const q = useQuery({
    queryKey: ["tasks", workspaceId, "deps-picker"],
    queryFn: api.listTasks,
    enabled: !!workspaceId,
  });

  if (q.isLoading) return <p className="text-xs text-muted-foreground">Loading tasks…</p>;

  const all = q.data?.items ?? [];
  const candidates = all
    .filter((t) => t.workspace_id === workspaceId && t.status !== "parsing")
    .slice(0, 20);

  if (candidates.length === 0) {
    return <p className="text-xs text-muted-foreground">No tasks in this workspace yet.</p>;
  }

  function toggle(id: string) {
    if (selected.includes(id)) onChange(selected.filter((x) => x !== id));
    else onChange([...selected, id]);
  }

  return (
    <ScrollArea className="max-h-[200px]">
      <div className="space-y-1.5">
        {candidates.map((t) => (
          <label key={t.id} className="flex items-center gap-2.5 cursor-pointer group">
            <Checkbox
              checked={selected.includes(t.id)}
              onCheckedChange={() => toggle(t.id)}
            />
            <span className="text-xs text-muted-foreground font-mono shrink-0">
              {t.id.slice(0, 8)}
            </span>
            <span className="text-sm text-foreground truncate group-hover:text-foreground/80">
              {t.title}
            </span>
            <Badge variant="secondary" className="text-xs shrink-0 font-mono px-1">
              {t.status}
            </Badge>
          </label>
        ))}
      </div>
    </ScrollArea>
  );
}
