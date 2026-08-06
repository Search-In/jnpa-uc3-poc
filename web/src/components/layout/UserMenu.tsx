// UserMenu — the signed-in identity and the sign-out control in the app header.
//
// Renders nothing in the default demo/mock build (VITE_AUTH_ENABLED !== "true"),
// where there is no login step and therefore no session to show or end. Before
// this existed the console had no logout at all: clearSession() was defined in
// lib/auth.ts and never called, so the only way out was clearing localStorage by
// hand.

import { LogOut, ShieldAlert, User } from "lucide-react";
import {
  authEnabled,
  getRole,
  getUsername,
  logout,
  mustChangePassword,
  ROLE_LABELS,
} from "@/lib/auth";

export function UserMenu() {
  const role = getRole();
  const username = getUsername();

  // No session (or an auth-disabled build) -> nothing to render.
  if (!authEnabled() || !role) return null;

  const roleLabel = ROLE_LABELS[role] ?? role;
  const needsPasswordChange = mustChangePassword();

  return (
    <div className="flex items-center gap-1.5">
      <div
        className="flex items-center gap-2 rounded-md border border-border px-2.5 py-1"
        title={`Signed in as ${username ?? "user"} (${roleLabel})`}
      >
        <span
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary"
          aria-hidden
        >
          <User className="h-3.5 w-3.5" strokeWidth={2.2} />
        </span>
        <span className="hidden flex-col items-start leading-tight md:flex">
          <span className="max-w-[10rem] truncate text-[13px] font-semibold text-foreground">
            {username ?? "—"}
          </span>
          <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {roleLabel}
          </span>
        </span>
        {needsPasswordChange && (
          <span
            className="inline-flex items-center gap-1 rounded-full bg-severity-warning/10 px-1.5 py-0.5 text-[10px] font-semibold text-severity-warning"
            title="This account still uses its bootstrap password. Set your own password."
          >
            <ShieldAlert className="h-3 w-3" />
            <span className="hidden lg:inline">Change password</span>
          </span>
        )}
      </div>

      <button
        type="button"
        onClick={() => logout()}
        aria-label="Sign out"
        title="Sign out"
        className="inline-flex h-9 items-center gap-1.5 rounded-md border border-border px-2.5 text-[13px] font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        <LogOut className="h-3.5 w-3.5" />
        <span className="hidden md:inline">Sign out</span>
      </button>
    </div>
  );
}

export default UserMenu;
