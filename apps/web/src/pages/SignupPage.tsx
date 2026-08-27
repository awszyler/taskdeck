import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/api/client";

type Step = "form" | "confirm" | "done";

type Props = {
  onBackToLogin: () => void;
};

export function SignupPage({ onBackToLogin }: Props) {
  const [step, setStep] = useState<Step>("form");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resendNotice, setResendNotice] = useState<string | null>(null);

  async function onSubmitForm(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (password.length < 8) {
      setErr("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setErr("Passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      await api.signup({ email, password });
      setStep("confirm");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onSubmitConfirm(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await api.signupConfirm({ email, code });
      setStep("done");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onResend() {
    setResendNotice(null);
    setErr(null);
    try {
      await api.signupResend({ email });
      setResendNotice("Confirmation code resent. Check your inbox.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-xl">
            {step === "form"
              ? "Create your account"
              : step === "confirm"
              ? "Confirm your email"
              : "All set"}
          </CardTitle>
          <CardDescription>
            {step === "form"
              ? "We'll set up two-factor authentication on your first sign-in."
              : step === "confirm"
              ? `We sent a 6-digit code to ${email}.`
              : "Your account is ready. Sign in to continue."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {step === "form" && (
            <form className="flex flex-col gap-4" onSubmit={onSubmitForm}>
              <div className="flex flex-col gap-2">
                <Label htmlFor="signup-email">Email</Label>
                <Input
                  id="signup-email"
                  type="email"
                  autoComplete="username"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  disabled={busy}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="signup-pw">Password</Label>
                <Input
                  id="signup-pw"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  disabled={busy}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="signup-pw2">Confirm password</Label>
                <Input
                  id="signup-pw2"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
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
                {busy ? "Creating…" : "Sign up"}
              </Button>
            </form>
          )}
          {step === "confirm" && (
            <form className="flex flex-col gap-4" onSubmit={onSubmitConfirm}>
              <div className="flex flex-col gap-2">
                <Label htmlFor="signup-code">6-digit code</Label>
                <Input
                  id="signup-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  autoFocus
                  value={code}
                  onChange={(e) =>
                    setCode(e.target.value.replace(/[^0-9]/g, ""))
                  }
                  required
                  disabled={busy}
                />
              </div>
              {err && (
                <div className="rounded border border-destructive/60 bg-destructive/10 p-2 text-sm text-destructive">
                  {err}
                </div>
              )}
              {resendNotice && (
                <div className="rounded border border-emerald-700/40 bg-emerald-900/20 p-2 text-sm text-emerald-300">
                  {resendNotice}
                </div>
              )}
              <Button type="submit" disabled={busy || code.length < 6}>
                {busy ? "Confirming…" : "Confirm"}
              </Button>
              <Button type="button" variant="ghost" onClick={onResend} disabled={busy}>
                Resend code
              </Button>
            </form>
          )}
          {step === "done" && (
            <div className="flex flex-col gap-4">
              <p className="text-sm text-muted-foreground">
                Sign in with your email and password. We&apos;ll prompt you to
                set up two-factor authentication on the first try.
              </p>
              <Button onClick={onBackToLogin}>Back to sign in</Button>
            </div>
          )}
        </CardContent>
        <CardFooter className="flex justify-center">
          {step !== "done" && (
            <button
              type="button"
              className="text-sm text-muted-foreground underline-offset-4 hover:underline"
              onClick={onBackToLogin}
            >
              Back to sign in
            </button>
          )}
        </CardFooter>
      </Card>
    </div>
  );
}
