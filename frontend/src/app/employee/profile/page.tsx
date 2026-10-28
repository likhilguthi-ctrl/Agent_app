"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function EmployeeProfilePage() {
  return (
    <div className="py-2">
      <ProfileViewEdit
        title="My Profile"
        subtitle="Review and edit your personal contact details, address, and profile photo."
      />
    </div>
  );
}

