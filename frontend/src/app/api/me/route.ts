import { NextResponse } from "next/server";
import { getSession } from "@/lib/session";

export const dynamic = "force-dynamic";


export async function GET() {
  const session = getSession();
  return NextResponse.json({ session });
}
