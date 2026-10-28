import EmployeeDetail from "@/components/EmployeeDetail";

export default function AdminEmployeeDetailPage({ params }: { params: { id: string } }) {
  return <EmployeeDetail basePath="/admin" id={params.id} />;
}
