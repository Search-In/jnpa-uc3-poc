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
  | "DTCCC_ADMIN";

export const ALL_ROLES: Role[] = [
  "JNPA_TRAFFIC",
  "TERMINAL_OPS",
  "CUSTOMS",
  "TRAFFIC_POLICE",
  "DRIVER",
  "DTCCC_ADMIN",
];

const CONTROL_ROOM: Role[] = ["JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS"];

/** Screen path -> roles allowed to see it. Mirrors gateway/auth.py _POLICY so the
 *  UI never offers a screen whose data the gateway would 403. */
export const SCREEN_ROLES: Record<string, Role[]> = {
  // Command Center is the shared DTCCC landing page — every role lands here.
  "/command-center": ALL_ROLES,
  // Consolidated Alerts Center — control room + enforcement + customs.
  "/alerts": [...CONTROL_ROOM, "TRAFFIC_POLICE", "CUSTOMS"],
  "/live": ALL_ROLES,
  "/advisory": [...CONTROL_ROOM, "DRIVER"],
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
  "/health": CONTROL_ROOM,
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

export function setSession(token: string, role: Role): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(ROLE_KEY, role);
  } catch {
    /* storage unavailable; session is in-memory only for this load */
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ROLE_KEY);
  } catch {
    /* ignore */
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

/** Mint a role token via the gateway dev-token seam (or a real login elsewhere). */
export async function login(username: string, password: string): Promise<Role> {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error("invalid credentials");
  const data = (await res.json()) as { access_token: string; role: Role };
  setSession(data.access_token, data.role);
  return data.role;
}
