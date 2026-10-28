import { NextRequest, NextResponse } from "next/server";
import { getAdminByCredentials, logActivity } from "@/lib/repo";
import { setSessionCookie } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function POST(req: NextRequest) {
  const { username, password } = await req.json();
  if (!username || !password) {
    return NextResponse.json({ error: "Please enter your username and password." }, { status: 400 });
  }
  const admin = await getAdminByCredentials(String(username), String(password));
  if (!admin) {
    return NextResponse.json({ error: "Invalid username or password." }, { status: 401 });
  }
  setSessionCookie({ type: "admin", id: admin.id, role: admin.role, name: admin.name });
  await logActivity({ actor: admin.name, action: "Logged in", details: `${admin.role} logged in` });
  return NextResponse.json({ role: admin.role, name: admin.name });
}
