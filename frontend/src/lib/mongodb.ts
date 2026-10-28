import { MongoClient, type Db } from "mongodb";

declare global {
  // eslint-disable-next-line no-var
  var _mongoClientPromise: Promise<MongoClient> | undefined;
  // eslint-disable-next-line no-var
  var _mongoSeeded: Promise<void> | undefined;
}

const connectOptions = {
  // Fail fast with a clear error instead of hanging for the ~30s default
  // when the database is unreachable (e.g. wrong URI, DB not started yet).
  serverSelectionTimeoutMS: 8000,
};

function connect(uri: string): Promise<MongoClient> {
  const client = new MongoClient(uri, connectOptions);
  return client.connect();
}

// Module-level cache so the connection is reused across requests within the
// same process. In dev, Next.js hot-reloads this module on every file save,
// so we additionally cache on `global`, which survives the reload.
let moduleClientPromise: Promise<MongoClient> | undefined;

function getClientPromise(uri: string): Promise<MongoClient> {
  if (process.env.NODE_ENV === "development") {
    if (!global._mongoClientPromise) {
      global._mongoClientPromise = connect(uri);
    }
    return global._mongoClientPromise;
  }
  if (!moduleClientPromise) {
    moduleClientPromise = connect(uri);
  }
  return moduleClientPromise;
}

/**
 * Returns a ready-to-use Db handle. Collections are created automatically
 * (MongoDB creates a collection the first time it's written to, and we force
 * creation up front via createIndex), and seed data is inserted exactly once
 * per server process the first time this is called.
 *
 * If MongoDB was unreachable on a previous call, the failed connection isn't
 * cached forever — the next call retries from scratch.
 */
export async function getDb(): Promise<Db> {
  const uri = process.env.MONGODB_URI?.trim();
  if (!uri) {
    throw new Error("MongoDB is not configured.");
  }

  let client: MongoClient;
  try {
    client = await getClientPromise(uri);
  } catch {
    if (process.env.NODE_ENV === "development") {
      global._mongoClientPromise = undefined;
    } else {
      moduleClientPromise = undefined;
    }
    global._mongoSeeded = undefined;
    throw new Error(
      "Could not connect to MongoDB. Check runtime configuration and database availability."
    );
  }

  const dbName = process.env.MONGODB_DB?.trim() || "sahayak_partners";
  const db = client.db(dbName);

  if (!global._mongoSeeded) {
    global._mongoSeeded = ensureCollections(db).catch((err) => {
      global._mongoSeeded = undefined;
      throw err;
    });
  }
  await global._mongoSeeded;

  return db;
}

export const COLLECTIONS = {
  admins: "admins",
  employees: "employees",
  users: "users",
  packages: "packages",
  supportTickets: "supportTickets",
  activityLog: "activityLog",
  otps: "otps",
} as const;

async function ensureCollections(db: Db) {
  const existing = new Set((await db.listCollections().toArray()).map((c) => c.name));

  // Creating each collection explicitly (rather than relying on first-insert)
  // means the full schema exists in MongoDB immediately, even before any data
  // is added through the app.
  for (const name of Object.values(COLLECTIONS)) {
    if (!existing.has(name)) {
      await db.createCollection(name);
    }
  }

  // Indexes — also implicitly guarantee the collections exist.
  await db.collection(COLLECTIONS.admins).createIndex({ username: 1 }, { unique: true });
  await db.collection(COLLECTIONS.employees).createIndex({ refId: 1 }, { unique: true });
  await db.collection(COLLECTIONS.employees).createIndex({ mobile: 1 }, { unique: true });
  await db.collection(COLLECTIONS.users).createIndex({ payToken: 1 }, { unique: true });
  await db.collection(COLLECTIONS.users).createIndex({ employeeRefId: 1 });
  await db.collection(COLLECTIONS.packages).createIndex({ id: 1 }, { unique: true });
  await db.collection(COLLECTIONS.supportTickets).createIndex({ employeeRefId: 1 });
  await db.collection(COLLECTIONS.activityLog).createIndex({ timestamp: -1 });
  await db.collection(COLLECTIONS.otps).createIndex({ mobile: 1 }, { unique: true });
  // TTL index: MongoDB automatically deletes OTP documents once `expiresAt` passes.
  await db.collection(COLLECTIONS.otps).createIndex({ expiresAt: 1 }, { expireAfterSeconds: 0 });

  await seedAdmins(db);
  await seedPackages(db);
}

async function seedAdmins(db: Db) {
  const admins = db.collection(COLLECTIONS.admins);
  const count = await admins.countDocuments();
  if (count > 0) return;

  await admins.insertMany([
    {
      id: "sa-1",
      username: "superadmin",
      password: "super@123",
      role: "superadmin",
      name: "Super Admin",
    },
    {
      id: "ad-1",
      username: "admin",
      password: "admin@123",
      role: "admin",
      name: "Admin",
    },
  ]);
}

async function seedPackages(db: Db) {
  const packages = db.collection(COLLECTIONS.packages);
  const count = await packages.countDocuments();
  if (count > 0) return;

  await packages.insertMany([
    {
      id: "pkg-100",
      name: "Starter",
      price: 100,
      tier: "bronze",
      tagline: "A light plan to get new members started",
      benefits: [
        "Access to core content library",
        "1 device at a time",
        "Standard definition streaming",
        "Email support within 48 hours",
      ],
      annualDiscountPercent: 15,
    },
    {
      id: "pkg-200",
      name: "Plus",
      price: 200,
      tier: "silver",
      tagline: "Our most popular plan for everyday use",
      benefits: [
        "Everything in Starter",
        "2 devices at a time",
        "High definition streaming",
        "Priority email & chat support",
        "Early access to new features",
      ],
      annualDiscountPercent: 18,
    },
    {
      id: "pkg-300",
      name: "Premium",
      price: 300,
      tier: "gold",
      tagline: "The full experience for power users",
      benefits: [
        "Everything in Plus",
        "4 devices at a time",
        "Ultra HD streaming",
        "24/7 priority support",
        "Exclusive premium-only content",
        "Free add-on credits every month",
      ],
      annualDiscountPercent: 20,
    },
  ]);
}
