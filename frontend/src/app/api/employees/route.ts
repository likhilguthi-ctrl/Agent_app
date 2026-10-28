import { NextResponse } from "next/server";
import { getAllEmployees, EMPLOYEE_LIMIT } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET() {
  const session = getSession();
  if (!session || session.type !== "admin") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const employees = await getAllEmployees();
  return NextResponse.json({ employees, limit: EMPLOYEE_LIMIT });
}

export async function POST() {
  return NextResponse.json(
    { error: "EMPLOYEE_PROVISIONING_UNAVAILABLE" },
    { status: 503, headers: { "Cache-Control": "no-store" } }
  );
}
