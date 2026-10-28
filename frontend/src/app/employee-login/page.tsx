import Link from "next/link";
import { Card } from "@/components/ui";

export default function EmployeeLoginPage() {
  return (
    <main className="min-h-screen flex items-center justify-center bg-sand-50 px-6">
      <Card className="w-full max-w-md p-7">
        <h1 className="font-display text-xl font-semibold text-navy-800">
          Employee sign-in unavailable
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          The legacy employee sign-in has been disabled because it disclosed
          login codes and created unsigned sessions. Employee access will return
          after a trusted server-side identity and authorization flow is
          approved and implemented.
        </p>
        <Link
          href="/login"
          className="mt-6 inline-flex min-h-11 items-center justify-center rounded-lg bg-navy-700 px-4 py-2 text-sm font-semibold text-white"
        >
          Go to community sign-in
        </Link>
      </Card>
    </main>
  );
}
