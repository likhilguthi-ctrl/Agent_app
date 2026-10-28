"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function AgentProfilePage() {
  return (
    <div className="py-2">
      <ProfileViewEdit
        title="Field Agent Profile"
        subtitle="Manage your personal details, profile photo, and review your assigned Mandal and operational hierarchy."
      />
    </div>
  );
}

