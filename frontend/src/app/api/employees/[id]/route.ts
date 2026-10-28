import { NextRequest, NextResponse } from "next/server";
import { getEmployeeById, setEmployeeStatus, getUsersByEmployeeRefId, logActivity } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function GET(_req: NextRequest, { params }: { params: { id: string } }) {
  const session = getSession();
  if (!session || session.type !== "admin") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const employee = await getEmployeeById(params.id);
  if (!employee) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const users = await getUsersByEmployeeRefId(employee.refId);
  return NextResponse.json({ employee, users });
}

export async function PATCH(req: NextRequest, { params }: { params: { id: string } }) {
  const session = getSession();
  if (!session || session.type !== "admin") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const body = await req.json();
  const employee = await getEmployeeById(params.id);
  if (!employee) return NextResponse.json({ error: "Not found" }, { status: 404 });

  if (body.status && (body.status === "active" || body.status === "inactive")) {
    if (body.status === "inactive" && !String(body.reason || "").trim()) {
      return NextResponse.json({ error: "A reason is required to deactivate an employee." }, { status: 400 });
    }
    const reason = body.status === "inactive" ? String(body.reason).trim() : undefined;
    const updated = await setEmployeeStatus(params.id, body.status, reason);
    await logActivity({
      actor: session.name,
      action: body.status === "active" ? "Activated employee" : "Deactivated employee",
      details:
        body.status === "inactive"
          ? `${employee.firstName} ${employee.lastName} (${employee.refId}) — reason: ${reason}`
          : `${employee.firstName} ${employee.lastName} (${employee.refId})`,
    });
    return NextResponse.json({ employee: updated });
  }

  return NextResponse.json({ employee });
}
