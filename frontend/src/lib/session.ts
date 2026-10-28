import { cookies } from "next/headers";

export interface AdminSession {
  type: "admin";
  id: string;
  role: "admin" | "superadmin";
  name: string;
}

export interface EmployeeSession {
  type: "employee";
  id: string;
  refId: string;
  name: string;
}

export type Session = AdminSession | EmployeeSession;

const COOKIE_NAME = "ep_session";

export function setSessionCookie(session: Session) {
  cookies().set(COOKIE_NAME, JSON.stringify(session), {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 7,
  });
}

export function getSession(): Session | null {
  const raw = cookies().get(COOKIE_NAME)?.value;
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Session;
  } catch {
    return null;
  }
}

export function clearSessionCookie() {
  cookies().delete(COOKIE_NAME);
}
