"use client";

import ProfileViewEdit from "@/components/ProfileViewEdit";

export default function AgentCustomerDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="py-2">
      <ProfileViewEdit
        targetProfileId={params.id}
        backHref="/agent/customers"
        title="Subscriber Profile"
        subtitle="View and manage subscriber profile and contact details within your attribution scope."
      />
    </div>
  );
}

