import EmployeeDetail from "@/components/EmployeeDetail";

export default function SuperAdminEmployeeDetailPage({ params }: { params: { id: string } }) {
  return <EmployeeDetail basePath="/superadmin" id={params.id} />;
}
