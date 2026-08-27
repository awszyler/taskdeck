import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, type LoginResult } from "@/api/client";

type Props = {
  flowId: string;
  email: string;
  otpauthUri: string;
  secret: string;
  onResult: (r: LoginResult) => Promise<void> | void;
  onBack: () => void;
};

export function MfaEnrollPanel({
  flowId,
  email,
  otpauthUri,
  secret,
  onResult,
  onBack,
}: Props) {
  const [svg, setSvg] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [device, setDevice] = useState(defaultDeviceName());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    QRCode.toString(otpauthUri, {
      type: "svg",
      color: { dark: "#ffffff", light: "#00000000" }, // dark-theme friendly
      margin: 1,
      width: 220,
    }).then((s) => {
      if (!cancelled) setSvg(s);
    });
    return () => {
      cancelled = true;
    };
  }, [otpauthUri]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!code) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.loginMfaSetup({
        flow_id: flowId,
        code,
        friendly_device_name: device.trim() || "Authenticator",
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
      <div className="flex flex-col items-center gap-2">
        <div
          className="rounded bg-muted p-3"
          aria-label={`Two-factor QR code for ${email}`}
          dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}
        />
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Can&apos;t scan? Show secret</summary>
          <code className="mt-2 block break-all rounded bg-muted p-2 font-mono">
            {secret}
          </code>
        </details>
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="device">Device name</Label>
        <Input
          id="device"
          type="text"
          value={device}
          onChange={(e) => setDevice(e.target.value)}
          disabled={busy}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor="totp-setup-code">First 6-digit code</Label>
        <Input
          id="totp-setup-code"
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
        {busy ? "Verifying…" : "Activate"}
      </Button>
      <Button type="button" variant="ghost" onClick={onBack} disabled={busy}>
        Back
      </Button>
    </form>
  );
}

function defaultDeviceName(): string {
  if (typeof navigator === "undefined") return "Browser";
  const ua = navigator.userAgent;
  if (/Macintosh/.test(ua)) return "Mac";
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android";
  if (/Windows/.test(ua)) return "Windows";
  return "Browser";
}
