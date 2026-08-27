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

export function MfaChallengePanel({ flowId, onResult, onBack }: Props) {
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!code) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.loginTotp({ flow_id: flowId, code });
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
        <Label htmlFor="totp-code">6-digit code</Label>
        <Input
          id="totp-code"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/[^0-9]/g, ""))}
          required
          disabled={busy}
        />
      </div>
      {err && (
        <div className="rounded border border-destructive/60 bg-destructive/10 p-2 text-sm text-destructive">
          {err}
        </div>
      )}
      <Button type="submit" disabled={busy || code.length < 6}>
        {busy ? "Verifying…" : "Verify"}
      </Button>
      <Button type="button" variant="ghost" onClick={onBack} disabled={busy}>
        Back
      </Button>
    </form>
  );
}
