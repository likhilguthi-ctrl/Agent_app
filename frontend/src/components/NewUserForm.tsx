import { Alert, Card } from "@/components/ui";

export default function NewUserForm() {
  return (
    <Card className="p-4 sm:p-7 w-full max-w-xl">
      <h2 className="font-display text-xl font-semibold text-navy-800">
        Subscriber enrollment unavailable
      </h2>
      <div className="mt-4">
        <Alert tone="amber">
          A tenant-scoped FastAPI plan and enrollment contract is not available. No subscriber,
          membership, payment link, or payment status was created. Enrollment is disabled until
          ownership, authorization, identity-linking, and payment terms are approved.
        </Alert>
      </div>
    </Card>
  );
}
