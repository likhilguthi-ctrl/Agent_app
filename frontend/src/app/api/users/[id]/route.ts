import { NextRequest, NextResponse } from "next/server";
import { getUserById, updateUser, logActivity } from "@/lib/repo";
import { getSession } from "@/lib/session";
import type { SubscriptionType } from "@/types";

export const dynamic = "force-dynamic";


const VALID_TYPES: SubscriptionType[] = ["existing", "payment_link", "notify_confirm"];

export async function PATCH(req: NextRequest, { params }: { params: { id: string } }) {
  const session = getSession();
  if (!session) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const body = await req.json();
  if (body.action === "mark-paid" || body.subscriptionType === "existing") {
    return NextResponse.json(
      { error: "PAYMENT_SETTLEMENT_UNAVAILABLE" },
      { status: 503, headers: { "Cache-Control": "no-store" } }
    );
  }

  const user = await getUserById(params.id);
  if (!user) return NextResponse.json({ error: "Not found" }, { status: 404 });

  // Employees may only edit their own referred users.
  if (session.type === "employee" && user.employeeRefId !== session.refId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const actorName = session.name;

  if (body.subscriptionType && VALID_TYPES.includes(body.subscriptionType)) {
    const previousType = user.subscriptionType;
    const update: { subscriptionType: SubscriptionType; notifiedAt?: string } = {
      subscriptionType: body.subscriptionType,
    };
    if (body.subscriptionType === "notify_confirm" && previousType !== "notify_confirm") {
      update.notifiedAt = new Date().toISOString();
    }
    await updateUser(params.id, update);
    await logActivity({
      actor: actorName,
      action: "Changed subscription type",
      details: `${user.name} — ${previousType} → ${body.subscriptionType}`,
    });
  }

  if (body.action === "resend") {
    await updateUser(params.id, { notifiedAt: new Date().toISOString() });
    await logActivity({ actor: actorName, action: "Resent payment link/notification", details: user.name });
  }

  const updated = await getUserById(params.id);
  return NextResponse.json({ user: updated });
}
