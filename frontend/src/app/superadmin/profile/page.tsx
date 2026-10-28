"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function SuperAdminProfilePage() {
  return (
    <div className="py-2">
      <ProfileViewEdit
        title="Super Admin Profile"
        subtitle="Manage your platform operator account details, contact email, and security settings."
      />
    </div>
  );
}

