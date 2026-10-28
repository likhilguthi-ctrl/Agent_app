"use client";

import React, { useState } from "react";
import { Card, Button, Input, Spinner, Alert } from "./ui";
import IndianMobileInput, { extractTenDigits, isValidIndianMobile } from "./IndianMobileInput";
import RegistrationPhotoCapture from "./RegistrationPhotoCapture";
import { uploadRegistrationImage } from "@/lib/storage";
import { fetchBackendApi } from "@/lib/backend-api";
import { extractResponseError, normalizeApiError } from "@/lib/api-error";
import { requestOtpNotification, verifyOtpNotification } from "@/lib/notifications";
import { useAuth } from "./AuthGuard";

export interface ApprovedCreationRequest {
  id: string;
  request_number: string;
  state_name: string;
  district_name: string;
  mandal_name: string;
  state_id: string;
  district_id: string;
  mandal_id: string;
}

interface CompleteChildAgentRegistrationModalProps {
  request: ApprovedCreationRequest | null;
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export default function CompleteChildAgentRegistrationModal({
  request,
  isOpen,
  onClose,
  onSuccess,
}: CompleteChildAgentRegistrationModalProps) {
  const { identity } = useAuth();

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mobile, setMobile] = useState("");
  const [email, setEmail] = useState("");
  const [address, setAddress] = useState("");
  const [aadhaarNumber, setAadhaarNumber] = useState("");
  const [panNumber, setPanNumber] = useState("");

