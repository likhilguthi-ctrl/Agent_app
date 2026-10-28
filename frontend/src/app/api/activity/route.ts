import { NextResponse } from "next/server";
import { getActivity } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function GET() {
  const session = getSession();
  if (!session || session.type !== "admin" || session.role !== "superadmin") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const activity = await getActivity(200);
  return NextResponse.json({ activity });
}
