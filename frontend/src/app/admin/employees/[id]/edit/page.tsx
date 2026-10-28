"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function AdminEmployeeEditPage({ params }: { params: { id: string } }) {
  return (
    <div className="py-2">
      <ProfileViewEdit
        targetProfileId={params.id}
        backHref={`/admin/employees/${params.id}`}
        title="Edit Agent Profile"
        subtitle="Update agent profile and contact details within your administrative scope."
      />
    </div>
  );
}

