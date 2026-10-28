import { getDb, COLLECTIONS } from "@/lib/mongodb";
import type {
  AdminUser,
  Employee,
  SubscribedUser,
  PackagePlan,
  SupportTicket,
  ActivityLogEntry,
  ExtraDetail,
  EmployeeStatus,
  SubscriptionType,
  TicketStatus,
} from "@/types";

function strip<T>(doc: any): T {
  if (!doc) return doc;
  const { _id, ...rest } = doc;
  return rest as T;
}

export function cryptoRandomId(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

const REF_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no confusing chars (0,O,1,I)

export async function generateUniqueRefId(): Promise<string> {
  const db = await getDb();
  const employees = db.collection(COLLECTIONS.employees);
  let refId = "";
  // Practically always succeeds on the first try (32^8 possibilities); loop
  // guards against the astronomically unlikely collision.
  for (let attempt = 0; attempt < 20; attempt++) {
    refId = Array.from({ length: 8 })
      .map(() => REF_CHARS[Math.floor(Math.random() * REF_CHARS.length)])
      .join("");
    const exists = await employees.findOne({ refId });
    if (!exists) return refId;
  }
  return refId;
}

const TOKEN_CHARS = "abcdefghjkmnpqrstuvwxyz23456789";

export async function generatePayToken(): Promise<string> {
  const db = await getDb();
  const users = db.collection(COLLECTIONS.users);
  for (let attempt = 0; attempt < 20; attempt++) {
    const token = Array.from({ length: 14 })
      .map(() => TOKEN_CHARS[Math.floor(Math.random() * TOKEN_CHARS.length)])
      .join("");
    const exists = await users.findOne({ payToken: token });
    if (!exists) return token;
  }
  return cryptoRandomId();
}

export function computeCommissionRate(paidUserCount: number): number {
  return paidUserCount >= 100 ? 12 : 10;
}

// ---------- Admins ----------

export async function getAdminByCredentials(username: string, password: string): Promise<AdminUser | null> {
  const db = await getDb();
  const doc = await db
    .collection(COLLECTIONS.admins)
    .findOne({ username: new RegExp(`^${escapeRegex(username)}$`, "i"), password });
  return strip<AdminUser>(doc);
}

function escapeRegex(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// ---------- Employees ----------

export const EMPLOYEE_LIMIT = 25;

export async function countEmployees(): Promise<number> {
  const db = await getDb();
  return db.collection(COLLECTIONS.employees).countDocuments();
}

export async function getAllEmployees(): Promise<Employee[]> {
  const db = await getDb();
  const docs = await db.collection(COLLECTIONS.employees).find().sort({ createdAt: -1 }).toArray();
  return docs.map((d) => strip<Employee>(d));
}

export async function getEmployeeById(id: string): Promise<Employee | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.employees).findOne({ id });
  return strip<Employee>(doc);
}

export async function getEmployeeByRefId(refId: string): Promise<Employee | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.employees).findOne({ refId });
  return strip<Employee>(doc);
}

export async function getEmployeeByMobile(mobile: string): Promise<Employee | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.employees).findOne({ mobile });
  return strip<Employee>(doc);
}

export interface NewEmployeeInput {
  firstName: string;
  lastName: string;
  mobile: string;
  email: string;
  address: string;
  aadharNumber: string;
  aadharImage: string;
  panNumber: string;
  panImage: string;
  extraDetails: ExtraDetail[];
  createdBy: string;
}

export async function insertEmployee(input: NewEmployeeInput): Promise<Employee> {
  const db = await getDb();
  const refId = await generateUniqueRefId();
  const employee: Employee = {
    id: cryptoRandomId(),
    refId,
    ...input,
    status: "active",
    createdAt: new Date().toISOString(),
    commissionRate: 10,
  };
  await db.collection(COLLECTIONS.employees).insertOne(employee as any);
  return employee;
}

export async function setEmployeeStatus(
  id: string,
  status: EmployeeStatus,
  reason?: string
): Promise<Employee | null> {
  const db = await getDb();
  if (status === "inactive") {
    await db.collection(COLLECTIONS.employees).updateOne({ id }, { $set: { status, deactivationReason: reason } });
  } else {
    await db.collection(COLLECTIONS.employees).updateOne(
      { id },
      { $set: { status }, $unset: { deactivationReason: "" } }
    );
  }
  return getEmployeeById(id);
}

export async function recalcEmployeeCommission(refId: string): Promise<void> {
  const db = await getDb();
  const paidCount = await db
    .collection(COLLECTIONS.users)
    .countDocuments({ employeeRefId: refId, paymentStatus: "paid" });
  await db
    .collection(COLLECTIONS.employees)
    .updateOne({ refId }, { $set: { commissionRate: computeCommissionRate(paidCount) } });
}

// ---------- Subscribed users ----------

export async function getUsersByEmployeeRefId(refId: string): Promise<SubscribedUser[]> {
  const db = await getDb();
  const docs = await db.collection(COLLECTIONS.users).find({ employeeRefId: refId }).sort({ createdAt: -1 }).toArray();
  return docs.map((d) => strip<SubscribedUser>(d));
}

export async function getAllUsers(): Promise<SubscribedUser[]> {
  const db = await getDb();
  const docs = await db.collection(COLLECTIONS.users).find().sort({ createdAt: -1 }).toArray();
  return docs.map((d) => strip<SubscribedUser>(d));
}

