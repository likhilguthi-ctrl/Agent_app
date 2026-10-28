import { NextResponse } from "next/server";
import { getEmployeeByRefId, getUsersByEmployeeRefId } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function GET() {
  const session = getSession();
  if (!session || session.type !== "employee") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const employee = await getEmployeeByRefId(session.refId);
  if (!employee) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const users = await getUsersByEmployeeRefId(session.refId);
  const paidUsers = users.filter((u) => u.paymentStatus === "paid");
  const commissionEarned = Math.round(
    paidUsers.reduce((sum, u) => sum + (u.amount * employee.commissionRate) / 100, 0)
  );
  return NextResponse.json({ employee, users, paidCount: paidUsers.length, commissionEarned });
}
