# Sahayak Partners — Employee & Referral Portal

A Next.js (App Router + TypeScript + Tailwind) app for managing employees, referral-based
subscriptions, and commissions. Data is stored in **MongoDB** — every collection, its indexes,
and initial seed data (demo admin accounts + the three packages) are created automatically the
first time the app connects. There's nothing to set up by hand in MongoDB itself.

> **Legacy prototype warning:** this document still describes Mongo-backed prototype flows.
> Direct `/api/*` handlers remain in the app and are not made secure by the FastAPI sign-in or
> proxy. Do not use the demo credentials, unsigned `ep_session`, employee OTP, or legacy business
> routes as trusted authentication or authorization in a shared or production deployment.

## Getting started

1. Have a MongoDB instance ready — either install MongoDB locally, run it in Docker
   (`docker run -d -p 27017:27017 mongo`), or create a free cluster on
   [MongoDB Atlas](https://www.mongodb.com/atlas).
2. Copy `.env.example` to `.env.local` and set `MONGODB_URI` to your connection string:
   ```bash
   cp .env.example .env.local
   ```
3. Install dependencies and run:
   ```bash
   npm install
   npm run dev
   ```
4. Open http://localhost:3000. On the very first request, the app connects to MongoDB and:
   - Creates all 7 collections (`admins`, `employees`, `users`, `packages`, `supportTickets`,
     `activityLog`, `otps`) if they don't already exist.
   - Creates the indexes each collection needs (unique reference IDs, unique mobile numbers,
     unique payment tokens, a TTL index that auto-expires OTP documents, etc.) — see
     `src/lib/mongodb.ts` for the exact list.
   - Seeds the two demo admin accounts and the three packages, only if those collections are
     still empty.

If MongoDB isn't reachable, API calls fail fast (within ~8 seconds) with a clear error message
telling you to check `MONGODB_URI`, rather than hanging.

## FastAPI community sign-in (local development only)

The `/login` page provides a community-member OTP form only when Next.js is running in
development mode. The form calls FastAPI through the explicit same-origin proxy routes for OTP
request/verify, `/me`, `/context`, and logout. Other FastAPI paths remain unavailable. The
community session is HttpOnly and scoped to `/backend-api/api/v1/auth`; logout sends the
session-bound CSRF token to FastAPI so the server-side session is revoked.

To use the local static OTP, configure the backend `.env` explicitly:

```dotenv
ENVIRONMENT=development
FRONTEND_ORIGINS=http://localhost:3000
OTP_CHALLENGE_TTL_SECONDS=300
DEVELOPMENT_STATIC_OTP_ENABLED=true
DEVELOPMENT_STATIC_OTP_CODE=<private local 6-12 digit value>
```

Set a private code locally; do not commit it. If Next.js reaches FastAPI from a loopback address,
add only that address to `TRUSTED_PROXY_CIDRS` so the backend can use the browser host for trusted
tenant-domain resolution. Browse to the already-registered tenant host ending in `.localhost`
(for example, `http://<registered-tenant>.localhost:3000`) and configure that exact origin in
`FRONTEND_ORIGINS`. Start Next.js from `frontend\` with
`npm run dev -- --hostname 127.0.0.1`; start FastAPI from `backend\` with
`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload`. The backend must use an
already-migrated isolated development database containing the approved existing tenant and active
membership. Authentication succeeds only for an existing active membership under the tenant
resolved by FastAPI. No user, membership, tenant, or role is created or granted.
The code is shared by all local developers and permits impersonation of any active member whose
mobile number is known, so use only with isolated, non-production test data. Configuration is
rejected outside `ENVIRONMENT=development`, and the proxy requires a loopback browser host and
local same-origin request.

The context may report effective backend permissions, but product-role and employee authority
currently remain unavailable. Admin, Agent, and Super Admin navigation and protected screens
therefore remain blocked. This sign-in integration does not make the legacy Mongo-backed `/api/*`
routes safe or replace their authorization.

## Legacy demo logins (not trusted FastAPI authentication)

| Legacy demo role | Username | Password |
|---|---|---|
| Super Admin | `superadmin` | `super@123` |
| Admin | `admin` | `admin@123` |

These prototype credentials are not accepted by the FastAPI authentication boundary. Do not use
them for shared or production access; the legacy login handler and plaintext password storage
require replacement before those routes can be used safely.

## Legacy employee login (not trusted FastAPI authentication)

1. An admin must first add the employee (Admin → Add employee) with their mobile number.
2. The legacy **Employee Login** route accepts an unsigned employee-cookie flow and is not
   connected to the trusted FastAPI community membership model.
3. Its demo OTP behavior is not a safe authentication contract and must not be used for trusted
   access. Use the separate local development community sign-in described above only with an
   existing active FastAPI membership.

## What's included

- **Admin dashboard** — legacy overview stats, employee list & search; employee provisioning is
  currently unavailable because the direct handler relied on unsigned `ep_session` and wrote
  identity documents to public local storage. The historical add-employee form (with
  Aadhar/PAN numbers + document photo upload, plus unlimited custom fields), employee detail
  page (documents, active/inactive toggle with a required reason + confirmation popup, users
  referred), support ticket inbox.
- **Super Admin dashboard** — everything Admin has, plus a platform-wide activity log of every
  login, employee change, and subscription.
- **Employee portal** — currently unavailable. The employee OTP routes returned login codes to
  the caller and issued unsigned sessions, so the legacy sign-in and portal access are disabled
  pending a trusted server-side employee identity and authorization contract. The historical
  prototype behavior described below is not currently a supported workflow:
  - **New user** — a form (name, mobile, email, address, package, billing cycle) with a
    consent checkbox, where the employee also picks how this particular subscription should be
    handled: **Send a payment
    link** (a unique link is generated for that user alone to share themselves), or **Notify to
    confirm & pay** (the user is sent a notification asking them to confirm and pay). Existing
    customer enrollment is unavailable until a verified payment/legacy reconciliation contract
    exists. Submitting
    generates a per-user link at `/pay/[token]` that skips straight to a payment screen.
  - **Existing users** — every user the employee has added, with legacy subscription type and
    payment status displayed. Payment status cannot be changed through this UI until a verified
    settlement flow is implemented.
  - Commission auto-upgrades from 10% to 12% after 100 paid referrals.
  - A Support/Help tab that logs tickets for the admin (2–3 working day SLA messaging) plus a
    WhatsApp shortcut.
- **Per-user payment page** (`/pay/[token]`) — lookup is disabled because the legacy bearer-token
  endpoint disclosed subscriber and employee details without authentication or tenant scope.
  Checkout and settlement remain unavailable until approved payment contracts are implemented.
  Legacy `paid` status is not provider verification.

## Data & limits

- All data lives in MongoDB (see "Getting started" above for how collections/indexes/seed data
  get created automatically).
- The legacy employee endpoint writes Aadhar/PAN images to `public/uploads/`, which is public local
  disk storage, not approved private object storage. Do not use that route for real PII or production.
- The employee list is capped at 25 (`EMPLOYEE_LIMIT` in `src/lib/repo.ts`) per the current
  brief — raise this constant any time to scale up.
- Reference IDs are permanent, 8-character, alphanumeric (uppercase letters + digits, with
  visually-confusing characters like `0/O` and `1/I` excluded), generated at creation time and
  checked for uniqueness against MongoDB.

## Moving to production

This prototype is intentionally self-contained so it's easy to run and review. Before shipping:

- Hash admin passwords; consider a proper auth provider.
- Connect and verify an approved SMS/OTP provider. Implement an approved payment-provider checkout,
  signed settlement callback, replay/idempotency handling, and reconciliation before enabling
  payment status changes.
- Wire the "Notify to confirm & pay" subscription type up to a real SMS/email/WhatsApp send —
  right now it just timestamps `notifiedAt` to simulate the send.
- Store uploaded documents in secure cloud storage (S3, GCS) rather than the local filesystem.
- Move the 25-employee cap into a configurable plan/setting if it needs to vary per admin.
- Use a production-grade MongoDB deployment (replica set on Atlas or self-hosted) with proper
  backups and access control (a dedicated database user rather than an admin connection string).
