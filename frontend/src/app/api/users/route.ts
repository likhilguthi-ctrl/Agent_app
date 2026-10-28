import { NextRequest, NextResponse } from "next/server";
import {
  getUsersByEmployeeRefId,
  getAllUsers,
} from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const session = getSession();
  if (!session) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  if (session.type === "admin") {
    const refId = req.nextUrl.searchParams.get("refId");
    const users = refId ? await getUsersByEmployeeRefId(refId) : await getAllUsers();
    return NextResponse.json({ users });
  }

  const users = await getUsersByEmployeeRefId(session.refId);
  return NextResponse.json({ users });
}

export async function POST() {
  return NextResponse.json(
    { error: "SUBSCRIBER_ENROLLMENT_UNAVAILABLE" },
    { status: 503, headers: { "Cache-Control": "no-store" } }
  );
}
