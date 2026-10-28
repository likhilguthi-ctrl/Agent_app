import { NextRequest, NextResponse } from "next/server";

export function middleware(req: NextRequest) {
  const pathname = req.nextUrl.pathname;

  if (pathname === "/employee-login" || pathname === "/employee-login/") {
    return NextResponse.redirect(new URL("/login", req.url));
  }

  if (pathname === "/agent" || pathname === "/agent/") {
    return NextResponse.redirect(new URL("/agent/dashboard", req.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/admin/:path*",
    "/superadmin/:path*",
    "/agent/:path*",
    "/employee/:path*",
    "/employee-login",
  ],
};

