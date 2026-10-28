"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  fetchBackendApi,
  getAuthenticatedIdentity,
  AuthenticatedIdentity,
} from "@/lib/backend-api";
import { Spinner } from "@/components/ui";
import IndianMobileInput from "@/components/IndianMobileInput";

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <div
        aria-hidden="true"
        className="flex h-10 w-10 items-center justify-center rounded-xl bg-navy-700 shadow-sm"
      >
        <span className="font-display text-sm font-bold text-emerald-400">CO</span>
      </div>
      <div>
        <p className="font-display text-base font-semibold text-navy-800">
          Community OTT
        </p>
        <p className="text-xs text-slate-500">Agent Platform</p>
      </div>
    </div>
  );
}

interface LoginContextItem {
  tenant_id: string | null;
  tenant_slug: string | null;
  role: string;
  username: string;
}

export default function LoginPage() {
  const router = useRouter();

  // Authentication check state
  const [checkingAuth, setCheckingAuth] = useState(true);

  // Flow step: "credentials" | "otp" | "change_password" | "select_context" | "forgot_password_mobile" | "forgot_password_otp" | "forgot_password_reset"
  const [step, setStep] = useState<
    | "credentials"
    | "otp"
    | "change_password"
    | "select_context"
    | "forgot_password_mobile"
    | "forgot_password_otp"
    | "forgot_password_reset"
  >("credentials");

  // Step 1: Credentials inputs
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // Step 2: OTP inputs
  const [otpCode, setOtpCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [maskedMobile, setMaskedMobile] = useState("");

  // Step 3: Password change inputs (enforced on first login)
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  // Context Selection state (multi-community accounts)
  const [selectionToken, setSelectionToken] = useState("");
  const [availableContexts, setAvailableContexts] = useState<LoginContextItem[]>([]);
  const [selectedContextIndex, setSelectedContextIndex] = useState(0);

  // Forgot Password state
  const [forgotMobileE164, setForgotMobileE164] = useState("");
  const [forgotTenDigits, setForgotTenDigits] = useState("");
  const [isForgotMobileValid, setIsForgotMobileValid] = useState(false);
  const [forgotChallengeId, setForgotChallengeId] = useState("");
  const [forgotMaskedMobile, setForgotMaskedMobile] = useState("");
  const [forgotOtp, setForgotOtp] = useState("");
  const [forgotResetToken, setForgotResetToken] = useState("");
  const [forgotAccounts, setForgotAccounts] = useState<
    Array<{
      account_id: string;
      username: string;
      role: string;
      account_type: string;
      community_id?: string | null;
      community_name?: string | null;
    }>
  >([]);
  const [selectedResetAccountId, setSelectedResetAccountId] = useState("");
  const [resetNewPassword, setResetNewPassword] = useState("");
  const [resetConfirmPassword, setResetConfirmPassword] = useState("");
  const [showResetNewPassword, setShowResetNewPassword] = useState(false);
  const [showResetConfirmPassword, setShowResetConfirmPassword] = useState(false);
  const [forgotCooldown, setForgotCooldown] = useState(0);

  // Timer and attempt feedback
  const [cooldown, setCooldown] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [successNotice, setSuccessNotice] = useState("");
  const [attemptsRemaining, setAttemptsRemaining] = useState<number | null>(null);

  function routeForRole(identity: AuthenticatedIdentity): string | null {
    if (identity.role === "SUPER_ADMIN") {
      return "/superadmin/dashboard";
    }
    if (identity.role === "ADMIN") {
      return "/admin/dashboard";
    }
    if (identity.role === "AGENT") {
      return "/agent/dashboard";
    }
    return null;
  }

  // Check existing session
  useEffect(() => {
    let mounted = true;
    async function checkExistingSession() {
      try {
        const id = await getAuthenticatedIdentity();
        if (mounted && id) {
          if (id.must_change_password) {
            setStep("change_password");
            setSuccessNotice("You must change your password before continuing to the dashboard.");
          } else {
            const destination = routeForRole(id);
            if (destination) {
              router.replace(destination);
              return;
            }
          }
        }
      } catch {
        // Unauthenticated
      } finally {
        if (mounted) setCheckingAuth(false);
      }
    }

    void checkExistingSession();
    return () => {
      mounted = false;
    };
  }, [router]);

  // Cooldown countdown timer
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  // Forgot Password Cooldown countdown timer
  useEffect(() => {
    if (forgotCooldown <= 0) return;
    const timer = setInterval(() => {
      setForgotCooldown((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [forgotCooldown]);

  // STEP 1: Submit Username + Password to verify credentials and trigger OTP
  async function handleCredentialsSubmit(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    const cleanUsername = username.trim().toLowerCase();
    if (!cleanUsername) {
      setErrorMessage("Please enter your username.");
      return;
    }
    if (!password) {
      setErrorMessage("Please enter your password.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          username: cleanUsername,
          password: password,
          two_factor: true,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        if (res.status === 401) {
          setErrorMessage("Invalid username or password. Please verify your credentials.");
        } else if (res.status === 429) {
          const retryMsg = errJson?.error?.message || errJson?.detail?.message;
          setErrorMessage(
            retryMsg && typeof retryMsg === "string" && retryMsg.toLowerCase().includes("wait")
              ? retryMsg
              : "Too many authentication requests. Please wait before trying again."
          );
        } else if (res.status === 502 || res.status === 503 || res.status === 504) {
          if (errJson?.error?.code === "MESSAGING_UNAVAILABLE") {
            setErrorMessage("Notification service is temporarily unavailable. Please try again later.");
          } else {
            setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
          }
        } else {
          setErrorMessage("Authentication could not be completed. Please try again.");
        }
        setSubmitting(false);
        return;
      }

      const data = await res.json();
      if (data.status === "otp_required") {
        setChallengeId(data.challenge_id || "");
        setMaskedMobile(data.masked_mobile || "");
        setCooldown(data.cooldown_seconds || 60);
        setCurrentPassword(password);
        setStep("otp");
        setSuccessNotice(`Verification code dispatched to ${data.masked_mobile}.`);
        setAttemptsRemaining(null);
      } else {
        // Direct session issued
        if (data.must_change_password) {
          setCurrentPassword(password);
          setStep("change_password");
          setSuccessNotice("You must change your password before accessing your account.");
        } else {
          await completeLoginRedirect();
        }
      }
    } catch {
      setErrorMessage("A network error occurred while verifying credentials. Please check your connection.");
    } finally {
      setSubmitting(false);
    }
  }

  // STEP 2: Verify OTP
  async function handleVerifyOtp(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    const cleanOtp = otpCode.trim();
    if (!cleanOtp) {
      setErrorMessage("Please enter the 6-digit verification code.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          username: username.trim().toLowerCase(),
          password: password,
          challenge_id: challengeId,
          otp: cleanOtp,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        if (res.status === 401) {
          const rem = errJson?.detail?.attempts_remaining;
          if (typeof rem === "number") {
            setAttemptsRemaining(rem);
            setErrorMessage(`Incorrect verification code. ${rem} attempt${rem === 1 ? "" : "s"} remaining.`);
          } else {
            setErrorMessage(errJson?.detail?.message || "Invalid verification code. Please try again.");
          }
        } else if (res.status === 429) {
          setErrorMessage("Too many failed attempts. This code has expired. Please request a new one.");
        } else if (res.status === 502 || res.status === 503 || res.status === 504) {
          if (errJson?.error?.code === "MESSAGING_UNAVAILABLE") {
            setErrorMessage("Notification service is temporarily unavailable. Please try again later.");
          } else {
            setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
          }
        } else {
          setErrorMessage(errJson?.detail?.message || "Verification failed. Please try again.");
        }
        setSubmitting(false);
        return;
      }

      const data = await res.json();

      // Check first-login password change requirement
      if (data.must_change_password) {
        setCurrentPassword(password);
        setStep("change_password");
        setSuccessNotice("First login detected. You must change your temporary password to continue.");
        setSubmitting(false);
        return;
      }

      // Check if context selection is required (multi-community / multi-role)
      if (data.status === "select_context" && Array.isArray(data.contexts)) {
        setSelectionToken(data.selection_token);
        setAvailableContexts(data.contexts);
        setStep("select_context");
        setSubmitting(false);
        return;
      }

      // Session established: redirect to dashboard
      await completeLoginRedirect();
    } catch {
      setErrorMessage("A network error occurred while verifying the code.");
      setSubmitting(false);
    }
  }

  // STEP 3: Handle Mandatory Password Change
  async function handlePasswordChange(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    if (!currentPassword) {
      setErrorMessage("Please enter your current temporary password.");
      return;
    }
    if (newPassword.length < 8) {
      setErrorMessage("New password must be at least 8 characters long.");
      return;
    }
    if (newPassword === currentPassword) {
      setErrorMessage("New password cannot be the same as your current temporary password.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setErrorMessage("New passwords do not match. Please re-enter.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/password/change", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        if (res.status === 502 || res.status === 503 || res.status === 504) {
          setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
        } else {
          setErrorMessage(errJson?.detail?.message || "Failed to update password. Please check your inputs.");
        }
        setSubmitting(false);
        return;
      }

      setSuccessNotice("Password successfully updated! Entering dashboard...");
      await completeLoginRedirect();
    } catch {
      setErrorMessage("A network error occurred while updating your password.");
      setSubmitting(false);
    }
  }

  // Resend OTP
  async function handleResendOtp() {
    setErrorMessage("");
    setSuccessNotice("");
    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          username: username.trim().toLowerCase(),
          password: password,
          two_factor: true,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => null);
        if (res.status === 429) {
          const retryMsg = errJson?.error?.message || errJson?.detail?.message;
          setErrorMessage(
            retryMsg && typeof retryMsg === "string" && retryMsg.toLowerCase().includes("wait")
              ? retryMsg
              : "Too many requests. Please wait before requesting another."
          );
        } else if (res.status === 502 || res.status === 503 || res.status === 504) {
          if (errJson?.error?.code === "MESSAGING_UNAVAILABLE") {
            setErrorMessage("Notification service is temporarily unavailable. Please try again later.");
          } else {
            setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
          }
        } else {
          setErrorMessage("Failed to resend verification code. Please wait before requesting another.");
        }
        setSubmitting(false);
        return;
      }

      const data = await res.json();
      setChallengeId(data.challenge_id || "");
      setCooldown(data.cooldown_seconds || 60);
      setSuccessNotice(`New verification code sent to ${maskedMobile || "your registered mobile"}.`);
    } catch {
      setErrorMessage("Network error while resending verification code.");
    } finally {
      setSubmitting(false);
    }
  }

  // Handle Context Selection Confirm
  async function handleSelectContext(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSubmitting(true);

    const chosen = availableContexts[selectedContextIndex];
    if (!chosen) {
      setErrorMessage("Please select a community context to proceed.");
      setSubmitting(false);
      return;
    }

    try {
      const res = await fetchBackendApi("api/v1/auth/otp/select-context", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          selection_token: selectionToken,
          tenant_id: chosen.tenant_id,
          role: chosen.role,
        }),
      });

      if (!res.ok) {
        if (res.status === 502 || res.status === 503 || res.status === 504) {
          setErrorMessage("Authentication service is temporarily unavailable. Please try again later.");
        } else {
          setErrorMessage("Context selection expired or invalid. Please sign in again.");
        }
        setSubmitting(false);
        return;
      }

      const data = await res.json();
      if (data.must_change_password) {
        setStep("change_password");
        setSuccessNotice("First login detected. You must change your temporary password.");
        setSubmitting(false);
        return;
      }

      await completeLoginRedirect();
    } catch {
      setErrorMessage("A network error occurred during context selection.");
      setSubmitting(false);
    }
  }

  async function completeLoginRedirect() {
    const identity = await getAuthenticatedIdentity();
    if (!identity) {
      setErrorMessage("Authenticated session established, but profile could not be loaded.");
      setSubmitting(false);
      return;
    }

    if (identity.must_change_password) {
      setStep("change_password");
      setSuccessNotice("You must change your password before continuing.");
      setSubmitting(false);
      return;
    }

    const destination = routeForRole(identity);
    if (!destination) {
      setErrorMessage(`Your account role (${identity.role}) is not assigned to a recognized dashboard.`);
      setSubmitting(false);
      return;
    }

    router.push(destination);
  }

  // Request Reset OTP
  async function handleForgotMobileSubmit(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    if (!isForgotMobileValid || !forgotMobileE164) {
      setErrorMessage("Please enter a valid 10-digit Indian mobile number.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/password-reset/request", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mobile_number: forgotMobileE164 }),
      });

      const data = await res.json().catch(() => null);

      if (!res.ok) {
        setErrorMessage(data?.message || data?.detail?.message || "Failed to dispatch password reset OTP.");
        return;
      }

      setForgotChallengeId(data.challenge_id || "");
      setForgotMaskedMobile(data.masked_mobile || forgotMobileE164);
      setForgotCooldown(data.cooldown_seconds || 60);
      setStep("forgot_password_otp");
      setSuccessNotice(data.message || `Verification code sent to ${data.masked_mobile || "your mobile"}.`);
    } catch {
      setErrorMessage("Network error occurred while requesting password reset. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // Resend Reset OTP
  async function handleResendForgotOtp() {
    if (forgotCooldown > 0 || submitting) return;
    setErrorMessage("");
    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/password-reset/request", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mobile_number: forgotMobileE164 }),
      });
      const data = await res.json().catch(() => null);
      if (res.ok) {
        setForgotChallengeId(data.challenge_id || "");
        setForgotCooldown(data.cooldown_seconds || 60);
        setSuccessNotice("A new verification code has been dispatched.");
      } else {
        setErrorMessage(data?.message || data?.detail?.message || "Failed to resend code.");
      }
    } catch {
      setErrorMessage("Failed to resend verification code. Please check your connection.");
    } finally {
      setSubmitting(false);
    }
  }

  // Verify Reset OTP
  async function handleForgotOtpSubmit(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    const cleanOtp = forgotOtp.trim();
    if (!cleanOtp) {
      setErrorMessage("Please enter the 6-digit verification code.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/password-reset/verify", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mobile_number: forgotMobileE164,
          challenge_id: forgotChallengeId,
          otp: cleanOtp,
        }),
      });

      const data = await res.json().catch(() => null);

      if (!res.ok) {
        setErrorMessage(data?.message || data?.detail?.message || "Invalid or expired verification code.");
        return;
      }

      setForgotResetToken(data.reset_token || "");
      const accounts = data.accounts || [];
      setForgotAccounts(accounts);
      if (data.account) {
        setSelectedResetAccountId(data.account.account_id);
      } else if (accounts.length > 0) {
        setSelectedResetAccountId(accounts[0].account_id);
      }
      setStep("forgot_password_reset");
      setSuccessNotice("Mobile verified successfully. Enter your new password.");
    } catch {
      setErrorMessage("Network error verifying code. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // Submit New Password
  async function handleResetPasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setErrorMessage("");
    setSuccessNotice("");

    if (!resetNewPassword || resetNewPassword.length < 8) {
      setErrorMessage("New password must be at least 8 characters long.");
      return;
    }
    if (resetNewPassword !== resetConfirmPassword) {
      setErrorMessage("Passwords do not match. Please ensure both passwords match.");
      return;
    }

    setSubmitting(true);
    try {
      const res = await fetchBackendApi("api/v1/auth/password-reset/complete", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mobile_number: forgotMobileE164,
          reset_token: forgotResetToken,
          new_password: resetNewPassword,
          confirm_password: resetConfirmPassword,
          account_id: selectedResetAccountId || undefined,
        }),
      });

      const data = await res.json().catch(() => null);

      if (!res.ok) {
        setErrorMessage(data?.message || data?.detail?.message || "Failed to reset password.");
        return;
      }

      // Pre-fill username from chosen account if available
      const chosenAccount = forgotAccounts.find((a) => a.account_id === selectedResetAccountId);
      if (chosenAccount) {
        setUsername(chosenAccount.username);
      }
      setPassword("");
      setResetNewPassword("");
      setResetConfirmPassword("");
      setForgotOtp("");
      setForgotResetToken("");
      setStep("credentials");
      setSuccessNotice("Password reset successfully! You can now log in with your new password.");
    } catch {
      setErrorMessage("Network error completing password reset. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (checkingAuth) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-sand-50">
        <div className="flex flex-col items-center gap-3">
          <Spinner className="w-8 h-8 text-emerald-600" />
          <p className="text-sm font-medium text-slate-500">Checking session...</p>
        </div>
      </main>
    );
  }

  return (
    <main className="relative flex min-h-screen flex-col overflow-hidden bg-sand-50">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-gradient-to-b from-white via-white to-sand-50"
      />

      <header className="relative z-10 mx-auto flex w-full max-w-6xl items-center justify-between px-5 py-6 sm:px-8 sm:py-8">
        <Brand />
        <span className="hidden rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-500 sm:inline-flex">
          Two-Factor Authentication
        </span>
      </header>

      <div className="relative z-10 mx-auto grid w-full max-w-6xl flex-1 content-center gap-10 px-5 pb-12 pt-6 sm:px-8 lg:grid-cols-[1fr_1fr] lg:gap-16">
        <section className="max-w-xl self-center">
          <p className="mb-3 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700">
            Secure Platform Access
          </p>
          <h1 className="font-display text-3xl font-semibold leading-tight text-navy-800 sm:text-4xl">
            Welcome to Community OTT
          </h1>
          <p className="mt-4 text-base leading-7 text-slate-600">
            Sign in with your username, password, and instant SMS One-Time Password (OTP) verification. 
            All logins are backed by Argon2/scrypt memory-hard hashing, strict rate-limiting, and short-lived session cookies.
          </p>

          <div className="mt-8 rounded-xl border border-slate-200 bg-white/80 p-5 backdrop-blur-sm">
            <h2 className="font-display text-sm font-semibold text-navy-800">
              Role-Based Access
            </h2>
            <ul className="mt-2 space-y-1.5 text-xs text-slate-600">
              <li className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
                <span className="font-medium text-slate-800">Super Admin:</span>
                Platform-wide administration & tenant management
              </li>
              <li className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 rounded-full bg-blue-500" />
                <span className="font-medium text-slate-800">Community Admin:</span>
                Community-scoped agent operations & approvals
              </li>
              <li className="flex items-center gap-2">
                <span className="inline-block h-2 w-2 rounded-full bg-amber-500" />
                <span className="font-medium text-slate-800">Agent:</span>
                Field subscriber registrations & appointments
              </li>
            </ul>
          </div>

          <div className="mt-4 rounded-xl border border-emerald-100 bg-emerald-50/70 p-4 text-xs text-emerald-900">
            <p className="font-semibold text-emerald-800">Super Admin Credentials:</p>
            <p className="mt-1">Username: <code className="font-mono bg-white/80 px-1 py-0.5 rounded">super_admin</code></p>
            <p className="mt-0.5">Password: <code className="font-mono bg-white/80 px-1 py-0.5 rounded">SuperAdmin@123</code></p>
            <p className="mt-0.5">Mobile: <code className="font-mono bg-white/80 px-1 py-0.5 rounded">+91 91002 09205</code></p>
            <p className="mt-1 text-[11px] text-emerald-700">In development mode, test dummy code: <code className="font-bold">123456</code></p>
          </div>
        </section>

        <div className="flex items-center justify-center">
          <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-7 shadow-card sm:p-9">
            <h2 className="font-display text-2xl font-semibold text-navy-800">
              {step === "credentials" && "Sign In"}
              {step === "otp" && "Verify OTP"}
              {step === "change_password" && "Change Password"}
              {step === "select_context" && "Choose Context"}
              {step === "forgot_password_mobile" && "Reset Password"}
              {step === "forgot_password_otp" && "Verify Reset Code"}
              {step === "forgot_password_reset" && "Set New Password"}
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              {step === "credentials" && "Enter your username and password to receive a verification code"}
              {step === "otp" && `Enter the 6-digit code sent to ${maskedMobile || "your mobile"}`}
              {step === "change_password" && "Your temporary password must be changed before accessing resources"}
              {step === "select_context" && "Select the community or workspace to open"}
              {step === "forgot_password_mobile" && "Enter your registered mobile number to receive a verification OTP"}
              {step === "forgot_password_otp" && `Enter the 6-digit code sent to ${forgotMaskedMobile || "your mobile"}`}
              {step === "forgot_password_reset" && "Create a new secure password with at least 8 characters"}
            </p>

            {errorMessage && (
              <div
                role="alert"
                aria-live="polite"
                className="mt-5 rounded-lg border border-red-200 bg-red-50 p-4"
              >
                <div className="flex items-start gap-3">
                  <span className="mt-0.5 text-red-600 font-bold" aria-hidden="true">
                    !
                  </span>
                  <p className="text-sm font-medium text-red-900 leading-snug">
                    {errorMessage}
                  </p>
                </div>
              </div>
            )}

            {successNotice && (
              <div
                role="status"
                className="mt-5 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm font-medium text-emerald-900"
              >
                {successNotice}
              </div>
            )}

            {/* STEP 1: USERNAME + PASSWORD */}
            {step === "credentials" && (
              <form onSubmit={handleCredentialsSubmit} noValidate className="mt-6 space-y-4">
                <div>
                  <label
                    htmlFor="username"
                    className="block text-sm font-medium text-slate-700 mb-1"
                  >
                    Username
                  </label>
                  <input
                    id="username"
                    name="username"
                    type="text"
                    autoComplete="username"
                    disabled={submitting}
                    required
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="super_admin"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <div>
                  <div className="flex items-center justify-between mb-1">
                    <label
                      htmlFor="password"
                      className="block text-sm font-medium text-slate-700"
                    >
                      Password
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        setErrorMessage("");
                        setSuccessNotice("");
                        setStep("forgot_password_mobile");
                      }}
                      className="text-xs font-semibold text-emerald-600 hover:text-emerald-700 hover:underline transition-colors"
                    >
                      Forgot Password?
                    </button>
                  </div>
                  <input
                    id="password"
                    name="password"
                    type="password"
                    autoComplete="current-password"
                    disabled={submitting}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="••••••••••••"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <button
                  type="submit"
                  disabled={submitting || !username.trim() || !password}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Verifying & Sending OTP...</span>
                    </span>
                  ) : (
                    "Sign In & Send Code"
                  )}
                </button>
              </form>
            )}

            {/* STEP 2: OTP ENTRY */}
            {step === "otp" && (
              <form onSubmit={handleVerifyOtp} noValidate className="mt-6 space-y-5">
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label
                      htmlFor="otpCode"
                      className="block text-sm font-medium text-slate-700"
                    >
                      6-Digit Verification Code
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        setStep("credentials");
                        setOtpCode("");
                        setErrorMessage("");
                        setSuccessNotice("");
                      }}
                      className="text-xs text-emerald-700 hover:text-emerald-800 font-medium"
                    >
                      Back to sign in
                    </button>
                  </div>
                  <input
                    id="otpCode"
                    name="otpCode"
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    maxLength={6}
                    autoComplete="one-time-code"
                    autoFocus
                    disabled={submitting}
                    required
                    value={otpCode}
                    onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ""))}
                    placeholder="123456"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-center font-mono text-xl tracking-[0.3em] text-slate-900 placeholder-slate-300 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                  {attemptsRemaining !== null && (
                    <p className="mt-1 text-xs text-amber-600 font-medium">
                      {attemptsRemaining} attempt{attemptsRemaining === 1 ? "" : "s"} remaining before code expires.
                    </p>
                  )}
                </div>

                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>Didn&apos;t receive code?</span>
                  {cooldown > 0 ? (
                    <span className="text-slate-400 font-medium">
                      Resend in {cooldown}s
                    </span>
                  ) : (
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => handleResendOtp()}
                      className="text-emerald-700 hover:text-emerald-800 font-semibold disabled:opacity-50"
                    >
                      Resend Code
                    </button>
                  )}
                </div>

                <button
                  type="submit"
                  disabled={submitting || otpCode.length < 6}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Verifying Code...</span>
                    </span>
                  ) : (
                    "Verify Code & Enter"
                  )}
                </button>
              </form>
            )}

            {/* STEP 3: MANDATORY PASSWORD CHANGE */}
            {step === "change_password" && (
              <form onSubmit={handlePasswordChange} noValidate className="mt-6 space-y-4">
                <div className="rounded-lg bg-amber-50 border border-amber-200 p-3 text-xs text-amber-800">
                  First login security policy: You must set a permanent password before accessing protected platform dashboards.
                </div>

                <div>
                  <label
                    htmlFor="currentPassword"
                    className="block text-sm font-medium text-slate-700 mb-1"
                  >
                    Current (Temporary) Password
                  </label>
                  <input
                    id="currentPassword"
                    name="currentPassword"
                    type="password"
                    autoComplete="current-password"
                    disabled={submitting}
                    required
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(e.target.value)}
                    placeholder="••••••••••••"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <div>
                  <label
                    htmlFor="newPassword"
                    className="block text-sm font-medium text-slate-700 mb-1"
                  >
                    New Password
                  </label>
                  <input
                    id="newPassword"
                    name="newPassword"
                    type="password"
                    autoComplete="new-password"
                    disabled={submitting}
                    required
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="At least 8 characters"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <div>
                  <label
                    htmlFor="confirmPassword"
                    className="block text-sm font-medium text-slate-700 mb-1"
                  >
                    Confirm New Password
                  </label>
                  <input
                    id="confirmPassword"
                    name="confirmPassword"
                    type="password"
                    autoComplete="new-password"
                    disabled={submitting}
                    required
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Re-enter new password"
                    className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <button
                  type="submit"
                  disabled={submitting || !newPassword || !confirmPassword}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Updating Password...</span>
                    </span>
                  ) : (
                    "Save New Password & Continue"
                  )}
                </button>
              </form>
            )}

            {/* STEP 4: CONTEXT SELECTION (MULTI-ROLE/MULTI-TENANT) */}
            {step === "select_context" && (
              <form onSubmit={handleSelectContext} noValidate className="mt-6 space-y-5">
                <p className="text-xs text-slate-600">
                  Your identity is associated with multiple roles or communities. Choose which one to access:
                </p>

                <div className="space-y-2 max-h-60 overflow-y-auto">
                  {availableContexts.map((ctx, idx) => (
                    <label
                      key={`${ctx.tenant_id || "platform"}_${ctx.role}_${idx}`}
                      className={`flex items-start gap-3 p-3.5 rounded-xl border cursor-pointer transition-all ${
                        selectedContextIndex === idx
                          ? "border-emerald-500 bg-emerald-50/40 ring-1 ring-emerald-500"
                          : "border-slate-200 hover:border-slate-300 bg-white"
                      }`}
                    >
                      <input
                        type="radio"
                        name="contextSelect"
                        checked={selectedContextIndex === idx}
                        onChange={() => setSelectedContextIndex(idx)}
                        className="mt-1 text-emerald-600 focus:ring-emerald-500"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-semibold text-sm text-navy-800">
                            {ctx.tenant_slug ? ctx.tenant_slug : "Platform Root"}
                          </span>
                          <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-slate-100 text-slate-700">
                            {ctx.role}
                          </span>
                        </div>
                        <p className="text-xs text-slate-500 mt-0.5 truncate">
                          User: {ctx.username}
                        </p>
                      </div>
                    </label>
                  ))}
                </div>

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full mt-3 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Entering Dashboard...</span>
                    </span>
                  ) : (
                    "Continue to Dashboard"
                  )}
                </button>
              </form>
            )}

            {/* STEP: FORGOT PASSWORD - MOBILE INPUT */}
            {step === "forgot_password_mobile" && (
              <form onSubmit={handleForgotMobileSubmit} noValidate className="mt-6 space-y-4">
                <IndianMobileInput
                  label="Registered Mobile Number"
                  value={forgotMobileE164}
                  onChange={(e164, tenDigits, isValid) => {
                    setForgotMobileE164(e164);
                    setForgotTenDigits(tenDigits);
                    setIsForgotMobileValid(isValid);
                  }}
                  disabled={submitting}
                  required
                  placeholder="98765 43210"
                  hint="Enter your 10-digit Indian mobile number to receive a verification OTP."
                />

                <button
                  type="submit"
                  disabled={submitting || !isForgotMobileValid}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Sending OTP...</span>
                    </span>
                  ) : (
                    "Send Verification Code"
                  )}
                </button>

                <div className="pt-2 text-center">
                  <button
                    type="button"
                    onClick={() => {
                      setErrorMessage("");
                      setSuccessNotice("");
                      setStep("credentials");
                    }}
                    className="text-xs font-semibold text-slate-500 hover:text-navy-800 transition-colors"
                  >
                    ← Back to Sign In
                  </button>
                </div>
              </form>
            )}

            {/* STEP: FORGOT PASSWORD - OTP ENTRY */}
            {step === "forgot_password_otp" && (
              <form onSubmit={handleForgotOtpSubmit} noValidate className="mt-6 space-y-4">
                <div>
                  <label htmlFor="forgot-otp" className="block text-sm font-medium text-slate-700 mb-1">
                    6-Digit Verification Code
                  </label>
                  <input
                    id="forgot-otp"
                    name="forgot-otp"
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    pattern="[0-9]*"
                    maxLength={6}
                    disabled={submitting}
                    required
                    value={forgotOtp}
                    onChange={(e) => setForgotOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                    placeholder="123456"
                    className="w-full text-center tracking-[0.4em] font-mono font-bold text-lg rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-slate-900 placeholder-slate-300 transition-colors focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                  />
                </div>

                <div className="flex items-center justify-between text-xs">
                  <span className="text-slate-500">Didn&apos;t receive code?</span>
                  <button
                    type="button"
                    disabled={forgotCooldown > 0 || submitting}
                    onClick={handleResendForgotOtp}
                    className="font-semibold text-emerald-600 hover:text-emerald-700 disabled:text-slate-400 disabled:cursor-not-allowed"
                  >
                    {forgotCooldown > 0 ? `Resend code in ${forgotCooldown}s` : "Resend code"}
                  </button>
                </div>

                <button
                  type="submit"
                  disabled={submitting || forgotOtp.length < 6}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Verifying Code...</span>
                    </span>
                  ) : (
                    "Verify Code & Continue"
                  )}
                </button>

                <div className="pt-2 text-center">
                  <button
                    type="button"
                    onClick={() => {
                      setErrorMessage("");
                      setSuccessNotice("");
                      setStep("forgot_password_mobile");
                    }}
                    className="text-xs font-semibold text-slate-500 hover:text-navy-800 transition-colors"
                  >
                    ← Change Mobile Number
                  </button>
                </div>
              </form>
            )}

            {/* STEP: FORGOT PASSWORD - NEW PASSWORD */}
            {step === "forgot_password_reset" && (
              <form onSubmit={handleResetPasswordSubmit} noValidate className="mt-6 space-y-4">
                {/* Account Selection if multiple accounts found */}
                {forgotAccounts.length > 1 && (
                  <div>
                    <label className="block text-xs font-semibold uppercase tracking-wider text-slate-600 mb-2">
                      Select Account to Reset
                    </label>
                    <div className="space-y-2 max-h-48 overflow-y-auto">
                      {forgotAccounts.map((acc) => (
                        <label
                          key={acc.account_id}
                          className={`flex items-start gap-3 p-3 rounded-xl border cursor-pointer transition ${
                            selectedResetAccountId === acc.account_id
                              ? "border-emerald-500 bg-emerald-50/40 ring-1 ring-emerald-500"
                              : "border-slate-200 bg-white hover:border-slate-300"
                          }`}
                        >
                          <input
                            type="radio"
                            name="resetAccount"
                            checked={selectedResetAccountId === acc.account_id}
                            onChange={() => setSelectedResetAccountId(acc.account_id)}
                            className="mt-1 text-emerald-600 focus:ring-emerald-500"
                          />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="font-semibold text-sm text-navy-900">{acc.username}</span>
                              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-100 text-slate-700">
                                {acc.role}
                              </span>
                            </div>
                            {acc.community_name && (
                              <p className="text-xs text-slate-500 mt-0.5">
                                Community: {acc.community_name}
                              </p>
                            )}
                          </div>
                        </label>
                      ))}
                    </div>
                  </div>
                )}

                {forgotAccounts.length === 1 && (
                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <p className="text-xs text-slate-500">Resetting password for:</p>
                      <p className="text-sm font-semibold text-navy-900">{forgotAccounts[0].username}</p>
                    </div>
                    <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-emerald-100 text-emerald-800">
                      {forgotAccounts[0].role}
                    </span>
                  </div>
                )}

                <div>
                  <label htmlFor="reset-new-password" className="block text-sm font-medium text-slate-700 mb-1">
                    New Password
                  </label>
                  <div className="relative">
                    <input
                      id="reset-new-password"
                      type={showResetNewPassword ? "text" : "password"}
                      required
                      disabled={submitting}
                      value={resetNewPassword}
                      onChange={(e) => setResetNewPassword(e.target.value)}
                      placeholder="At least 8 characters"
                      className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 pr-14 text-sm text-slate-900 placeholder-slate-400 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                    />
                    <button
                      type="button"
                      onClick={() => setShowResetNewPassword(!showResetNewPassword)}
                      className="absolute right-3 top-2.5 text-xs text-slate-400 hover:text-slate-600 font-semibold"
                    >
                      {showResetNewPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                  <p className="text-[11px] text-slate-400 mt-1">Minimum 8 characters.</p>
                </div>

                <div>
                  <label htmlFor="reset-confirm-password" className="block text-sm font-medium text-slate-700 mb-1">
                    Confirm New Password
                  </label>
                  <div className="relative">
                    <input
                      id="reset-confirm-password"
                      type={showResetConfirmPassword ? "text" : "password"}
                      required
                      disabled={submitting}
                      value={resetConfirmPassword}
                      onChange={(e) => setResetConfirmPassword(e.target.value)}
                      placeholder="Re-enter new password"
                      className="w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 pr-14 text-sm text-slate-900 placeholder-slate-400 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/20 disabled:opacity-60"
                    />
                    <button
                      type="button"
                      onClick={() => setShowResetConfirmPassword(!showResetConfirmPassword)}
                      className="absolute right-3 top-2.5 text-xs text-slate-400 hover:text-slate-600 font-semibold"
                    >
                      {showResetConfirmPassword ? "Hide" : "Show"}
                    </button>
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={submitting || resetNewPassword.length < 8 || !resetConfirmPassword}
                  className="w-full mt-2 inline-flex min-h-12 items-center justify-center rounded-lg bg-navy-700 px-5 py-3 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-navy-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-600 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="h-4 w-4" />
                      <span>Resetting Password...</span>
                    </span>
                  ) : (
                    "Reset Password & Back to Login"
                  )}
                </button>

                <div className="pt-2 text-center">
                  <button
                    type="button"
                    onClick={() => {
                      setErrorMessage("");
                      setSuccessNotice("");
                      setStep("credentials");
                    }}
                    className="text-xs font-semibold text-slate-500 hover:text-navy-800 transition-colors"
                  >
                    ← Cancel & Back to Sign In
                  </button>
                </div>
              </form>
            )}

            <p className="mt-6 text-center text-xs text-slate-400">
              Community OTT Agent Platform · Protected Access
            </p>
          </div>
        </div>
      </div>

      <footer className="relative z-10 mx-auto w-full max-w-6xl px-5 pb-6 text-xs text-slate-400 sm:px-8">
        Community OTT Agent Platform
      </footer>
    </main>
  );
}
