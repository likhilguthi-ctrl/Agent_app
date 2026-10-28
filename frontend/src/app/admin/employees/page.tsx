import EmployeeList from "@/components/EmployeeList";

export default function AdminEmployeesPage() {
  return (
    <div>
      <h1 className="font-display text-2xl font-semibold text-navy-800 mb-1">Employees</h1>
      <p className="text-sm text-slate-500 mb-6">Add, review and manage everyone on your team.</p>
      <EmployeeList basePath="/admin" />
    </div>
  );
}
