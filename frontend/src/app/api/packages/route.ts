import { NextResponse } from "next/server";
import { getAllPackages } from "@/lib/repo";

export const dynamic = "force-dynamic";


export async function GET() {
  const packages = await getAllPackages();
  return NextResponse.json({ packages });
}
