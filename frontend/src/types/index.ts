export type Role = "superadmin" | "admin";

export interface AdminUser {
  id: string;
  username: string;
  password: string; // plaintext for demo only
  role: Role;
  name: string;
}

export type EmployeeStatus = "active" | "inactive";

export interface ExtraDetail {
  label: string;
  value: string;
}

export interface Employee {
  id: string;
  refId: string; // permanent 8-char alnum
  firstName: string;
  lastName: string;
  mobile: string;
  email: string;
  address: string;
  aadharNumber: string;
  aadharImage: string; // path
  panNumber: string;
  panImage: string; // path
  extraDetails: ExtraDetail[];
  status: EmployeeStatus;
  deactivationReason?: string;
  createdAt: string;
  createdBy: string; // admin id
  commissionRate: number; // 10 or 12
}

export interface PackageBenefit {
  text: string;
}

export interface PackagePlan {
  id: string;
  name: string;
  price: number; // monthly base price in Rs
  tier: "bronze" | "silver" | "gold";
  tagline: string;
  benefits: string[];
  annualDiscountPercent: number;
}

export type BillingCycle = "monthly" | "annually";
export type PaymentStatus = "paid" | "pending" | "failed";

/**
 * How this particular user's subscription/payment is being handled.
 * - existing: an already-paying customer the employee is logging on the books; marked paid right away.
 * - payment_link: the employee generates a unique link and shares it with the user themselves.
 * - notify_confirm: the system sends the user a notification asking them to confirm & pay.
 * The employee can change this at any time from the Existing Users tab.
 */
export type SubscriptionType = "existing" | "payment_link" | "notify_confirm";

export interface SubscribedUser {
  id: string;
  name: string;
  contact: string;
  email?: string;
  address?: string;
  employeeRefId: string;
  packageId: string;
  billingCycle: BillingCycle;
  amount: number;
  paymentStatus: PaymentStatus;
  subscriptionType: SubscriptionType;
  consentAccepted: boolean;
  payToken: string;
  notifiedAt?: string;
  createdAt: string;
}

export type TicketStatus = "open" | "resolved";

export interface SupportTicket {
  id: string;
  employeeRefId: string;
  employeeName: string;
  message: string;
  status: TicketStatus;
  createdAt: string;
  resolvedAt?: string;
}

export interface ActivityLogEntry {
  id: string;
  actor: string; // name/role
  action: string;
  details: string;
  timestamp: string;
}

export interface OtpEntry {
  mobile: string;
  otp: string;
  expiresAt: number;
}

export interface DB {
  admins: AdminUser[];
  employees: Employee[];
  users: SubscribedUser[];
  packages: PackagePlan[];
  supportTickets: SupportTicket[];
  activityLog: ActivityLogEntry[];
  otps: OtpEntry[];
}
