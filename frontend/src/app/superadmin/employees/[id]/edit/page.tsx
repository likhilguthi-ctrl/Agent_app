"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function SuperAdminEmployeeEditPage({ params }: { params: { id: string } }) {
  return (
    <div className="py-2">
      <ProfileViewEdit
        targetProfileId={params.id}
        backHref={`/superadmin/employees/${params.id}`}
        title="Edit Agent Profile"
        subtitle="Update agent profile and contact details as Super Admin."
      />
    </div>
  );
}

