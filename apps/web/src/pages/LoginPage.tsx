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
import { api, type LoginResult } from "@/api/client";
import { SrpExchange } from "@/lib/srp";
import { MfaEnrollPanel } from "./auth/MfaEnrollPanel";
import { MfaChallengePanel } from "./auth/MfaChallengePanel";
import { NewPasswordPanel } from "./auth/NewPasswordPanel";

type LoginState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "totp_required"; flowId: string; email: string; password: string }
  | {
      kind: "mfa_setup";
      flowId: string;
      email: string;
      password: string;
      otpauthUri: string;
      secret: string;
    }
  | {
      kind: "new_password_required";
      flowId: string;
      email: string;
      password: string;
    }
  | { kind: "error"; message: string };

type Props = {
  allowSignup: boolean;
  poolName: string;
  onSignupClick: () => void;
  onSuccess: () => void;
};

export function LoginPage({ allowSignup, poolName, onSignupClick, onSuccess }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState<LoginState>({ kind: "idle" });

  async function applyResult(
    result: LoginResult,
    keep: { email: string; password: string; flowId?: string },
  ) {
    switch (result.status) {
      case "ok":
        onSuccess();
        return;
      case "totp_required":
        setState({
          kind: "totp_required",
          flowId: result.flow_id,
          email: keep.email,
          password: keep.password,
        });
        return;
      case "mfa_setup":
        setState({
          kind: "mfa_setup",
          flowId: result.flow_id,
          email: keep.email,
          password: keep.password,
          otpauthUri: result.otpauth_uri,
          secret: result.secret,
        });
        return;
      case "new_password_required":
        setState({
          kind: "new_password_required",
          flowId: result.flow_id,
          email: keep.email,
          password: keep.password,
        });
        return;
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email || !password) return;
    setState({ kind: "submitting" });
    try {
      const srp = new SrpExchange(poolName);
      const srpA = await srp.largeA();
      const init = await api.loginInit({ email, srp_a: srpA });
      const proof = await srp.proof({
        username: init.username_internal,
        password,
        srpB: init.srp_b,
        salt: init.salt,
        secretBlock: init.secret_block,
      });
      const result = await api.loginRespond({
        flow_id: init.flow_id,
        ...proof,
      });
      await applyResult(result, { email, password });
    } catch (e) {
      setState({
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }

  // Render branches keyed by state machine kind.
  if (state.kind === "totp_required") {
    return (
      <ShellCard title="Two-factor code" description="Open your authenticator app and enter the 6-digit code.">
        <MfaChallengePanel
          flowId={state.flowId}
          onResult={(r) => applyResult(r, state)}
          onBack={() => setState({ kind: "idle" })}
        />
      </ShellCard>
    );
  }
  if (state.kind === "mfa_setup") {
    return (
      <ShellCard
        title="Set up two-factor authentication"
        description="Scan the QR code with your authenticator (Google Authenticator, 1Password, Authy, etc.) and enter the first code it shows."
      >
        <MfaEnrollPanel
          flowId={state.flowId}
          email={state.email}
          otpauthUri={state.otpauthUri}
          secret={state.secret}
          onResult={(r) => applyResult(r, state)}
          onBack={() => setState({ kind: "idle" })}
        />
      </ShellCard>
    );
  }
  if (state.kind === "new_password_required") {
    return (
      <ShellCard
        title="Choose a new password"
        description="The temporary password you used has expired. Pick a new one to continue."
      >
        <NewPasswordPanel
          flowId={state.flowId}
          onResult={(r) => applyResult(r, state)}
          onBack={() => setState({ kind: "idle" })}
        />
      </ShellCard>
    );
  }

  return (
    <ShellCard
      title="Welcome to Taskdeck"
      description="Personal / team kanban for AI agents"
    >
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <div className="flex flex-col gap-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            disabled={state.kind === "submitting"}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={state.kind === "submitting"}
          />
        </div>
        {state.kind === "error" && (
          <div className="rounded border border-destructive/60 bg-destructive/10 p-2 text-sm text-destructive">
            {state.message}
          </div>
        )}
        <Button type="submit" disabled={state.kind === "submitting"}>
          {state.kind === "submitting" ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      {allowSignup && (
        <div className="mt-4 text-center text-sm text-muted-foreground">
          Don&apos;t have an account?{" "}
          <button
            type="button"
            className="font-medium underline-offset-4 hover:underline"
            onClick={onSignupClick}
          >
            Sign up
          </button>
        </div>
      )}
    </ShellCard>
  );
}

function ShellCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-xl">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
        <CardFooter />
      </Card>
    </div>
  );
}
