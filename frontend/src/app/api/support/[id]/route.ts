import { NextRequest, NextResponse } from "next/server";
import { getSupportTicketById, resolveSupportTicket, logActivity } from "@/lib/repo";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function PATCH(_req: NextRequest, { params }: { params: { id: string } }) {
  const session = getSession();
  if (!session || session.type !== "admin") {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const ticket = await getSupportTicketById(params.id);
  if (!ticket) return NextResponse.json({ error: "Not found" }, { status: 404 });

  const updated = await resolveSupportTicket(params.id);
  await logActivity({ actor: session.name, action: "Resolved support ticket", details: ticket.employeeName });

  return NextResponse.json({ ticket: updated });
}
