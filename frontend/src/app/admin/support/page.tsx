import SupportTicketsList from "@/components/SupportTicketsList";

export default function AdminSupportPage() {
  return (
    <div>
      <h1 className="font-display text-2xl font-semibold text-navy-800 mb-1">Support tickets</h1>
      <p className="text-sm text-slate-500 mb-6">Issues raised by employees. Aim to resolve within 2–3 working days.</p>
      <SupportTicketsList />
    </div>
  );
}
