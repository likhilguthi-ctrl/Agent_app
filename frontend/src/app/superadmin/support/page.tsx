import SupportTicketsList from "@/components/SupportTicketsList";

export default function SuperAdminSupportPage() {
  return (
    <div>
      <h1 className="font-display text-2xl font-semibold text-navy-800 mb-1">Support tickets</h1>
      <p className="text-sm text-slate-500 mb-6">Every issue raised by any employee, across all admins.</p>
      <SupportTicketsList />
    </div>
  );
}
