import { NextRequest, NextResponse } from "next/server";
import { getSupportTickets, insertSupportTicket, getEmployeeByRefId, logActivity } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function GET() {
  const session = getSession();
  if (!session) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  if (session.type === "admin") {
    const tickets = await getSupportTickets();
    return NextResponse.json({ tickets });
  }
  const tickets = await getSupportTickets(session.refId);
  return NextResponse.json({ tickets });
}

export async function POST(req: NextRequest) {
  const session = getSession();
  if (!session || session.type !== "employee") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { message } = await req.json();
  if (!message || !String(message).trim()) {
    return NextResponse.json({ error: "Please describe the issue." }, { status: 400 });
  }
  const employee = await getEmployeeByRefId(session.refId);
  const employeeName = employee ? `${employee.firstName} ${employee.lastName}` : session.name;

  const ticket = await insertSupportTicket({
    employeeRefId: session.refId,
    employeeName,
    message: String(message).trim(),
  });

  await logActivity({
    actor: ticket.employeeName,
    action: "Raised a support ticket",
    details: ticket.message.slice(0, 120),
  });

  return NextResponse.json({ ticket });
}
