// Web-side auth + RBAC (Wave 3 / SEC-1). Mirrors the gateway role model and the
// per-screen policy so the UI hides screens a role can't reach AND every request
// carries the bearer token.
//
// Flag-gated: when VITE_AUTH_ENABLED is not "true" (the default demo/mock build)
// this is a no-op — there is no login, all screens render, and no token is sent,
// so the demo is frictionless. When enabled, the app shows a login gate and the
// nav/routes are filtered by the logged-in role.

export type Role =
  | "JNPA_TRAFFIC"
  | "TERMINAL_OPS"
  | "CUSTOMS"
  | "TRAFFIC_POLICE"
  | "DRIVER"
  | "DTCCC_ADMIN"
  | "TRANSPORTER";

export const ALL_ROLES: Role[] = [
  "JNPA_TRAFFIC",
  "TERMINAL_OPS",
  "CUSTOMS",
  "TRAFFIC_POLICE",
  "DRIVER",
  "DTCCC_ADMIN",
  "TRANSPORTER",
];

/** Human labels for the roles an account can hold. The console is specified in
 *  terms of the six stakeholder roles; the operator-facing names used when
 *  creating accounts (ADMIN / OPERATOR / GATE_USER) are aliases the gateway
 *  normalises — see gateway/auth.py ROLE_ALIASES. */
export const ROLE_LABELS: Record<Role, string> = {
  JNPA_TRAFFIC: "JNPA Traffic",
  TERMINAL_OPS: "Terminal Operator",
  CUSTOMS: "Customs / Gate",
  TRAFFIC_POLICE: "Traffic Police",
  DRIVER: "Driver",
  DTCCC_ADMIN: "DTCCC Admin",
  TRANSPORTER: "Transport Partner",
};

const CONTROL_ROOM: Role[] = ["JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS"];

/** Screen path -> roles allowed to see it. Mirrors gateway/auth.py _POLICY so the
 *  UI never offers a screen whose data the gateway would 403. */
export const SCREEN_ROLES: Record<string, Role[]> = {
  // Command Center is the shared DTCCC landing page — every role lands here.
  "/command-center": ALL_ROLES,
  // Consolidated Alerts Center — control room + enforcement + customs.
  "/alerts": [...CONTROL_ROOM, "TRAFFIC_POLICE", "CUSTOMS"],
  "/live": ALL_ROLES,
  // Transport partners see the advisory feed for their vehicles alongside the
  // control room and drivers; every other screen below stays closed to them.
  "/advisory": [...CONTROL_ROOM, "DRIVER", "TRANSPORTER"],
  "/geofencing": [...CONTROL_ROOM, "TRAFFIC_POLICE"],
  "/geofence-events": [...CONTROL_ROOM, "TRAFFIC_POLICE"],
  "/reports": [...CONTROL_ROOM, "TRAFFIC_POLICE", "CUSTOMS"],
  // FASTag ULIP — mirrors gateway/auth.py /api/fastag policy (control room + customs).
  "/fastag": [...CONTROL_ROOM, "CUSTOMS"],
  "/intelligence": [...CONTROL_ROOM, "TRAFFIC_POLICE", "CUSTOMS"],
  // Customs & Gate console (e-Seal/Form-13/Weighbridge/ICEGATE/Auto-LEO).
  "/gate-customs": [...CONTROL_ROOM, "CUSTOMS"],
  // Parking Management dashboard — control room + traffic police.
  "/parking": [...CONTROL_ROOM, "TRAFFIC_POLICE"],
  // Driver enrollment approval — biometric-sensitive, mirrors the gateway
  // /api/identity policy (customs + admin only).
  "/enrollments": ["DTCCC_ADMIN", "CUSTOMS"],
  // Vehicle Master administration — same audience as the enrollment surface it feeds.
  "/vehicles": ["DTCCC_ADMIN", "CUSTOMS"],
  // CFS-ECY CODECO off-dock container movements (read-only) — container/customs
  // audience, same as the Customs & Gate + FASTag consoles.
  "/cfs-ecy": [...CONTROL_ROOM, "CUSTOMS"],
  // UC-3 Lifecycle console (job spine + gate documents + ECY→CFS chains) —
  // mirrors the gateway policy for /api/jobs, /api/gate-docs and /api/scan.
  "/uc3-lifecycle": [...CONTROL_ROOM, "CUSTOMS"],
  "/truck-ops": [...CONTROL_ROOM, "CUSTOMS"],
  // T-04 Truck Visit Detail (real gate documents + original scans) — same
  // audience and same gateway policy as /api/gate-docs.
  "/truck-visit": [...CONTROL_ROOM, "CUSTOMS"],
  // T-02 Gate & Lane Board — gate operations. Reads are open to the same
  // audience as the other gate screens; the WRITES (lane reassignment, release
  // recompute) are control-room-only, enforced in gateway/auth.py.
  "/gate-lane-board": [...CONTROL_ROOM, "CUSTOMS"],
  "/vehicle-registry": [...CONTROL_ROOM, "CUSTOMS"],
  "/corridor-simulation": [...CONTROL_ROOM, "CUSTOMS"],
  // Shipping Lines (IAL/EAL/EDO) — mirrors gateway/auth.py /api/shipping-lines policy.
  "/shipping-lines": [...CONTROL_ROOM, "CUSTOMS"],
  // Berthing Reports (module 7) — mirrors gateway/auth.py /api/berthing policy.
  "/berthing": [...CONTROL_ROOM, "CUSTOMS"],
  "/health": CONTROL_ROOM,
  // Cargo What-If — mirrors the gateway policy for /api/cargo writes
  // (gateway/auth.py _METHOD_POLICY: control room + customs). The simulate
  // endpoints are POSTs and inherit that rule, so the screen must not be
  // visible to a role whose token the API would refuse.
  "/cargo-whatif": [...CONTROL_ROOM, "CUSTOMS"],
  "/what-if": CONTROL_ROOM,
  "/whatif": CONTROL_ROOM,
  "/simulator": CONTROL_ROOM,
  "/demo": CONTROL_ROOM,
};

