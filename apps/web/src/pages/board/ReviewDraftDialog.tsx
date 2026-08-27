"use client";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, Link2, Paperclip, Sparkles } from "lucide-react";
import { api, type Task } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type Props = {
  task: Task | null;
  onClose: () => void;
  onSaved: () => void;
};

// Static enum of agents the dialog can offer. The parser may produce
// agent values outside this list (e.g. openclaw, hermes, agentcore-*);
// we render them as an extra <SelectItem> at the top of the menu so
// the dropdown isn't blank when task.agent isn't one of these.
const STATIC_AGENTS = ["shell", "claude-code", "kiro-cli", "openclaw", "hermes", "codex"] as const;

/**
 * Quick-fix dialog for parse_failed tasks. The parser couldn't produce a
 * usable spec (LLM down, schema-invalid output, illegal agent). The user
 * reviews/edits the fields and clicks "Submit" to promote the task to
 * pending.
 *
 * Fields: title, agent, repo, prompt, plus a read-only Depends-on
 * summary so the user can see the dependencies they checked at create
 * time are still wired up. Dependencies aren't editable here — to
 * change them, cancel and use the structured /tasks API.
 */
export function ReviewDraftDialog({ task, onClose, onSaved }: Props) {
  const [title, setTitle] = useState("");
  const [agent, setAgent] = useState("claude-code");
  const [repo, setRepo] = useState("");
  const [prompt, setPrompt] = useState("");

  // Sync local form with the task when it changes.
  useEffect(() => {
    if (task) {
      setTitle(task.title);
      setAgent(task.agent || "claude-code");
      setRepo(task.repo ?? "");
      setPrompt(task.prompt);
    }
  }, [task]);

  // Pull dependencies whenever the dialog has a task. Read-only
  // surface — we don't let the user edit deps here; we just want them
  // to see the chain so they don't fear it got dropped.
  const depsQ = useQuery({
    queryKey: ["task", task?.id, "dependencies"],
    queryFn: () => api.getTaskDependencies(task!.id),
    enabled: !!task,
    staleTime: 30_000,
    retry: false,
  });

  // P7: same idea as deps — show the user the files they uploaded so
  // they know the attachments are still wired up before submitting.
  const attachmentsQ = useQuery({
    queryKey: ["task", task?.id, "attachments"],
    queryFn: () => api.getTaskAttachments(task!.id),
    enabled: !!task,
    staleTime: 30_000,
    retry: false,
  });

  // Compose the agent dropdown options. We ALWAYS include the task's
  // current agent (even if it's an exotic value the parser invented)
  // so the select isn't blank — confirming an unknown value should
  // still be possible if the user trusts it.
  const agentOptions = useMemo(() => {
    const base = [...STATIC_AGENTS] as string[];
    if (agent && !base.includes(agent)) base.unshift(agent);
    return base;
  }, [agent]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!task) throw new Error("no task");
      // Save edits first.
      await api.updateTask(task.id, {
        title,
        agent,
        repo: repo || null,
        prompt,
      });
      // Then submit.
      return api.submitTask(task.id);
    },
    onSuccess: () => {
      toast.success("Task submitted");
      onSaved();
      onClose();
    },
    onError: (err) => toast.error(String(err)),
  });

  if (!task) return null;

  return (
    <Dialog open={!!task} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-info" />
            Fix &amp; submit
          </DialogTitle>
          <DialogDescription>
            The parser couldn't route this automatically. Confirm or fix the
            fields, then submit.
          </DialogDescription>
        </DialogHeader>

        {task.raw_input && (
          <div className="rounded-md bg-muted/50 p-3 text-xs text-muted-foreground border border-border/40">
            <span className="font-mono uppercase tracking-wider text-[10px] text-muted-foreground/70 block mb-1">
              Original input
            </span>
            {task.raw_input}
          </div>
        )}

        {depsQ.data && depsQ.data.parents.length > 0 && (
          <div className="rounded-md bg-info/5 border border-info/30 p-3 text-xs">
            <span className="flex items-center gap-1.5 font-mono uppercase tracking-wider text-[10px] text-info mb-1.5">
              <Link2 className="h-3 w-3" />
              Depends on
            </span>
            <ul className="space-y-1">
              {depsQ.data.parents.map((p) => (
                <li
                  key={p.id}
                  className="flex items-center gap-2 text-muted-foreground"
                >
                  <span className="font-mono text-[10px] uppercase opacity-70 w-14 shrink-0">
                    {p.status}
                  </span>
                  <span className="truncate">{p.title}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {attachmentsQ.data && attachmentsQ.data.items.length > 0 && (
          <div className="rounded-md bg-muted/40 border border-border/40 p-3 text-xs">
            <span className="flex items-center gap-1.5 font-mono uppercase tracking-wider text-[10px] text-muted-foreground mb-1.5">
              <Paperclip className="h-3 w-3" />
              Attached files
            </span>
            <ul className="space-y-1">
              {attachmentsQ.data.items.map((a) => (
                <li
                  key={a.id}
                  className="flex items-center gap-2 text-muted-foreground"
                >
                  <span className="truncate flex-1">{a.original_filename}</span>
                  <span className="shrink-0 opacity-70">
                    {a.size_bytes < 1024
                      ? `${a.size_bytes} B`
                      : a.size_bytes < 1024 * 1024
                        ? `${(a.size_bytes / 1024).toFixed(0)} KB`
                        : `${(a.size_bytes / 1024 / 1024).toFixed(1)} MB`}
                  </span>
                  <a
                    href={api.attachmentDownloadUrl(a.id)}
                    download={a.original_filename}
                    className="text-muted-foreground hover:text-foreground"
                    title={`Download ${a.original_filename}`}
                  >
                    <Download className="h-3.5 w-3.5" />
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="space-y-3">
          <Field label="Title">
            <Input value={title} onChange={(e) => setTitle(e.target.value)} className="text-sm" />
          </Field>
          <Field label="Agent">
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger className="h-8 text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {agentOptions.map((a) => (
                  <SelectItem key={a} value={a}>
                    {a}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field label="Repo (optional)">
            <Input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="github.com/org/name or full URL"
              className="text-sm font-mono"
            />
          </Field>
          <Field label="Prompt">
            <Textarea
              rows={6}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              className="text-sm font-mono resize-none"
            />
          </Field>
        </div>

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !title.trim() || !prompt.trim()}
          >
            {mutation.isPending ? "Submitting…" : "Save & submit"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <label className="text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}
