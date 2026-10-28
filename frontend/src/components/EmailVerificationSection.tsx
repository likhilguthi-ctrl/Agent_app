"use client";

import React, { useState, useEffect } from "react";
import {
  requestEmailOtpNotification,
  verifyEmailOtpNotification,
} from "@/lib/notifications";

interface EmailVerificationSectionProps {
  email: string;
  isVerified: boolean;
  verificationToken?: string;
  required?: boolean;
  communityId?: string;
  onChange: (data: {
    email: string;
    isVerified: boolean;
    verificationToken?: string;
  }) => void;
  disabled?: boolean;
  className?: string;
}

export default function EmailVerificationSection({
  email,
  isVerified,
  verificationToken,
  required = true,
  communityId,
  onChange,
  disabled = false,
  className = "",
}: EmailVerificationSectionProps) {
  const [currentEmail, setCurrentEmail] = useState(email);
  const [otpCode, setOtpCode] = useState("");
  const [challengeId, setChallengeId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  useEffect(() => {
    setCurrentEmail(email);
  }, [email]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((c) => Math.max(0, c - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  const isValidEmailFormat = (val: string) => {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val.trim());
  };

  const handleEmailChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setCurrentEmail(val);
    setError(null);
    setSuccessMsg(null);
    setOtpCode("");
    setChallengeId(null);

    // If user changes email, reset verification
    onChange({
      email: val,
      isVerified: false,
      verificationToken: undefined,
    });
  };

  const handleSendOtp = async () => {
    const cleanEmail = currentEmail.trim().toLowerCase();
    if (!isValidEmailFormat(cleanEmail)) {
      setError("Please enter a valid email address.");
      return;
    }

    setSending(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const res = await requestEmailOtpNotification({
        email: cleanEmail,
        purpose: "email_verification",
        communityId,
      });
      setChallengeId(res.challengeId);
      setCooldown(res.cooldownSeconds || 60);
      setSuccessMsg("Verification code sent to your email address.");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to send email OTP.";
      setError(msg);
    } finally {
      setSending(false);
    }
  };

  const handleVerifyOtp = async () => {
    const cleanOtp = otpCode.trim();
    if (!cleanOtp || cleanOtp.length < 6) {
      setError("Please enter the 6-digit OTP sent to your email.");
      return;
    }
    if (!challengeId) {
      setError("Please request a verification code first.");
      return;
    }

    const cleanEmail = currentEmail.trim().toLowerCase();
    setVerifying(true);
    setError(null);
    try {
      const res = await verifyEmailOtpNotification({
        email: cleanEmail,
        otp: cleanOtp,
        challengeId,
        purpose: "email_verification",
        communityId,
      });

      if (res.success && res.verificationToken) {
        setSuccessMsg("Email successfully verified!");
        onChange({
          email: cleanEmail,
          isVerified: true,
          verificationToken: res.verificationToken,
        });
      } else {
        setError("Verification was not completed. Please try again.");
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Verification failed.";
      setError(msg);
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className={`space-y-3 ${className}`}>
      <div>
        <label className="block text-sm font-medium text-slate-700 mb-1.5">
          Email Address
          {required && <span className="text-emerald-600"> *</span>}
          {!required && <span className="text-slate-400 font-normal"> (Optional)</span>}
        </label>
        <div className="flex rounded-lg shadow-sm">
          <input
            type="email"
            value={currentEmail}
            onChange={handleEmailChange}
            disabled={disabled || isVerified}
            placeholder="name@example.com"
            className={`flex-1 rounded-lg border ${
              error
                ? "border-red-300 focus:border-red-400 focus:ring-red-100"
                : "border-slate-300 focus:border-navy-400 focus:ring-navy-100"
            } bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:ring-2 transition disabled:bg-slate-50 disabled:text-slate-500`}
          />
        </div>
      </div>

      {/* Verification Action / Status */}
      {currentEmail.trim().length > 0 && (
        <div className="p-3.5 rounded-xl border border-slate-200 bg-slate-50 space-y-3">
          {isVerified ? (
            <div className="flex items-center space-x-2 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 px-3.5 py-2 rounded-lg">
              <svg className="w-4 h-4 text-emerald-600 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
              </svg>
              <span>Email Verified</span>
              <button
                type="button"
                onClick={() => {
                  onChange({
                    email: currentEmail,
                    isVerified: false,
                    verificationToken: undefined,
                  });
                }}
                className="ml-auto text-xs text-slate-500 hover:text-slate-800 underline"
              >
                Change
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center space-x-2">
                <button
                  type="button"
                  onClick={handleSendOtp}
                  disabled={
                    disabled ||
                    sending ||
                    cooldown > 0 ||
                    !isValidEmailFormat(currentEmail)
                  }
                  className="px-4 py-2 text-xs font-semibold rounded-lg bg-navy-600 text-white hover:bg-navy-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition"
                >
                  {sending
                    ? "Sending..."
                    : cooldown > 0
                    ? `Resend in ${cooldown}s`
                    : challengeId
                    ? "Resend Email OTP"
                    : "Send Email OTP"}
                </button>
                <span className="text-xs text-slate-500">
                  {required
                    ? "Email OTP verification is mandatory."
                    : "Email entered must be verified."}
                </span>
              </div>

              {challengeId && (
                <div className="flex items-center space-x-2 pt-1">
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    placeholder="6-digit OTP"
                    value={otpCode}
                    onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                    className="w-36 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-center tracking-widest font-mono text-slate-900 outline-none focus:border-navy-400 focus:ring-2 focus:ring-navy-100 transition"
                  />
                  <button
                    type="button"
                    onClick={handleVerifyOtp}
                    disabled={disabled || verifying || otpCode.length < 6}
                    className="px-4 py-2 text-xs font-semibold rounded-lg bg-navy-600 text-white hover:bg-navy-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition"
                  >
                    {verifying ? "Verifying..." : "Verify Code"}
                  </button>
                </div>
              )}
            </div>
          )}

          {error && <p className="text-xs text-red-600">{error}</p>}
          {successMsg && !isVerified && <p className="text-xs text-emerald-600">{successMsg}</p>}
        </div>
      )}
    </div>
  );
}

