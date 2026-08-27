import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, type LoginResult } from "@/api/client";

type Props = {
  flowId: string;
  onResult: (r: LoginResult) => Promise<void> | void;
  onBack: () => void;
};

export function NewPasswordPanel({ flowId, onResult, onBack }: Props) {
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (pw.length < 8) {
      setErr("Password must be at least 8 characters.");
      return;
    }
    if (pw !== pw2) {
      setErr("Passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const r = await api.loginNewPassword({
        flow_id: flowId,
        new_password: pw,
      });
      await onResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      <div className="flex flex-col gap-2">
        <Label htmlFor="np-pw">New password</Label>
        <Input
          id="np-pw"
          type="password"
          autoComplete="new-password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          required
          autoFocus
          disabled={busy}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="np-pw2">Confirm password</Label>
        <Input
          id="np-pw2"
          type="password"
          autoComplete="new-password"
          value={pw2}
          onChange={(e) => setPw2(e.target.value)}
          required
          disabled={busy}
        />
      </div>
      {err && (
        <div className="rounded border border-destructive/60 bg-destructive/10 p-2 text-sm text-destructive">
          {err}
        </div>
      )}
      <Button type="submit" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </Button>
      <Button type="button" variant="ghost" onClick={onBack} disabled={busy}>
        Back
      </Button>
    </form>
  );
}
