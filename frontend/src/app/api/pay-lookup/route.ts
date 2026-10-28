import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(
    { error: "PAYMENT_LOOKUP_UNAVAILABLE" },
    { status: 503, headers: { "Cache-Control": "no-store" } }
  );
}
