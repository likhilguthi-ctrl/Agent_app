"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "./AuthGuard";
import { Card, Button, Input, RefChip, Badge, Alert, Spinner } from "./ui";
import RegistrationPhotoCapture from "./RegistrationPhotoCapture";
import IndianMobileInput, { extractTenDigits, isValidIndianMobile } from "./IndianMobileInput";
import WhatsAppVerificationSection from "./WhatsAppVerificationSection";
import EmailVerificationSection from "./EmailVerificationSection";
import CascadingLocationSelector, { LocationSelection } from "./CascadingLocationSelector";
import { uploadRegistrationImage } from "@/lib/storage";
import { fetchBackendApi } from "@/lib/backend-api";
import { extractResponseError, normalizeApiError } from "@/lib/api-error";
import {
  requestOtpNotification,
  verifyOtpNotification,
} from "@/lib/notifications";

interface CommunityOption {
  id: string;
  slug: string;
  status: string;
}

interface RegionOption {
  id: string;
  code: string;
  name: string;
  state: string;
  description?: string | null;
  status: string;
}

interface CreatedAgentResult {
  id: string;
  ref_id: string;
  first_name: string;
  last_name: string;
  username: string;
  mobile_e164: string;
  state?: string;
  effective_commission_rate: string | number;
  aadhaar_number_masked: string;
  pan_number_masked: string;
  status: string;
  community_slug: string;
  assigned_region_ids?: string[];
}

const PHONE_REGEX = /^\+[1-9]\d{7,14}$/;
const AADHAAR_REGEX = /^\d{12}$/;
const PAN_REGEX = /^[A-Z]{5}[0-9]{4}[A-Z]{1}$/;