  // OTP
  const [otpSent, setOtpSent] = useState(false);
  const [otpCode, setOtpCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [cooldown, setCooldown] = useState(0);
  const [sendingOtp, setSendingOtp] = useState(false);
  const [verifyingOtp, setVerifyingOtp] = useState(false);
  const [isMobileVerified, setIsMobileVerified] = useState(false);
  const [verificationToken, setVerificationToken] = useState<string | null>(null);

  // Photos
  const [profilePhoto, setProfilePhoto] = useState<File | null>(null);
  const [aadhaarDoc, setAadhaarDoc] = useState<File | null>(null);
  const [panDoc, setPanDoc] = useState<File | null>(null);

  // Submission
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  // Countdown timer
  React.useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => setCooldown((c) => (c > 0 ? c - 1 : 0)), 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  if (!isOpen || !request) return null;

  async function handleSendOtp() {
    setError(null);
    const cleanTen = extractTenDigits(mobile);
    if (!isValidIndianMobile(cleanTen)) {
      setError("Please enter a valid 10-digit Indian mobile number.");
      return;
    }
    const cleanMobile = `+91${cleanTen}`;
    setSendingOtp(true);
    try {
      const data = await requestOtpNotification({
        mobileNumber: cleanMobile,
        purpose: "agent_registration",
        communityId: identity?.tenant_id,
      });
      setChallengeId(data.challengeId);
      setCooldown(data.cooldownSeconds);
      setOtpSent(true);
    } catch (err) {
      setError(normalizeApiError(err, "Failed to send OTP."));
    } finally {
      setSendingOtp(false);
    }
  }

  async function handleVerifyOtp() {
    setError(null);
    if (otpCode.length < 6) {
      setError("Please enter the 6-digit verification code.");
      return;
    }
    setVerifyingOtp(true);
    try {
      const cleanMobile = `+91${extractTenDigits(mobile)}`;
      const data = await verifyOtpNotification({
        mobileNumber: cleanMobile,
        otp: otpCode.trim(),
        challengeId,
        purpose: "agent_registration",
      });
      if (data.verificationToken) {
        setVerificationToken(data.verificationToken);
        setIsMobileVerified(true);
      } else {
        setError("Verification did not return a valid verification token.");
      }
    } catch (err) {
      setError(normalizeApiError(err, "OTP verification failed."));
    } finally {
      setVerifyingOtp(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!firstName.trim() || !lastName.trim()) {
      setError("First and Last name are required.");
      return;
    }
    if (!username.trim() || username.length < 3) {
      setError("Username must be at least 3 characters.");
      return;
    }
    if (!password.trim() || password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (!isMobileVerified || !verificationToken) {
      setError("Candidate mobile number must be verified via OTP.");
      return;
    }
    if (aadhaarNumber.length !== 12) {
      setError("Aadhaar Number must be 12 digits.");
      return;
    }
    if (panNumber.length !== 10) {
      setError("PAN Number must be 10 characters.");
      return;
    }
    if (!profilePhoto || !aadhaarDoc || !panDoc) {
      setError("Profile photo, Aadhaar card document, and PAN card document are mandatory.");
      return;
    }

    const csrfToken = identity?.csrf_token || "";
    setIsSubmitting(true);
    setStatusMsg("Uploading candidate documents to secure storage…");

    try {
      const photoResult = await uploadRegistrationImage(
        profilePhoto,
        "agent-photo",
        csrfToken,
        verificationToken
      );
      const aadhaarResult = await uploadRegistrationImage(
        aadhaarDoc,
        "aadhaar-document",
        csrfToken,
        verificationToken
      );
      const panResult = await uploadRegistrationImage(
        panDoc,
        "pan-document",
        csrfToken,
        verificationToken
      );

      setStatusMsg("Registering Child Agent under approved authorization…");
      const cleanMobile = `+91${extractTenDigits(mobile)}`;

      const payload = {
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        username: username.trim(),
        password: password.trim(),
        mobile_e164: cleanMobile,
        verification_token: verificationToken,
        email: email.trim() || `${username.trim()}@avanflix.local`,
        whatsapp_same_as_mobile: true,
        whatsapp_number: cleanMobile,
        address: address.trim() || null,
        state: request.state_name,
        state_id: request.state_id,
        district: request.district_name,
        district_id: request.district_id,
        mandal: request.mandal_name,
        mandal_id: request.mandal_id,
        aadhaar_number: aadhaarNumber,
        aadhaar_document_key: aadhaarResult.object_key,
        pan_number: panNumber.toUpperCase(),
        pan_document_key: panResult.object_key,
        profile_photo_key: photoResult.object_key,
        status: "active",
      };

      const res = await fetchBackendApi(
        `api/v1/agent/agents-from-request?creation_request_id=${request.id}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(payload),
          credentials: "same-origin",
        }
      );

      if (!res.ok) {
        const errText = await extractResponseError(res, "Failed to create child agent.");
        throw new Error(errText);
      }

      onSuccess();
    } catch (err: unknown) {
      setError(normalizeApiError(err, "Registration failed."));
    } finally {
      setIsSubmitting(false);
      setStatusMsg(null);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-xs overflow-y-auto">
      <Card className="max-w-2xl w-full p-6 space-y-5 my-8 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between border-b pb-3">
          <div>
            <h2 className="text-lg font-bold text-navy-900">
              Complete Child Agent Registration
            </h2>
            <p className="text-xs text-slate-500">
              Authorization Ref: <span className="font-mono font-semibold text-navy-800">{request.request_number}</span>
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-lg font-bold"
          >
            ✕
          </button>
        </div>

        {error && <Alert tone="red">{error}</Alert>}

        {/* Territory Info Card */}
        <div className="rounded-xl border border-indigo-100 bg-indigo-50/60 p-4 text-xs">
          <p className="font-semibold text-indigo-900 mb-1">Approved Operating Territory</p>
          <p className="text-indigo-800">
            State: <span className="font-semibold">{request.state_name}</span> · District: <span className="font-semibold">{request.district_name}</span> · Mandal: <span className="font-semibold">{request.mandal_name}</span>
          </p>
          <p className="text-indigo-600 mt-1">
            This child agent will be bound to this approved jurisdiction and will report directly to you.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                First Name <span className="text-red-500">*</span>
              </label>
              <Input
                value={firstName}
                onChange={(e) => setFirstName(e.target.value)}
                placeholder="Candidate First Name"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Last Name <span className="text-red-500">*</span>
              </label>
              <Input
                value={lastName}
                onChange={(e) => setLastName(e.target.value)}
                placeholder="Candidate Last Name"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Username <span className="text-red-500">*</span>
              </label>
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="e.g. subagent_ramesh"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Initial Password <span className="text-red-500">*</span>
              </label>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Minimum 8 characters"
                required
              />
            </div>
          </div>

          {/* Candidate Mobile & OTP Verification */}
          <div className="rounded-xl border border-slate-200 bg-slate-50/60 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <label className="block text-xs font-semibold text-slate-800">
                Candidate Mobile Number & Verification <span className="text-red-500">*</span>
              </label>
              {isMobileVerified && (
                <span className="text-xs font-semibold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full">
                  ✓ Verified
                </span>
              )}
            </div>
            <div className="flex flex-col sm:flex-row gap-2">
              <div className="flex-1">
                <IndianMobileInput
                  value={mobile}
                  disabled={isMobileVerified || sendingOtp}
                  onChange={(val) => {
                    setMobile(val);
                    setIsMobileVerified(false);
                    setVerificationToken(null);
                    setOtpSent(false);
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
                >
                  {sendingOtp ? <Spinner className="w-3.5 h-3.5" /> : cooldown > 0 ? `${cooldown}s` : "Verify OTP"}
                </Button>
              )}
            </div>

            {otpSent && !isMobileVerified && (
              <div className="flex items-center gap-2 pt-2">
                <Input
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                  placeholder="6-digit OTP (dummy: 123456)"
                  className="font-mono text-center tracking-widest"
                />
                <Button
                  type="button"
                  disabled={verifyingOtp || otpCode.length < 6}
                  onClick={handleVerifyOtp}
                >
                  {verifyingOtp ? <Spinner className="w-3.5 h-3.5" /> : "Confirm"}
                </Button>
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Email Address <span className="text-slate-400 font-normal">(optional)</span>
              </label>
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="agent@example.com"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Physical Address
              </label>
              <Input
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder="Mandal / Town, PIN"
              />
            </div>
          </div>

          {/* Identity Documents */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                Aadhaar Number (12 Digits) <span className="text-red-500">*</span>
              </label>
              <Input
                value={aadhaarNumber}
                onChange={(e) => setAadhaarNumber(e.target.value.replace(/\D/g, "").slice(0, 12))}
                placeholder="12-digit number"
                required
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                PAN Number <span className="text-red-500">*</span>
              </label>
              <Input
                value={panNumber}
                onChange={(e) => setPanNumber(e.target.value.toUpperCase().slice(0, 10))}
                placeholder="ABCDE1234F"
                required
              />
            </div>
          </div>

          <div className="space-y-3 pt-2 border-t">
            <RegistrationPhotoCapture
              label="Candidate Profile Photo"
              required
              onPhotoChange={(f) => setProfilePhoto(f)}
            />
            <RegistrationPhotoCapture
              label="Candidate Aadhaar Card Document"
              required
              onPhotoChange={(f) => setAadhaarDoc(f)}
            />
            <RegistrationPhotoCapture
              label="Candidate PAN Card Document"
              required
              onPhotoChange={(f) => setPanDoc(f)}
            />
          </div>

          <div className="flex items-center justify-between pt-4 border-t">
            <Button type="button" variant="secondary" onClick={onClose} disabled={isSubmitting}>
              Cancel
            </Button>
            <div className="flex items-center gap-2">
              {statusMsg && <span className="text-xs text-navy-700 animate-pulse">{statusMsg}</span>}
              <Button type="submit" disabled={isSubmitting || !isMobileVerified}>
                {isSubmitting ? (
                  <span className="flex items-center gap-2">
                    <Spinner className="w-3.5 h-3.5" />
                    <span>Registering…</span>
                  </span>
                ) : (
                  "Complete Child Agent Registration"
                )}
              </Button>
            </div>
          </div>
        </form>
      </Card>
    </div>
  );
}
