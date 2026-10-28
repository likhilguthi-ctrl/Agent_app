import AddEmployeeForm from "@/components/AddEmployeeForm";

export default function SuperAdminAddEmployeePage() {
  return (
    <div className="max-w-3xl">
      <h1 className="font-display text-2xl font-semibold text-navy-800 mb-1">
        Employee and Agent management
      </h1>
      <p className="text-sm text-slate-500 mb-6">
        Staff management is unavailable until its identity and appointment contracts are approved.
      </p>
      <AddEmployeeForm />
    </div>
  );
}
