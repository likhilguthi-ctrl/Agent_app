"use client";

import { useState } from "react";
import NewUserForm from "@/components/NewUserForm";
import ExistingUsersTab from "@/components/ExistingUsersTab";

export default function EmployeeDashboard() {
  const [tab, setTab] = useState<"new" | "existing">("new");

  return (
    <div className="w-full">
      <div className="mb-5 sm:mb-6">
        <h1 className="font-display text-xl sm:text-2xl font-semibold text-navy-800">Home</h1>
        <p className="text-sm text-slate-500 mt-1 leading-5">
          Subscriber enrollment is unavailable. Existing referrals are shown separately.
        </p>
      </div>

      <div className="flex w-full sm:w-fit items-center gap-1 mt-4 sm:mt-6 bg-white border border-slate-200 rounded-xl p-1 shadow-sm">
        <button
          onClick={() => setTab("new")}
          className={`flex-1 sm:flex-none px-3.5 sm:px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
            tab === "new" ? "bg-navy-700 text-white" : "text-slate-500 hover:text-navy-700"
          }`}
        >
          Enroll subscriber
        </button>
        <button
          onClick={() => setTab("existing")}
          className={`flex-1 sm:flex-none px-3.5 sm:px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
            tab === "existing" ? "bg-navy-700 text-white" : "text-slate-500 hover:text-navy-700"
          }`}
        >
          Existing users
        </button>
      </div>

      <div className="mt-5 sm:mt-6">
        {tab === "new" ? (
          <NewUserForm />
        ) : (
          <ExistingUsersTab refreshKey={0} />
        )}
      </div>
    </div>
  );
}
