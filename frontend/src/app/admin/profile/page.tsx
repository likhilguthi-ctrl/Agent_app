"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function AdminProfilePage() {
  return (
    <div className="py-2">
      <ProfileViewEdit
        title="Manager Profile"
        subtitle="Review and edit your manager contact details, profile photo, and view assigned geographic territories."
      />
    </div>
  );
}