export async function getUserById(id: string): Promise<SubscribedUser | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.users).findOne({ id });
  return strip<SubscribedUser>(doc);
}

export async function getUserByPayToken(token: string): Promise<SubscribedUser | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.users).findOne({ payToken: token });
  return strip<SubscribedUser>(doc);
}

export interface NewUserInput {
  name: string;
  contact: string;
  email?: string;
  address?: string;
  employeeRefId: string;
  packageId: string;
  billingCycle: "monthly" | "annually";
  amount: number;
  subscriptionType: SubscriptionType;
}

export async function insertUser(input: NewUserInput): Promise<SubscribedUser> {
  const db = await getDb();
  const payToken = await generatePayToken();
  const now = new Date().toISOString();
  const isExisting = input.subscriptionType === "existing";

  const user: SubscribedUser = {
    id: cryptoRandomId(),
    name: input.name,
    contact: input.contact,
    email: input.email,
    address: input.address,
    employeeRefId: input.employeeRefId,
    packageId: input.packageId,
    billingCycle: input.billingCycle,
    amount: input.amount,
    paymentStatus: isExisting ? "paid" : "pending",
    subscriptionType: input.subscriptionType,
    consentAccepted: true,
    payToken,
    notifiedAt: input.subscriptionType === "notify_confirm" ? now : undefined,
    createdAt: now,
  };
  await db.collection(COLLECTIONS.users).insertOne(user as any);
  if (isExisting) await recalcEmployeeCommission(input.employeeRefId);
  return user;
}

export interface UserUpdate {
  subscriptionType?: SubscriptionType;
  notifiedAt?: string;
}

export async function updateUser(id: string, update: UserUpdate): Promise<SubscribedUser | null> {
  const db = await getDb();
  if (Object.keys(update).length > 0) {
    await db.collection(COLLECTIONS.users).updateOne({ id }, { $set: update });
  }
  const user = await getUserById(id);
  if (user) await recalcEmployeeCommission(user.employeeRefId);
  return getUserById(id);
}

// ---------- Packages ----------

export async function getAllPackages(): Promise<PackagePlan[]> {
  const db = await getDb();
  const docs = await db.collection(COLLECTIONS.packages).find().toArray();
  return docs.map((d) => strip<PackagePlan>(d));
}

export async function getPackageById(id: string): Promise<PackagePlan | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.packages).findOne({ id });
  return strip<PackagePlan>(doc);
}

// ---------- Support tickets ----------

export async function getSupportTickets(refId?: string): Promise<SupportTicket[]> {
  const db = await getDb();
  const filter = refId ? { employeeRefId: refId } : {};
  const docs = await db.collection(COLLECTIONS.supportTickets).find(filter).sort({ createdAt: -1 }).toArray();
  return docs.map((d) => strip<SupportTicket>(d));
}

export async function insertSupportTicket(input: {
  employeeRefId: string;
  employeeName: string;
  message: string;
}): Promise<SupportTicket> {
  const db = await getDb();
  const ticket: SupportTicket = {
    id: cryptoRandomId(),
    ...input,
    status: "open",
    createdAt: new Date().toISOString(),
  };
  await db.collection(COLLECTIONS.supportTickets).insertOne(ticket as any);
  return ticket;
}

export async function getSupportTicketById(id: string): Promise<SupportTicket | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.supportTickets).findOne({ id });
  return strip<SupportTicket>(doc);
}

export async function resolveSupportTicket(id: string): Promise<SupportTicket | null> {
  const db = await getDb();
  await db
    .collection(COLLECTIONS.supportTickets)
    .updateOne({ id }, { $set: { status: "resolved" as TicketStatus, resolvedAt: new Date().toISOString() } });
  return getSupportTicketById(id);
}

// ---------- Activity log ----------

export async function logActivity(entry: { actor: string; action: string; details: string }): Promise<void> {
  const db = await getDb();
  const record: ActivityLogEntry = {
    id: cryptoRandomId(),
    timestamp: new Date().toISOString(),
    ...entry,
  };
  await db.collection(COLLECTIONS.activityLog).insertOne(record as any);
}

export async function getActivity(limit = 200): Promise<ActivityLogEntry[]> {
  const db = await getDb();
  const docs = await db.collection(COLLECTIONS.activityLog).find().sort({ timestamp: -1 }).limit(limit).toArray();
  return docs.map((d) => strip<ActivityLogEntry>(d));
}

// ---------- OTPs ----------

export async function setOtp(mobile: string, otp: string, expiresAt: Date): Promise<void> {
  const db = await getDb();
  await db.collection(COLLECTIONS.otps).updateOne({ mobile }, { $set: { mobile, otp, expiresAt } }, { upsert: true });
}

export async function getValidOtp(mobile: string): Promise<{ mobile: string; otp: string; expiresAt: Date } | null> {
  const db = await getDb();
  const doc = await db.collection(COLLECTIONS.otps).findOne({ mobile });
  if (!doc) return null;
  if (new Date(doc.expiresAt).getTime() < Date.now()) return null;
  return strip(doc);
}

export async function deleteOtp(mobile: string): Promise<void> {
  const db = await getDb();
  await db.collection(COLLECTIONS.otps).deleteOne({ mobile });
}