export function authEnabled(): boolean {
  return import.meta.env.VITE_AUTH_ENABLED === "true";
}

const TOKEN_KEY = "jnpa_uc3_token";
const ROLE_KEY = "jnpa_uc3_role";
const USER_KEY = "jnpa_uc3_user";
const PWD_CHANGE_KEY = "jnpa_uc3_must_change_password";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getRole(): Role | null {
  try {
    const r = localStorage.getItem(ROLE_KEY);
    return r && (ALL_ROLES as string[]).includes(r) ? (r as Role) : null;
  } catch {
    return null;
  }
}

/** The signed-in account name, for display in the header. */
export function getUsername(): string | null {
  try {
    return localStorage.getItem(USER_KEY);
  } catch {
    return null;
  }
}

/** True when the account still carries the bootstrap password it was seeded with. */
export function mustChangePassword(): boolean {
  try {
    return localStorage.getItem(PWD_CHANGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function setSession(
  token: string,
  role: Role,
  username?: string | null,
  needsPasswordChange = false,
): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(ROLE_KEY, role);
    if (username) localStorage.setItem(USER_KEY, username);
    localStorage.setItem(PWD_CHANGE_KEY, needsPasswordChange ? "true" : "false");
  } catch {
    /* storage unavailable; session is in-memory only for this load */
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ROLE_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(PWD_CHANGE_KEY);
  } catch {
    /* ignore */
  }
}

/** Sign out: drop the stored session and return to the login gate.
 *
 *  The JWT is stateless, so this is a client-side action — the gateway cannot
 *  revoke an already-issued token (max 8 h TTL). Disabling the account via
 *  /api/users/{username}/disable is the server-side revocation path: it fails the
 *  next login and the next verifySession() check. */
export function logout(): void {
  clearSession();
  // Full reload rather than a route change: it tears down every cached query and
  // open WebSocket that was opened with the previous identity's token. Guarded
  // because navigation is unimplemented in jsdom (unit tests).
  try {
    window.location.assign("/");
  } catch {
    /* non-browser environment */
  }
}

/** Roles permitted on a screen path (defaults to ALL_ROLES if unmapped). */
export function rolesForScreen(path: string): Role[] {
  return SCREEN_ROLES[path] ?? ALL_ROLES;
}

/** Can the given role (or current session role) see the screen? Always true when
 *  auth is disabled. */
export function canSeeScreen(path: string, role: Role | null = getRole()): boolean {
  if (!authEnabled()) return true;
  if (!role) return false;
  return rolesForScreen(path).includes(role);
}

/** Sign in against core.app_user and store the resulting session.
 *
 *  The gateway returns one opaque 401 for every failure (unknown user, wrong
 *  password, disabled account) so the endpoint cannot be used to enumerate
 *  accounts — there is deliberately nothing here to tell those cases apart. */
export async function login(username: string, password: string): Promise<Role> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error("invalid credentials");
  const data = (await res.json()) as {
    access_token: string;
    role: Role;
    username?: string;
    must_change_password?: boolean;
  };
  setSession(
    data.access_token,
    data.role,
    data.username ?? username,
    Boolean(data.must_change_password),
  );
  return data.role;
}

export type SessionInfo = {
  username: string;
  role: Role;
  full_name?: string | null;
  must_change_password?: boolean;
};

/** Validate the stored session against the gateway.
 *
 *  Returns the live identity, or null when the token is expired/invalid or the
 *  account has since been disabled. Without this the console would keep rendering
 *  with an 8-hour-expired token, every panel erroring and no route back to the
 *  login gate. Refreshes the cached username/flag as a side effect. */
export async function verifySession(): Promise<SessionInfo | null> {
  const token = getToken();
  if (!token) return null;
  try {
    const res = await fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    const data = (await res.json()) as SessionInfo;
    if (!data?.role || !(ALL_ROLES as string[]).includes(data.role)) return null;
    setSession(token, data.role, data.username, Boolean(data.must_change_password));
    return data;
  } catch {
    // Network failure is not proof the session is bad — keep the existing
    // session and let the normal request path surface the outage. Only when
    // there is no stored role is there nothing to preserve.
    const role = getRole();
    return role ? { username: getUsername() ?? "", role } : null;
  }
}

/** Change the signed-in account's own password (clears must_change_password). */
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const token = getToken();
  const res = await fetch("/api/auth/change-password", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? "could not change password");
  }
  try {
    localStorage.setItem(PWD_CHANGE_KEY, "false");
  } catch {
    /* ignore */
  }
}
