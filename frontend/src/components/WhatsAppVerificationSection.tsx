"use client";

import React, { useState, useEffect } from "react";
import IndianMobileInput, { isValidIndianMobile, extractTenDigits } from "./IndianMobileInput";
import { requestOtpNotification, verifyOtpNotification } from "@/lib/notifications";

interface WhatsAppVerificationSectionProps {
  primaryMobile: string; // e.g. "+919876543210"
  whatsappNumber: string;
  sameAsMobile: boolean;
  isVerified: boolean;
  verificationToken?: string;
  communityId?: string;
  onChange: (data: {
    whatsappNumber: string;
    sameAsMobile: boolean;
    isVerified: boolean;
    verificationToken?: string;
  }) => void;
  disabled?: boolean;
  className?: string;
}

export default function WhatsAppVerificationSection({
  primaryMobile,
  whatsappNumber,
  sameAsMobile,
  isVerified,
  verificationToken,
  communityId,
  onChange,
  disabled = false,
  className = "",
}: WhatsAppVerificationSectionProps) {
  const [altNumber, setAltNumber] = useState(
    sameAsMobile ? "" : whatsappNumber
  );
  const [otpCode, setOtpCode] = useState("");
  const [challengeId, setChallengeId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Cooldown timer
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((c) => Math.max(0, c - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  const handleCheckboxToggle = (e: React.ChangeEvent<HTMLInputElement>) => {
    const checked = e.target.checked;
    setError(null);
    setSuccessMsg(null);
    setOtpCode("");
    setChallengeId(null);

    if (checked) {
      onChange({
        whatsappNumber: primaryMobile,
        sameAsMobile: true,
        isVerified: true,
        verificationToken: undefined,
      });
    } else {
      const currentTen = extractTenDigits(altNumber);
      const e164 = currentTen ? `+91${currentTen}` : "";
      onChange({
        whatsappNumber: e164,
        sameAsMobile: false,
        isVerified: false,
        verificationToken: undefined,
      });
    }
  };

  const handleAltNumberChange = (
    e164Val: string,
    tenDigits: string,
    isValid: boolean
  ) => {
    setAltNumber(e164Val);
    setError(null);
    setSuccessMsg(null);
    setOtpCode("");
    setChallengeId(null);

    // If alternate number is literally identical to primary mobile, consider it verified
    const isSame = primaryMobile && e164Val === primaryMobile;
    onChange({
      whatsappNumber: e164Val,
      sameAsMobile: isSame,
      isVerified: isSame,
      verificationToken: undefined,
    });
  };

  const handleSendOtp = async () => {
    const tenDigits = extractTenDigits(altNumber);
    if (!isValidIndianMobile(tenDigits)) {
      setError("Please enter a valid 10-digit Indian WhatsApp number.");
      return;
    }
    const e164 = `+91${tenDigits}`;

    setSending(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const res = await requestOtpNotification({
        channel: "WHATSAPP",
        mobileNumber: e164,
        purpose: "whatsapp_verification",
        communityId,
      });
      setChallengeId(res.challengeId);
      setCooldown(res.cooldownSeconds || 60);
      setSuccessMsg("Verification code sent to your WhatsApp number.");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to send WhatsApp OTP.";
      setError(msg);
    } finally {
      setSending(false);
    }
  };

  const handleVerifyOtp = async () => {
    const cleanOtp = otpCode.trim();
    if (!cleanOtp || cleanOtp.length < 6) {
      setError("Please enter the 6-digit OTP received on WhatsApp.");
      return;
    }
    if (!challengeId) {
      setError("Please request an OTP first.");
      return;
    }

    const e164 = `+91${extractTenDigits(altNumber)}`;
    setVerifying(true);
    setError(null);
    try {
      const res = await verifyOtpNotification({
        mobileNumber: e164,
        otp: cleanOtp,
        challengeId,
        purpose: "whatsapp_verification",
        communityId,
      });

      if (res.success && res.verificationToken) {
        setSuccessMsg("WhatsApp number successfully verified!");
        onChange({
          whatsappNumber: e164,
          sameAsMobile: false,
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
      {/* Checkbox: Same as Mobile */}
      <div className="flex items-center space-x-2.5">
        <input
          id="whatsapp_same_as_mobile"
          type="checkbox"
          checked={sameAsMobile}
          onChange={handleCheckboxToggle}
          disabled={disabled}
          className="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500 transition"
        />
        <label
          htmlFor="whatsapp_same_as_mobile"
          className="text-sm font-medium text-slate-700 cursor-pointer select-none"
        >
          WhatsApp number is the same as my mobile number
        </label>
      </div>

      {/* Alternative WhatsApp Number Section */}
      {!sameAsMobile && (
        <div className="p-4 rounded-xl border border-slate-200 bg-slate-50 space-y-3.5">
          <IndianMobileInput
            label="Alternative WhatsApp Number"
            value={altNumber}
            onChange={handleAltNumberChange}
            disabled={disabled || isVerified}
            required
            hint="We will send a verification code to this WhatsApp number"
          />

          {isVerified ? (
            <div className="flex items-center space-x-2 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 px-3.5 py-2 rounded-lg">
              <svg className="w-4 h-4 text-emerald-600 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
              </svg>
              <span>WhatsApp Number Verified</span>
              <button
                type="button"
                onClick={() => {
                  onChange({
                    whatsappNumber: altNumber,
                    sameAsMobile: false,
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
                    !isValidIndianMobile(extractTenDigits(altNumber))
                  }
                  className="px-4 py-2 text-xs font-semibold rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition"
                >
                  {sending
                    ? "Sending..."
                    : cooldown > 0
                    ? `Resend in ${cooldown}s`
                    : challengeId
                    ? "Resend WhatsApp OTP"
                    : "Send WhatsApp OTP"}
                </button>
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

