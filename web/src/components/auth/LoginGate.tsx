// Login gate (Wave 3 / SEC-1). Only rendered when VITE_AUTH_ENABLED === "true".
// In the default demo/mock build this component is never mounted (App short-
// circuits), so the demo has no login step.

import { useState } from "react";
import { login, type Role } from "@/lib/auth";

export function LoginGate({ onAuthed }: { onAuthed: (role: Role) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const role = await login(username, password);
      onAuthed(role);
    } catch {
      setErr("Invalid credentials");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-background p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-card p-6 shadow-sm"
      >
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">JNPA UC-III — Sign in</h1>
          <p className="text-xs text-muted-foreground">
            Role-scoped access (JNPA Traffic · Terminal Ops · Customs · Traffic Police · Driver ·
            DTCCC Admin).
          </p>
        </div>
        <label className="block space-y-1">
          <span className="text-sm font-medium">Username</span>
          <input
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="block space-y-1">
          <span className="text-sm font-medium">Password</span>
          <input
            type="password"
            className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {err ? <div className="text-sm text-red-600">{err}</div> : null}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
