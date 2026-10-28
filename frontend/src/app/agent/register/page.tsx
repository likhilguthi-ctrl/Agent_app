"use client";

import React, { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/AuthGuard";
import { Card, Button, Input, RefChip, Badge, Alert, Spinner } from "@/components/ui";
import RegistrationPhotoCapture from "@/components/RegistrationPhotoCapture";
import IndianMobileInput, { extractTenDigits, isValidIndianMobile } from "@/components/IndianMobileInput";
import WhatsAppVerificationSection from "@/components/WhatsAppVerificationSection";
import EmailVerificationSection from "@/components/EmailVerificationSection";
import { uploadRegistrationImage } from "@/lib/storage";
import { fetchBackendApi } from "@/lib/backend-api";
import { normalizeApiError } from "@/lib/api-error";
import {
  requestOtpNotification,
  verifyOtpNotification,
} from "@/lib/notifications";

interface PolicyInfo {
  tenant_id: string;
  community_slug: string;
  photo_requirement: "OPTIONAL" | "REQUIRED";
  default_commission_rate: number;
  policy_version: number;
}

interface SubscriptionPlan {
  id: string;
  code: string;
  name: string;
  description: string | null;
  benefits: string[] | Record<string, unknown> | null;
  duration_days: number;
  nominal_price: string | number | null;
  currency: string;
  status: "active" | "inactive";
}

interface RegisteredSubscriber {
  id: string;
  tenant_id: string;
  community_slug: string;
  membership_id: string;
  user_id: string;
  full_name: string;
  mobile_e164: string;
  masked_mobile?: string | null;
  whatsapp_number?: string | null;
  whatsapp_verified: boolean;
  whatsapp_consent: boolean;
  email?: string | null;
  email_verified: boolean;
  address?: string | null;
  photo_key?: string | null;
  package_plan?: string | null;
  package_type?: string | null;
  status: string;
  payment_status?: string | null;
  subscription_status?: string | null;
  payment_amount?: number | string | null;
  payment_order_id?: string | null;
  payment_page_url?: string | null;
  external_payment_id?: string | null;
  created_at: string;
}

export default function AgentSubscriberRegistrationPage() {
  const router = useRouter();
  const { identity } = useAuth();

  const [policy, setPolicy] = useState<PolicyInfo | null>(null);
  const [loadingPolicy, setLoadingPolicy] = useState(true);

  // Available Subscription Plans
  const [plans, setPlans] = useState<SubscriptionPlan[]>([]);
  const [loadingPlans, setLoadingPlans] = useState(true);
  const [selectedPlanId, setSelectedPlanId] = useState<string>("");
  const [isCustomAmount, setIsCustomAmount] = useState<boolean>(false);
  const [customAmount, setCustomAmount] = useState<string>("");

  // Form Fields (Sequential 1 to 8)
  // 1. Subscriber Name
  const [fullName, setFullName] = useState("");

  // 2. Mobile + OTP
  const [mobile, setMobile] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [otpCode, setOtpCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [cooldown, setCooldown] = useState(0);
  const [sendingOtp, setSendingOtp] = useState(false);
  const [verifyingOtp, setVerifyingOtp] = useState(false);
  const [isMobileVerified, setIsMobileVerified] = useState(false);
  const [verificationToken, setVerificationToken] = useState<string | null>(null);
  const [otpFeedback, setOtpFeedback] = useState<string | null>(null);
  const [otpError, setOtpError] = useState<string | null>(null);

  // 3. Email (optional)
  const [email, setEmail] = useState("");
  const [emailVerified, setEmailVerified] = useState<boolean>(false);
  const [emailVerificationToken, setEmailVerificationToken] = useState<string | undefined>(undefined);

  // WhatsApp verification details
  const [whatsappSameAsMobile, setWhatsappSameAsMobile] = useState<boolean>(true);
  const [whatsappNumber, setWhatsappNumber] = useState<string>("");
  const [whatsappVerified, setWhatsappVerified] = useState<boolean>(true);
  const [whatsappVerificationToken, setWhatsappVerificationToken] = useState<string | undefined>(undefined);

  // 4. WhatsApp Consent Checkbox (Strict)
  const [whatsappConsent, setWhatsappConsent] = useState<boolean>(true);

  // 5. Subscriber Photo (R2)
  const [photoFile, setPhotoFile] = useState<File | null>(null);

  // Physical Address (optional)
  const [address, setAddress] = useState("");

  // Status & Success state
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [createdSubscriber, setCreatedSubscriber] = useState<RegisteredSubscriber | null>(null);

  // Load Community Policy
  useEffect(() => {
    fetchBackendApi("api/v1/agent/policy", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("Failed to load community policy"))))
      .then((data: PolicyInfo) => {
        setPolicy(data);
      })
      .catch(() => {
        setFormError("Could not retrieve community registration policy.");
      })
      .finally(() => setLoadingPolicy(false));
  }, []);

  // Load Active Subscription Plans
  useEffect(() => {
    fetchBackendApi("api/v1/subscription-plans", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("Failed to load plans"))))
      .then((data: { items: SubscriptionPlan[] }) => {
        const activeOnly = (data.items || []).filter((p) => p.status === "active");
        setPlans(activeOnly);
        if (activeOnly.length > 0) {
          setSelectedPlanId(activeOnly[0].id);
        }
      })
      .catch(() => {
        // Fallback default
        setPlans([]);
      })
      .finally(() => setLoadingPlans(false));
  }, []);

  // Cooldown timer
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((c) => (c > 1 ? c - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  const handleSendOtp = useCallback(async () => {
    const tenDigits = extractTenDigits(mobile);
    if (!isValidIndianMobile(tenDigits)) {
      setOtpError("Please enter a valid 10-digit Indian mobile number starting with 6-9.");
      return;
    }
    const cleanNumber = `+91${tenDigits}`;

    setSendingOtp(true);
    setOtpError(null);
    setOtpFeedback(null);

    try {
      const res = await requestOtpNotification({
        mobileNumber: cleanNumber,
        channel: "SMS",
        purpose: "user_registration",
        communityId: identity?.tenant_id || undefined,
      });

      setChallengeId(res.challengeId);
      setOtpSent(true);
      setCooldown(res.cooldownSeconds || 60);
      setOtpFeedback(`OTP sent via SMS to ${cleanNumber}. Please ask the subscriber for the code.`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to send OTP. Please try again.";
      setOtpError(msg);
    } finally {
      setSendingOtp(false);
    }
  }, [mobile, identity?.tenant_id]);

  const handleVerifyOtp = useCallback(async () => {
    const tenDigits = extractTenDigits(mobile);
    const cleanNumber = `+91${tenDigits}`;

    if (!otpCode || otpCode.trim().length < 6) {
      setOtpError("Please enter the complete 6-digit OTP code.");
      return;
    }

    setVerifyingOtp(true);
    setOtpError(null);

    try {
      if (!challengeId) {
        setOtpError("No active OTP challenge found. Please request a new OTP.");
        return;
      }
      const res = await verifyOtpNotification({
        mobileNumber: cleanNumber,
        otp: otpCode.trim(),
        challengeId: challengeId,
        purpose: "user_registration",
        communityId: identity?.tenant_id || undefined,
      });

      if (!res.success || !res.verificationToken) {
        setOtpError("Invalid or expired OTP code.");
        return;
      }

      setVerificationToken(res.verificationToken);
      setIsMobileVerified(true);
      setOtpFeedback("Mobile number verified successfully!");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Invalid or expired OTP code.";
      setOtpError(msg);
    } finally {
      setVerifyingOtp(false);
    }
  }, [mobile, otpCode, challengeId, identity?.tenant_id]);

  function resetForm() {
    setFullName("");
    setMobile("");
    setWhatsappSameAsMobile(true);
    setWhatsappNumber("");
    setWhatsappVerified(true);
    setWhatsappVerificationToken(undefined);
    setWhatsappConsent(true);
    setEmailVerified(false);
    setEmailVerificationToken(undefined);
    setEmail("");
    setAddress("");
    setPhotoFile(null);
    setFormError(null);
    setUploadStatus(null);
    setCreatedSubscriber(null);
    setIsMobileVerified(false);
    setVerificationToken(null);
    setOtpSent(false);
    setOtpCode("");
    setChallengeId("");
    setIsCustomAmount(false);
    setCustomAmount("");
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);

    if (!fullName.trim()) {
      setFormError("Subscriber full name is required.");
      return;
    }
    const tenDigits = extractTenDigits(mobile);
    if (!isValidIndianMobile(tenDigits)) {
      setFormError("Valid 10-digit Indian mobile number is required (starting with 6-9).");
      return;
    }
    const cleanMobile = `+91${tenDigits}`;
    if (!isMobileVerified || !verificationToken) {
      setFormError("Subscriber mobile number must be verified via OTP before registration.");
      return;
    }

    if (!whatsappSameAsMobile && !whatsappVerified) {
      setFormError("Alternative WhatsApp number must be verified via OTP.");
      return;
    }

    if (email.trim().length > 0 && (!emailVerified || !emailVerificationToken)) {
      setFormError("Email address must be verified via email OTP when provided.");
      return;
    }

    const isPhotoRequired = policy?.photo_requirement === "REQUIRED";
    if (isPhotoRequired && !photoFile) {
      setFormError("Community policy requires a subscriber photo. Please take or upload a photo to continue.");
      return;
    }

    if (isCustomAmount) {
      const parsed = parseFloat(customAmount);
      if (isNaN(parsed) || parsed <= 0) {
        setFormError("Please enter a valid custom amount greater than 0.");
        return;
      }
    } else if (!selectedPlanId && plans.length > 0) {
      setFormError("Please select a subscription package.");
      return;
    }

    const csrfToken = identity?.csrf_token || "";
    setIsSubmitting(true);

    try {
      let photoKey: string | null = null;
      if (photoFile) {
        setUploadStatus("Uploading subscriber photo to secure storage…");
        const uploadRes = await uploadRegistrationImage(
          photoFile,
          "user-photo",
          csrfToken,
          verificationToken
        );
        photoKey = uploadRes.object_key;
      }

      setUploadStatus("Registering subscriber & initiating payment…");
      const selectedPlan = plans.find((p) => p.id === selectedPlanId);

      const callbackUrl = `${window.location.origin}/agent/payment/callback`;

      const payload = {
        full_name: fullName.trim(),
        mobile_e164: cleanMobile,
        verification_token: verificationToken,
        whatsapp_same_as_mobile: whatsappSameAsMobile,
        whatsapp_number: whatsappSameAsMobile ? cleanMobile : whatsappNumber,
        whatsapp_verification_token: whatsappSameAsMobile ? null : (whatsappVerificationToken || null),
        whatsapp_consent: whatsappConsent,
        email: email.trim() || null,
        email_verification_token: email.trim() ? emailVerificationToken : null,
        address: address.trim() || null,
        photo_key: photoKey,
        package_plan: isCustomAmount ? `Custom (Rs. ${customAmount})` : selectedPlan?.name || "Standard",
        plan_id: isCustomAmount ? null : selectedPlanId || null,
        custom_amount: isCustomAmount ? parseFloat(customAmount) : null,
        initiate_payment: true,
        callback_url: callbackUrl,
      };

      const res = await fetchBackendApi("api/v1/agent/users", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          Origin: window.location.origin,
        },
        body: JSON.stringify(payload),
        credentials: "same-origin",
      });

      if (!res.ok) {
        let msg = `Subscriber registration failed (HTTP ${res.status})`;
        try {
          const json = await res.json();
          msg = normalizeApiError(json, msg);
        } catch {}
        setFormError(msg);
        setIsSubmitting(false);
        setUploadStatus(null);
        return;
      }

      const subscriberData: RegisteredSubscriber = await res.json();
      setCreatedSubscriber(subscriberData);
      setIsSubmitting(false);
      setUploadStatus(null);

      // If payment_page_url is available, present option or open hosted checkout
      if (subscriberData.payment_page_url) {
        window.open(subscriberData.payment_page_url, "_blank");
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Registration error occurred.";
      setFormError(msg);
      setIsSubmitting(false);
      setUploadStatus(null);
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      {/* Breadcrumb Navigation */}
      <nav className="flex items-center gap-2 text-xs font-medium text-slate-500">
        <Link href="/agent/dashboard" className="hover:text-navy-800 transition">
          Dashboard
        </Link>
        <span>/</span>
        <span className="text-slate-800 font-semibold">Subscriber Registration & Payment</span>
      </nav>

      {/* Header */}
      <div>
        <h1 className="font-display text-2xl font-bold text-navy-900 tracking-tight">
          Subscriber Registration & Payment
        </h1>
        <p className="text-sm text-slate-500 mt-1">
          Agent-led subscriber acquisition with package selection, WhatsApp consent, and Payment Microservice checkout.
        </p>
      </div>

      {formError && <Alert tone="red">{formError}</Alert>}

      {/* Success State */}
      {createdSubscriber ? (
        <Card className="p-8 border-emerald-200 bg-emerald-50/20 shadow-sm space-y-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 font-bold text-xl">
              ✓
            </div>
            <div>
              <h2 className="text-xl font-bold text-slate-800">Subscriber Registered & Order Created!</h2>
              <p className="text-sm text-slate-500">
                Payment session initiated with the Payment Microservice. Complete payment to activate OTT subscription.
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 bg-white p-5 rounded-xl border border-slate-200 text-sm">
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold mb-1">Subscriber ID</p>
              <span className="font-mono text-xs text-slate-700">{createdSubscriber.id.slice(0, 13)}…</span>
            </div>
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold mb-1">Payment Status</p>
              <Badge tone={createdSubscriber.payment_status === "paid" ? "emerald" : "amber"}>
                {(createdSubscriber.payment_status || "PENDING").toUpperCase()}
              </Badge>
            </div>
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold">Subscriber Name</p>
              <p className="font-medium text-slate-800">{createdSubscriber.full_name}</p>
            </div>
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold">Mobile</p>
              <p className="font-medium text-slate-800 flex items-center gap-1.5">
                <span>{createdSubscriber.masked_mobile || createdSubscriber.mobile_e164}</span>
                <span className="text-xs text-emerald-600 font-semibold">✓ Verified</span>
              </p>
            </div>
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold">Package / Amount</p>
              <p className="font-medium text-slate-800">
                {createdSubscriber.package_plan} (₹{createdSubscriber.payment_amount || "0.00"})
              </p>
            </div>
            <div>
              <p className="text-xs uppercase text-slate-400 font-semibold">WhatsApp Consent</p>
              <p className="font-medium text-slate-800">
                {createdSubscriber.whatsapp_consent ? "✓ Opted in for updates" : "Opted out"}
              </p>
            </div>
          </div>

          {/* Hosted Payment Checkout CTA */}
          {createdSubscriber.payment_page_url && (
            <div className="p-4 rounded-xl bg-navy-900 text-white flex flex-col sm:flex-row items-center justify-between gap-4">
              <div>
                <p className="font-semibold text-sm">Hosted Checkout Session Ready</p>
                <p className="text-xs text-slate-300">
                  Open hosted payment screen to accept subscriber payment securely.
                </p>
              </div>
              <div className="flex gap-2">
                <a
                  href={createdSubscriber.payment_page_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="px-4 py-2 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-navy-950 font-bold text-xs transition"
                >
                  Open Checkout Page ↗
                </a>
                <Link
                  href={`/agent/payment/callback?order_id=${createdSubscriber.payment_order_id}&status=COMPLETED`}
                  className="px-3 py-2 rounded-lg bg-navy-800 hover:bg-navy-700 text-slate-200 text-xs font-medium border border-navy-700 transition"
                >
                  Verify Status
                </Link>
              </div>
            </div>
          )}

          <div className="flex flex-wrap gap-3 pt-2">
            <Button onClick={resetForm}>+ Register Another Subscriber</Button>
            <Link href="/agent/dashboard">
              <Button variant="secondary">Back to Dashboard</Button>
            </Link>
          </div>
        </Card>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Policy Information Card */}
          <Card className="p-5 border-slate-200 bg-white space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                Community Registration Policy
              </span>
              {loadingPolicy ? (
                <span className="text-xs text-slate-400">Loading policy…</span>
              ) : policy?.photo_requirement === "REQUIRED" ? (
                <Badge tone="amber">Photo Mandatory</Badge>
              ) : (
                <Badge tone="slate">Photo Optional</Badge>
              )}
            </div>
            <p className="text-xs text-slate-600">
              Operating Community: <strong className="text-slate-800 font-mono">{identity.tenant?.name || identity.tenant?.slug || "..."}</strong>.
              All subscriber acquisitions will be attributed to your authenticated agent account.
            </p>
          </Card>

          {/* 1. Subscriber Name */}
          <Card className="p-6 space-y-4">
            <h2 className="font-semibold text-slate-800 text-base">1. Subscriber Information</h2>
            <div>
              <label htmlFor="sub-name" className="block text-sm font-medium text-slate-700 mb-1">
                Full Name <span className="text-red-500">*</span>
              </label>
              <Input
                id="sub-name"
                placeholder="e.g. Priya Sharma"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                required
              />
            </div>
          </Card>

          {/* 2. Indian Mobile Input & OTP Verification */}
          <Card className="p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold text-slate-800 text-base">
                2. Mobile Number & Verification <span className="text-red-500">*</span>
              </h2>
              {isMobileVerified && (
                <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700 bg-emerald-100 px-2.5 py-0.5 rounded-full">
                  ✓ Verified
                </span>
              )}
            </div>

            <div className="flex flex-col sm:flex-row gap-3">
              <div className="flex-1">
                <IndianMobileInput
                  value={mobile}
                  disabled={isMobileVerified || sendingOtp}
                  onChange={(e164) => {
                    setMobile(e164);
                    setIsMobileVerified(false);
                    setVerificationToken(null);
                    setOtpSent(false);
                    if (whatsappSameAsMobile) {
                      setWhatsappNumber(e164);
                    }
                  }}
                  required
                />
              </div>
              {!isMobileVerified && (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={sendingOtp || cooldown > 0 || !isValidIndianMobile(extractTenDigits(mobile))}
                  onClick={handleSendOtp}
                  className="self-start sm:self-center mt-1"
                >
                  {sendingOtp ? (
                    <span className="flex items-center gap-1.5">
                      <Spinner className="w-3.5 h-3.5" />
                      <span>Sending...</span>
                    </span>
                  ) : cooldown > 0 ? (
                    `Resend in ${cooldown}s`
                  ) : otpSent ? (
                    "Resend OTP"
                  ) : (
                    "Send OTP to Subscriber"
                  )}
                </Button>
              )}
            </div>

            {/* OTP Entry Box */}
            {otpSent && !isMobileVerified && (
              <div className="flex flex-col sm:flex-row items-center gap-3 pt-2">
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={6}
                  placeholder="Enter 6-digit OTP"
                  value={otpCode}
                  disabled={verifyingOtp}
                  onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ""))}
                  className="w-full sm:w-48 rounded-lg border border-slate-300 bg-white px-3 py-2 text-center font-mono text-base tracking-widest text-slate-800 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-navy-600"
                />
                <Button
                  type="button"
                  disabled={verifyingOtp || otpCode.length < 6}
                  onClick={handleVerifyOtp}
                >
                  {verifyingOtp ? (
                    <span className="flex items-center gap-1.5">
                      <Spinner className="w-3.5 h-3.5" />
                      <span>Verifying...</span>
                    </span>
                  ) : (
                    "Confirm OTP"
                  )}
                </Button>
              </div>
            )}

            {otpFeedback && !otpError && (
              <p className="text-xs text-emerald-600 font-medium">{otpFeedback}</p>
            )}
            {otpError && (
              <p className="text-xs text-red-600 font-medium">{otpError}</p>
            )}
            <p className="text-xs text-slate-400">
              In test/dev dummy mode, use OTP code: <code className="font-semibold text-slate-600">123456</code>
            </p>
          </Card>

          {/* 3. Optional Email */}
          <Card className="p-6 space-y-4">
            <h2 className="font-semibold text-slate-800 text-base">3. Email Address (Optional)</h2>
            <EmailVerificationSection
              email={email}
              isVerified={emailVerified}
              verificationToken={emailVerificationToken}
              required={false}
              communityId={identity?.tenant_id}
              onChange={(d) => {
                setEmail(d.email);
                setEmailVerified(d.isVerified);
                setEmailVerificationToken(d.verificationToken);
              }}
            />
          </Card>

          {/* 4. WhatsApp Consent Checkbox (Strict) */}
          <Card className="p-6 space-y-4">
            <h2 className="font-semibold text-slate-800 text-base">4. WhatsApp Notifications & Consent</h2>
            <WhatsAppVerificationSection
              primaryMobile={mobile}
              whatsappNumber={whatsappNumber}
              sameAsMobile={whatsappSameAsMobile}
              isVerified={whatsappVerified}
              verificationToken={whatsappVerificationToken}
              communityId={identity?.tenant_id}
              onChange={(d) => {
                setWhatsappNumber(d.whatsappNumber);
                setWhatsappSameAsMobile(d.sameAsMobile);
                setWhatsappVerified(d.isVerified);
                setWhatsappVerificationToken(d.verificationToken);
              }}
            />

            <div className="pt-3 border-t border-slate-100">
              <label className="flex items-start gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={whatsappConsent}
                  onChange={(e) => setWhatsappConsent(e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                />
                <span className="text-xs text-slate-700 leading-relaxed">
                  <strong>Subscriber Consent:</strong> The subscriber explicitly consents to receive transactional receipt, welcome messages, and renewal reminders via WhatsApp. (If unchecked, WhatsApp welcome will be skipped).
                </span>
              </label>
            </div>
          </Card>

          {/* 5. Subscriber Photo (R2) */}
          <Card className="p-6 space-y-4">
            <div>
              <h2 className="font-semibold text-slate-800 text-base">5. Subscriber Photo</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                {policy?.photo_requirement === "REQUIRED"
                  ? "A front-facing portrait is required by community policy."
                  : "A front-facing photo is optional."}
              </p>
            </div>

            <RegistrationPhotoCapture
              label="Subscriber Photo"
              description="Capture via camera or upload file."
              required={policy?.photo_requirement === "REQUIRED"}
              onPhotoChange={setPhotoFile}
            />
          </Card>

          {/* 6. Subscription Package Selection & 7. Custom Amount */}
          <Card className="p-6 space-y-4">
            <div>
              <h2 className="font-semibold text-slate-800 text-base">6 & 7. Subscription Package & Amount</h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Choose a pre-configured community subscription plan or input a custom collection amount.
              </p>
            </div>

            {loadingPlans ? (
              <p className="text-xs text-slate-400">Loading subscription plans…</p>
            ) : (
              <div className="space-y-4">
                {/* Plan selection grid */}
                {!isCustomAmount && plans.length === 0 && (
                  <div className="p-4 rounded-xl bg-amber-50 border border-amber-200 text-amber-800 text-xs">
                    <p className="font-semibold">No active subscription plans found for this community.</p>
                    <p className="mt-1 text-amber-700">
                      You can enable &quot;Enter Custom Subscription Amount&quot; below to proceed, or contact your Community Admin to add a plan.
                    </p>
                  </div>
                )}

                {!isCustomAmount && plans.length > 0 && (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {plans.map((p) => {
                      const selected = selectedPlanId === p.id;
                      return (
                        <div
                          key={p.id}
                          onClick={() => setSelectedPlanId(p.id)}
                          className={`p-4 rounded-xl border cursor-pointer transition ${
                            selected
                              ? "border-emerald-500 bg-emerald-50/30 ring-2 ring-emerald-500/20"
                              : "border-slate-200 bg-white hover:border-slate-300"
                          }`}
                        >
                          <div className="flex items-center justify-between">
                            <span className="font-bold text-slate-800 text-sm">{p.name}</span>
                            <span className="font-extrabold text-navy-900 text-base">
                              ₹{p.nominal_price || "199"}
                            </span>
                          </div>
                          <p className="text-xs text-slate-500 mt-1">
                            {p.duration_days} Days validity {p.description ? `• ${p.description}` : ""}
                          </p>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Custom Amount Toggle */}
                <div className="pt-2 border-t border-slate-100 flex items-center justify-between">
                  <label className="flex items-center gap-2 cursor-pointer text-sm font-medium text-slate-700">
                    <input
                      type="checkbox"
                      checked={isCustomAmount}
                      onChange={(e) => setIsCustomAmount(e.target.checked)}
                      className="rounded border-slate-300 text-navy-600 focus:ring-navy-500"
                    />
                    <span>Enter Custom Subscription Amount</span>
                  </label>
                  {isCustomAmount && (
                    <Badge tone="navy">Custom Amount Mode</Badge>
                  )}
                </div>

                {isCustomAmount && (
                  <div className="pt-2">
                    <label htmlFor="custom-amt" className="block text-xs font-semibold text-slate-700 uppercase mb-1">
                      Custom Amount (in INR) <span className="text-red-500">*</span>
                    </label>
                    <div className="relative rounded-lg shadow-sm max-w-xs">
                      <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
                        <span className="text-slate-500 sm:text-sm">₹</span>
                      </div>
                      <input
                        type="number"
                        id="custom-amt"
                        min="1"
                        step="0.01"
                        placeholder="e.g. 499.00"
                        value={customAmount}
                        onChange={(e) => setCustomAmount(e.target.value)}
                        className="block w-full rounded-lg border border-slate-300 pl-7 pr-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-navy-600 font-semibold"
                        required={isCustomAmount}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}
          </Card>

          {/* Physical Address (Optional) */}
          <Card className="p-6 space-y-4">
            <h2 className="font-semibold text-slate-800 text-base">Physical Address (Optional)</h2>
            <Input
              id="sub-address"
              placeholder="Flat No, Apartment, Street, City"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
          </Card>

          {/* 8. Payment Initiation Button */}
          <div className="flex flex-col sm:flex-row items-center justify-between gap-4 pt-2">
            <Link href="/agent/dashboard">
              <Button variant="secondary" type="button" disabled={isSubmitting}>
                Cancel
              </Button>
            </Link>

            <div className="flex items-center gap-3">
              {uploadStatus && (
                <span className="text-xs font-medium text-navy-700 animate-pulse">
                  {uploadStatus}
                </span>
              )}
              <Button type="submit" disabled={isSubmitting || !isMobileVerified}>
                {isSubmitting ? (
                  <span className="flex items-center gap-1.5">
                    <Spinner className="w-3.5 h-3.5" />
                    <span>Initiating Payment Session…</span>
                  </span>
                ) : (
                  "Register & Proceed to Payment →"
                )}
              </Button>
            </div>
          </div>
        </form>
      )}
    </div>
  );
}