export default function AddEmployeeForm({ initialCommunityId }: { initialCommunityId?: string }) {
  const router = useRouter();
  const { identity } = useAuth();
  const isSuperAdmin = identity?.role === "SUPER_ADMIN";
  const basePath = isSuperAdmin ? "/superadmin" : "/admin";

  // Communities (for Super Admin selector)
  const [communities, setCommunities] = useState<CommunityOption[]>([]);
  const [selectedCommunityId, setSelectedCommunityId] = useState(initialCommunityId || "");
  const [loadingCommunities, setLoadingCommunities] = useState(false);

  // Location Hierarchy Selection
  const [locationSelection, setLocationSelection] = useState<LocationSelection>({
    state: "Andhra Pradesh",
    district: "",
    mandal: "",
  });

  // Managers (for Super Admin assignment dropdown)
  const [managers, setManagers] = useState<
    Array<{ user_id: string; username: string; first_name?: string; last_name?: string }>
  >([]);
  const [selectedManagerUserId, setSelectedManagerUserId] = useState<string>("");
  const [loadingManagers, setLoadingManagers] = useState(false);

  // Geographic Scope (for Manager creating agent)
  const [managerScope, setManagerScope] = useState<{
    states: Array<{ id: string; name: string }>;
    districts: Array<{ id: string; name: string; state_id: string }>;
    all_districts_by_state: Record<string, boolean>;
    all_mandals_by_district: Record<string, boolean>;
    mandals_by_district: Record<string, Array<{ id: string; name: string }>>;
  } | null>(null);

  // Form Fields
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mobile, setMobile] = useState("");
  const [whatsappSameAsMobile, setWhatsappSameAsMobile] = useState<boolean>(true);
  const [whatsappNumber, setWhatsappNumber] = useState<string>("");
  const [whatsappVerified, setWhatsappVerified] = useState<boolean>(true);
  const [whatsappVerificationToken, setWhatsappVerificationToken] = useState<string | undefined>(undefined);
  const [email, setEmail] = useState("");
  const [emailVerified, setEmailVerified] = useState<boolean>(false);
  const [emailVerificationToken, setEmailVerificationToken] = useState<string | undefined>(undefined);
  const [address, setAddress] = useState("");
  const [commissionOverride, setCommissionOverride] = useState("");

  // OTP Verification State
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

  // Aadhaar and PAN numbers
  const [aadhaarNumber, setAadhaarNumber] = useState("");
  const [panNumber, setPanNumber] = useState("");

  // Image files
  const [profilePhoto, setProfilePhoto] = useState<File | null>(null);
  const [aadhaarDoc, setAadhaarDoc] = useState<File | null>(null);
  const [panDoc, setPanDoc] = useState<File | null>(null);
  const [uploadedKeys, setUploadedKeys] = useState<{
    profilePhotoKey: string | null;
    aadhaarDocKey: string | null;
    panDocKey: string | null;
  }>({
    profilePhotoKey: null,
    aadhaarDocKey: null,
    panDocKey: null,
  });

  // Submission State
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const [createdAgent, setCreatedAgent] = useState<CreatedAgentResult | null>(null);

  // Load communities for Super Admin
  useEffect(() => {
    if (isSuperAdmin && !initialCommunityId) {
      setLoadingCommunities(true);
      fetchBackendApi("api/v1/platform/communities?limit=100", {
        credentials: "same-origin",
      })
        .then(async (res) => {
          if (res.status === 401) {
            setFormError("Your session has expired or authentication is required. Please log in again to manage agents.");
            setSessionExpired(true);
            return;
          }
          if (!res.ok) {
            throw new Error("Failed to load communities");
          }
          const data = await res.json();
          const activeOnly = (data.items || []).filter((c: CommunityOption) => c.status === "active");
          setCommunities(activeOnly);
          if (activeOnly.length > 0 && !selectedCommunityId) {
            setSelectedCommunityId(activeOnly[0].id);
          }
        })
        .catch(() => {})
        .finally(() => setLoadingCommunities(false));
    }
  }, [isSuperAdmin, initialCommunityId, selectedCommunityId]);

  // Load managers when Super Admin selects a community
  useEffect(() => {
    const commId = isSuperAdmin ? selectedCommunityId : null;
    if (!commId) return;

    setLoadingManagers(true);
    fetchBackendApi(`api/v1/platform/communities/${commId}/managers`, {
      credentials: "same-origin",
    })
      .then(async (res) => {
        if (!res.ok) return;
        const data = await res.json();
        setManagers(data.items || []);
      })
      .catch(() => setManagers([]))
      .finally(() => setLoadingManagers(false));
  }, [isSuperAdmin, selectedCommunityId]);

  // Load manager's own geographic scope when logged in as Manager
  useEffect(() => {
    if (!isSuperAdmin) {
      fetchBackendApi("api/v1/locations/manager-scope", {
        credentials: "same-origin",
      })
        .then(async (res) => {
          if (!res.ok) return;
          const data = await res.json();
          setManagerScope(data);
        })
        .catch(() => {});
    }
  }, [isSuperAdmin]);

  const restrictedStateIds = managerScope ? managerScope.states.map((s) => s.id) : undefined;
  const restrictedDistrictIds = React.useMemo(() => {
    if (!managerScope || !locationSelection.state_id) return undefined;
    if (managerScope.all_districts_by_state[locationSelection.state_id]) return undefined;
    return managerScope.districts
      .filter((d) => d.state_id === locationSelection.state_id)
      .map((d) => d.id);
  }, [managerScope, locationSelection.state_id]);

  const restrictedMandalIds = React.useMemo(() => {
    if (!managerScope || !locationSelection.district_id) return undefined;
    if (managerScope.all_mandals_by_district[locationSelection.district_id]) return undefined;
    const mandals = managerScope.mandals_by_district[locationSelection.district_id];
    return mandals ? mandals.map((m) => m.id) : undefined;
  }, [managerScope, locationSelection.district_id]);

  // Cooldown countdown
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setInterval(() => {
      setCooldown((prev) => (prev > 0 ? prev - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [cooldown]);

  function normalizeMobile(input: string): string {
    const trimmed = input.trim();
    if (!trimmed.startsWith("+")) {
      return `+${trimmed.replace(/\D/g, "")}`;
    }
    return `+${trimmed.slice(1).replace(/\D/g, "")}`;
  }

  // Handle Request OTP for Agent Registration
  async function handleSendOtp() {
    setOtpError(null);
    setOtpFeedback(null);

    const cleanMobile = normalizeMobile(mobile);
    if (!PHONE_REGEX.test(cleanMobile)) {
      setOtpError("Mobile number must be in E.164 format (e.g., +919876543210).");
      return;
    }

    const commId = isSuperAdmin ? selectedCommunityId : identity?.tenant_id;
    setSendingOtp(true);

    try {
      const data = await requestOtpNotification({
        mobileNumber: cleanMobile,
        purpose: "agent_registration",
        communityId: commId || undefined,
      });
      setChallengeId(data.challengeId);
      setCooldown(data.cooldownSeconds);
      setOtpSent(true);
      setOtpFeedback(`Verification code sent to ${cleanMobile}.`);
    } catch (err) {
      setOtpError(normalizeApiError(err, "Failed to send verification OTP."));
    } finally {
      setSendingOtp(false);
    }
  }

  // Handle Verify OTP for Agent Registration
  async function handleVerifyOtp() {
    setOtpError(null);
    setOtpFeedback(null);

    const cleanOtp = otpCode.trim();
    if (!cleanOtp) {
      setOtpError("Please enter the 6-digit OTP.");
      return;
    }

    setVerifyingOtp(true);
    try {
      const data = await verifyOtpNotification({
        mobileNumber: normalizeMobile(mobile),
        otp: cleanOtp,
        challengeId,
        purpose: "agent_registration",
      });

      if (data.verificationToken) {
        setVerificationToken(data.verificationToken);
        setIsMobileVerified(true);
        setOtpFeedback("Mobile number successfully verified!");
      } else {
        setOtpError("Verification response did not return a valid verification token.");
      }
    } catch (err) {
      setOtpError(normalizeApiError(err, "OTP verification failed."));
    } finally {
      setVerifyingOtp(false);
    }
  }

  function resetForm() {
    setFirstName("");
    setLastName("");
    setUsername("");
    setPassword("");
    setMobile("");
    setWhatsappSameAsMobile(true);
    setWhatsappNumber("");
    setWhatsappVerified(true);
    setWhatsappVerificationToken(undefined);
    setEmail("");
    setEmailVerified(false);
    setEmailVerificationToken(undefined);
    setAddress("");
    setCommissionOverride("");
    setAadhaarNumber("");
    setPanNumber("");
    setProfilePhoto(null);
    setAadhaarDoc(null);
    setPanDoc(null);
    setUploadedKeys({
      profilePhotoKey: null,
      aadhaarDocKey: null,
      panDocKey: null,
    });
    setFormError(null);
    setSessionExpired(false);
    setUploadStatus(null);
    setCreatedAgent(null);
    setIsMobileVerified(false);
    setVerificationToken(null);
    setOtpSent(false);
    setOtpCode("");
    setChallengeId("");
    setSelectedManagerUserId("");
    setLocationSelection({
      state: "Andhra Pradesh",
      district: "",
      mandal: "",
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    if (isSubmitting) return;
    e.preventDefault();
    if (sessionExpired) {
      setFormError("Your session has expired or authentication is required. Please log in again to manage agents.");
      return;
    }
    setFormError(null);

    // Validation
    if (!firstName.trim() || !lastName.trim()) {
      setFormError("First Name and Last Name are required.");
      return;
    }
    if (!username.trim() || username.length < 3) {
      setFormError("Username must be at least 3 characters.");
      return;
    }
    if (!password.trim() || password.trim().length < 8) {
      setFormError("Initial Password is required and must be at least 8 characters.");
      return;
    }
    if (!locationSelection.state) {
      setFormError("Operating state is mandatory.");
      return;
    }
    if (!locationSelection.district) {
      setFormError("Operating district is mandatory for agent registration.");
      return;
    }
    if (!locationSelection.mandal) {
      setFormError("Operating mandal is mandatory for agent registration.");
      return;
    }
    const cleanTen = extractTenDigits(mobile);
    if (!isValidIndianMobile(cleanTen)) {
      setFormError("Valid 10-digit Indian mobile number is required (starting with 6-9).");
      return;
    }
    const cleanMobile = `+91${cleanTen}`;
    if (!isMobileVerified || !verificationToken) {
      setFormError("Mobile number must be verified via OTP before registration.");
      return;
    }

    if (!whatsappSameAsMobile && !whatsappVerified) {
      setFormError("Alternative WhatsApp number must be verified via OTP.");
      return;
    }

    if (!email.trim()) {
      setFormError("Agent email address is mandatory.");
      return;
    }
    if (!emailVerified || !emailVerificationToken) {
      setFormError("Agent email address must be verified via email OTP.");
      return;
    }
    const cleanAadhaar = aadhaarNumber.replace(/\s+/g, "");
    if (!AADHAAR_REGEX.test(cleanAadhaar)) {
      setFormError("Aadhaar Number must be exactly 12 digits.");
      return;
    }
    const cleanPan = panNumber.trim().toUpperCase();
    if (!PAN_REGEX.test(cleanPan)) {
      setFormError("PAN Number must be in format ABCDE1234F (5 uppercase letters, 4 digits, 1 letter).");
      return;
    }
    if (!profilePhoto && !uploadedKeys.profilePhotoKey) {
      setFormError("Agent Profile Photo is mandatory for registration.");
      return;
    }
    if (!aadhaarDoc && !uploadedKeys.aadhaarDocKey) {
      setFormError("Aadhaar Card document image is mandatory for registration.");
      return;
    }
    if (!panDoc && !uploadedKeys.panDocKey) {
      setFormError("PAN Card document image is mandatory for registration.");
      return;
    }
    if (isSuperAdmin && !selectedCommunityId) {
      setFormError("Please select a community for this Agent.");
      return;
    }

    const csrfToken = identity?.csrf_token || "";
    setIsSubmitting(true);

    try {
      // 1. Upload Profile Photo if not already cached
      let currentPhotoKey = uploadedKeys.profilePhotoKey;
      if (!currentPhotoKey) {
        if (!profilePhoto) {
          throw new Error("Agent Profile Photo is mandatory for registration.");
        }
        setUploadStatus("Uploading Profile Photo to secure storage…");
        const photoResult = await uploadRegistrationImage(
          profilePhoto,
          "agent-photo",
          csrfToken,
          verificationToken
        );
        if (!photoResult || !photoResult.object_key) {
          throw new Error("Profile Photo upload did not return a valid storage key.");
        }
        currentPhotoKey = photoResult.object_key;
        setUploadedKeys((prev) => ({ ...prev, profilePhotoKey: currentPhotoKey }));
      }

      // 2. Upload Aadhaar Document if not already cached
      let currentAadhaarKey = uploadedKeys.aadhaarDocKey;
      if (!currentAadhaarKey) {
        if (!aadhaarDoc) {
          throw new Error("Aadhaar Card document image is mandatory for registration.");
        }
        setUploadStatus("Uploading Aadhaar document to secure storage…");
        const aadhaarDocResult = await uploadRegistrationImage(
          aadhaarDoc,
          "aadhaar-document",
          csrfToken,
          verificationToken
        );
        if (!aadhaarDocResult || !aadhaarDocResult.object_key) {
          throw new Error("Aadhaar Document upload did not return a valid storage key.");
        }
        currentAadhaarKey = aadhaarDocResult.object_key;
        setUploadedKeys((prev) => ({ ...prev, aadhaarDocKey: currentAadhaarKey }));
      }

      // 3. Upload PAN Document if not already cached
      let currentPanKey = uploadedKeys.panDocKey;
      if (!currentPanKey) {
        if (!panDoc) {
          throw new Error("PAN Card document image is mandatory for registration.");
        }
        setUploadStatus("Uploading PAN document to secure storage…");
        const panDocResult = await uploadRegistrationImage(
          panDoc,
          "pan-document",
          csrfToken,
          verificationToken
        );
        if (!panDocResult || !panDocResult.object_key) {
          throw new Error("PAN Document upload did not return a valid storage key.");
        }
        currentPanKey = panDocResult.object_key;
        setUploadedKeys((prev) => ({ ...prev, panDocKey: currentPanKey }));
      }

      // Validate all keys exist
      if (!currentPhotoKey || !currentAadhaarKey || !currentPanKey) {
        throw new Error("All registration photos must be uploaded to secure storage before registration.");
      }

      // 4. Create Agent Record with verified R2 keys and verification token
      setUploadStatus("Finalizing Agent registration…");
      const endpoint = isSuperAdmin
        ? `api/v1/platform/communities/${selectedCommunityId}/agents`
        : "api/v1/agents";

      const payload: Record<string, unknown> = {
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        username: username.trim(),
        password: password.trim(),
        mobile_e164: cleanMobile,
        verification_token: verificationToken,
        email: email.trim(),
        email_verification_token: emailVerificationToken,
        whatsapp_same_as_mobile: whatsappSameAsMobile,
        whatsapp_number: whatsappSameAsMobile ? cleanMobile : whatsappNumber,
        whatsapp_verification_token: whatsappSameAsMobile ? null : (whatsappVerificationToken || null),
        address: address.trim() || null,
        state: locationSelection.state,
        state_id: locationSelection.state_id,
        district: locationSelection.district,
        district_id: locationSelection.district_id,
        mandal: locationSelection.mandal,
        mandal_id: locationSelection.mandal_id,
        aadhaar_number: cleanAadhaar,
        aadhaar_document_key: currentAadhaarKey,
        pan_number: cleanPan,
        pan_document_key: currentPanKey,
        profile_photo_key: currentPhotoKey,
        status: "active",
        region_ids: [],
        ...(isSuperAdmin && selectedManagerUserId ? { manager_user_id: selectedManagerUserId } : {}),
      };

      if (commissionOverride.trim()) {
        const rate = parseFloat(commissionOverride.trim());
        if (!isNaN(rate) && rate >= 0 && rate <= 100) {
          payload.commission_rate_override = rate;
        }
      }

      const agentRes = await fetchBackendApi(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          Origin: window.location.origin,
        },
        body: JSON.stringify(payload),
        credentials: "same-origin",
      });

      if (!agentRes.ok) {
        if (agentRes.status === 401) {
          setFormError("Your session has expired or authentication is required. Please log in again to manage agents.");
          setSessionExpired(true);
          setIsSubmitting(false);
          setUploadStatus(null);
          return;
        }
        const errText = await extractResponseError(
          agentRes,
          `Registration failed (HTTP ${agentRes.status})`
        );
        if (
          agentRes.status === 400 &&
          (errText.toLowerCase().includes("otp") ||
            errText.toLowerCase().includes("verification") ||
            errText.toLowerCase().includes("token"))
        ) {
          setIsMobileVerified(false);
          setVerificationToken(null);
          setUploadedKeys({ profilePhotoKey: null, aadhaarDocKey: null, panDocKey: null });
        }
        setFormError(errText);
        setIsSubmitting(false);
        setUploadStatus(null);
        return;
      }

      const created = await agentRes.json();
      setCreatedAgent(created);
      setIsSubmitting(false);
      setUploadStatus(null);
    } catch (err: unknown) {
      const msg = normalizeApiError(err, "Failed to register Agent.");
      setFormError(msg);
      setIsSubmitting(false);
      setUploadStatus(null);
    }

  }

  if (createdAgent) {
    return (
      <Card className="p-8 border-emerald-200 bg-emerald-50/20 shadow-sm space-y-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 font-bold text-xl">
            ✓
          </div>
          <div>
            <h2 className="text-xl font-bold text-slate-800">Agent Registered Successfully!</h2>
            <p className="text-sm text-slate-500">
              The Agent profile has been verified via OTP, encrypted with AES-256-GCM, and stored with R2 documents.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 bg-white p-5 rounded-xl border border-slate-200 text-sm">
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold mb-1">Permanent Reference ID</p>
            <RefChip refId={createdAgent.ref_id} />
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold mb-1">Status</p>
            <Badge tone="amber">Pending Approval</Badge>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Agent Name</p>
            <p className="font-medium text-slate-800">{createdAgent.first_name} {createdAgent.last_name}</p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Login Username</p>
            <p className="font-mono font-bold text-navy-900 text-base">{createdAgent.username}</p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Verified Mobile</p>
            <p className="font-medium text-slate-800 flex items-center gap-1.5">
              <span>{createdAgent.mobile_e164}</span>
              <span className="text-xs text-emerald-600 font-semibold">✓ Verified</span>
            </p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Community</p>
            <p className="font-medium text-slate-800">{createdAgent.community_slug}</p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Assigned State</p>
            <p className="font-medium text-slate-800">{createdAgent.state || locationSelection.state}</p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Assigned Territory</p>
            <p className="font-medium text-slate-800">
              {locationSelection.district}{locationSelection.mandal ? ` - ${locationSelection.mandal}` : ""}
            </p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">Aadhaar (Masked)</p>
            <p className="font-mono text-slate-700">{createdAgent.aadhaar_number_masked}</p>
          </div>
          <div>
            <p className="text-xs uppercase text-slate-400 font-semibold">PAN (Masked)</p>
            <p className="font-mono text-slate-700">{createdAgent.pan_number_masked}</p>
          </div>
          <div className="md:col-span-2">
            <p className="text-xs uppercase text-slate-400 font-semibold">Effective Commission Rate</p>
            <p className="text-lg font-bold text-emerald-600">{createdAgent.effective_commission_rate}%</p>
          </div>
        </div>

        <div className="rounded-lg bg-amber-50/80 border border-amber-200/80 p-4 text-xs text-amber-900 flex items-start gap-2.5">
          <span className="text-base leading-none">⏳</span>
          <div>
            <p className="font-semibold text-amber-950">Dual-Custody Governance — Pending Super Admin Approval</p>
            <p className="text-amber-800 mt-0.5 leading-relaxed">
              Login credentials for <code className="font-mono font-bold text-navy-900">{createdAgent.username}</code> have been created with the initial password. In accordance with platform governance, the agent account remains inactive until reviewed and approved by a Super Admin.
            </p>
          </div>
        </div>

        <div className="flex flex-wrap gap-3 pt-2">
          <Button onClick={resetForm}>+ Register Another Agent</Button>
          <Link href={`${basePath}/employees`}>
            <Button variant="secondary">View All Employees & Agents</Button>
          </Link>
        </div>
      </Card>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-8">
      {formError && (
        <div className="space-y-2">
          <Alert tone="red">{formError}</Alert>
          {sessionExpired && (
            <div className="flex items-center gap-2">
              <Link
                href="/login"
                className="inline-flex items-center px-4 py-2 text-sm font-medium text-white bg-navy-600 rounded-lg hover:bg-navy-700 transition"
              >
                Go to Login
              </Link>
            </div>
          )}
        </div>
      )}

      {/* Community Selection / Display */}
      {isSuperAdmin ? (
        <Card className="p-6 space-y-4">
          <h2 className="font-semibold text-slate-800 text-base">Community Assignment</h2>
          <div>
            <label htmlFor="agent-community" className="block text-sm font-medium text-slate-700 mb-1">
              Select Community <span className="text-red-500">*</span>
            </label>
            {loadingCommunities ? (
              <p className="text-xs text-slate-400">Loading active communities…</p>
            ) : (
              <select
                id="agent-community"
                value={selectedCommunityId}
                onChange={(e) => setSelectedCommunityId(e.target.value)}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-navy-600"
                required
              >
                {communities.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.slug}
                  </option>
                ))}
              </select>
            )}
            <p className="mt-1 text-xs text-slate-500">
              The Agent will be securely bound to this community.
            </p>
          </div>
        </Card>
      ) : (
        <Card className="p-6 space-y-4">
          <h2 className="font-semibold text-slate-800 text-base">Community Assignment</h2>
          <div>
            <label htmlFor="agent-community-fixed" className="block text-sm font-medium text-slate-700 mb-1">
              Assigned Community
            </label>
            <input
              id="agent-community-fixed"
              type="text"
              readOnly
              disabled
              value={identity?.tenant?.name || identity?.tenant?.slug || identity?.tenant_id || "Your Community"}
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm bg-slate-50 text-slate-700 font-medium cursor-not-allowed"
            />
            <p className="mt-1 text-xs text-slate-500">
              Agents created by a Manager are automatically assigned to your active community.
            </p>
          </div>
        </Card>
      )}

      {/* Section 1: Basic Information */}
      <Card className="p-6 space-y-4">
        <h2 className="font-semibold text-slate-800 text-base">1. Agent Details & Verification</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label htmlFor="agent-first-name" className="block text-sm font-medium text-slate-700 mb-1">
              First Name <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-first-name"
              placeholder="e.g. Ramesh"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="agent-last-name" className="block text-sm font-medium text-slate-700 mb-1">
              Last Name <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-last-name"
              placeholder="e.g. Kumar"
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="agent-username" className="block text-sm font-medium text-slate-700 mb-1">
              Account Username <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-username"
              placeholder="e.g. agent_ramesh"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="agent-password" className="block text-sm font-medium text-slate-700 mb-1">
              Initial Password <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-password"
              type="password"
              placeholder="•••••••• (minimum 8 characters)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          {/* Inline Mobile OTP Verification */}
          <div className="sm:col-span-2 rounded-xl border border-slate-200 bg-slate-50/60 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <label htmlFor="agent-mobile" className="block text-sm font-semibold text-slate-800">
                Mobile Number & Verification <span className="text-red-500">*</span>
              </label>
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
                  onChange={(e164, tenDigits, valid) => {
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
                    "Verify via OTP"
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
          </div>

          {/* WhatsApp Verification Section */}
          <div className="sm:col-span-2">
            <WhatsAppVerificationSection
              primaryMobile={mobile}
              whatsappNumber={whatsappNumber}
              sameAsMobile={whatsappSameAsMobile}
              isVerified={whatsappVerified}
              verificationToken={whatsappVerificationToken}
              communityId={isSuperAdmin ? selectedCommunityId : identity?.tenant_id}
              onChange={(d) => {
                setWhatsappNumber(d.whatsappNumber);
                setWhatsappSameAsMobile(d.sameAsMobile);
                setWhatsappVerified(d.isVerified);
                setWhatsappVerificationToken(d.verificationToken);
              }}
            />
          </div>

          {/* Mandatory Email Verification Section */}
          <div className="sm:col-span-2">
            <EmailVerificationSection
              email={email}
              isVerified={emailVerified}
              verificationToken={emailVerificationToken}
              required={true}
              communityId={isSuperAdmin ? selectedCommunityId : identity?.tenant_id}
              onChange={(d) => {
                setEmail(d.email);
                setEmailVerified(d.isVerified);
                setEmailVerificationToken(d.verificationToken);
              }}
            />
          </div>

          <div className="sm:col-span-2">
            <label htmlFor="agent-address" className="block text-sm font-medium text-slate-700 mb-1">
              Physical Address <span className="text-slate-400 font-normal">(optional)</span>
            </label>
            <Input
              id="agent-address"
              placeholder="House/Plot No., Street, City, PIN"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
          </div>
        </div>
      </Card>

      {/* Section 2: Operating Location Assignment */}
      <Card className="p-6 space-y-4">
        <div>
          <h2 className="font-semibold text-slate-800 text-base">2. Authorized Operating Location</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Assign the operating state, district, and mandal where this Agent is permitted to register subscribers. 
            Managers may only assign territories within their own authorized scope.
          </p>
        </div>

        <CascadingLocationSelector
          value={locationSelection}
          onChange={(val) => setLocationSelection(val)}
          restrictedStateIds={restrictedStateIds}
          restrictedDistrictIds={restrictedDistrictIds}
          restrictedMandalIds={restrictedMandalIds}
          required
        />
      </Card>

      {/* Optional Manager Assignment (Super Admin Only) */}
      {isSuperAdmin && (
        <Card className="p-6 space-y-3">
          <div>
            <h2 className="font-semibold text-slate-800 text-base">Reporting Manager Assignment</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Select the Community Manager this Agent will report to. Leave as &quot;Auto-allocate&quot; to assign automatically based on geographic territory.
            </p>
          </div>
          <select
            id="reporting-manager-select"
            value={selectedManagerUserId}
            onChange={(e) => setSelectedManagerUserId(e.target.value)}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-navy-600"
          >
            <option value="">(Auto-allocate based on territory)</option>
            {managers.map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {[m.first_name, m.last_name].filter(Boolean).join(" ") || m.username} (@{m.username})
              </option>
            ))}
          </select>
          {loadingManagers && <p className="text-xs text-slate-400">Loading community managers…</p>}
        </Card>
      )}

      {/* Section 3: Identity Verification & Documents */}
      <Card className="p-6 space-y-6">
        <div>
          <h2 className="font-semibold text-slate-800 text-base">3. Identity Verification & Government Documents</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Identity numbers are encrypted at rest with AES-256-GCM and masked in all regular views.
          </p>
        </div>

        {/* Profile Photo */}
        <div className="pt-2 border-t border-slate-100">
          <RegistrationPhotoCapture
            label="Agent Profile Photo"
            description="Clear front-facing photo of the agent (mandatory)."
            required={true}
            onPhotoChange={(file) => {
              setProfilePhoto(file);
              setUploadedKeys((prev) => ({ ...prev, profilePhotoKey: null }));
            }}
          />
        </div>

        {/* Aadhaar Details */}
        <div className="pt-4 border-t border-slate-100 space-y-4">
          <div>
            <label htmlFor="agent-aadhaar" className="block text-sm font-medium text-slate-700 mb-1">
              Aadhaar Number (12 Digits) <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-aadhaar"
              placeholder="123456789012"
              maxLength={12}
              value={aadhaarNumber}
              onChange={(e) => setAadhaarNumber(e.target.value.replace(/\D/g, ""))}
              required
            />
            {aadhaarNumber.length === 12 && (
              <p className="mt-1 text-xs text-emerald-600 font-mono">
                Masked storage preview: XXXXXXXX{aadhaarNumber.slice(-4)}
              </p>
            )}
          </div>
          <RegistrationPhotoCapture
            label="Aadhaar Card Document Image"
            description="Take a photo or upload scan of Aadhaar Card front (mandatory)."
            required={true}
            onPhotoChange={(file) => {
              setAadhaarDoc(file);
              setUploadedKeys((prev) => ({ ...prev, aadhaarDocKey: null }));
            }}
          />
        </div>

        {/* PAN Details */}
        <div className="pt-4 border-t border-slate-100 space-y-4">
          <div>
            <label htmlFor="agent-pan" className="block text-sm font-medium text-slate-700 mb-1">
              PAN Number (10 Characters) <span className="text-red-500">*</span>
            </label>
            <Input
              id="agent-pan"
              placeholder="ABCDE1234F"
              maxLength={10}
              value={panNumber}
              onChange={(e) => setPanNumber(e.target.value.toUpperCase())}
              required
            />
            {panNumber.length === 10 && (
              <p className="mt-1 text-xs text-emerald-600 font-mono">
                Masked storage preview: XXXXX{panNumber.slice(5, 9)}X
              </p>
            )}
          </div>
          <RegistrationPhotoCapture
            label="PAN Card Document Image"
            description="Take a photo or upload scan of PAN Card (mandatory)."
            required={true}
            onPhotoChange={(file) => {
              setPanDoc(file);
              setUploadedKeys((prev) => ({ ...prev, panDocKey: null }));
            }}
          />
        </div>
      </Card>

      {/* Section 4: Commission Override */}
      <Card className="p-6 space-y-3">
        <h2 className="font-semibold text-slate-800 text-base">4. Commission Settings</h2>
        <div>
          <label htmlFor="agent-commission" className="block text-sm font-medium text-slate-700 mb-1">
            Agent-Specific Commission Rate (%) <span className="text-slate-400 font-normal">(optional override)</span>
          </label>
          <Input
            id="agent-commission"
            type="number"
            step="0.01"
            min="0"
            max="100"
            placeholder="e.g. 12.50 (leave empty to inherit community default)"
            value={commissionOverride}
            onChange={(e) => setCommissionOverride(e.target.value)}
          />
          <p className="mt-1 text-xs text-slate-500">
            If left blank, this agent will automatically receive the community default commission rate.
          </p>
        </div>
      </Card>

      {/* Submit Buttons and Progress */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-4 pt-2">
        <Link href={`${basePath}/employees`}>
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
          <Button type="submit" disabled={isSubmitting || !isMobileVerified || sessionExpired}>
            {isSubmitting ? "Registering Agent…" : "Register Agent"}
          </Button>
        </div>
      </div>
    </form>
  );
}
