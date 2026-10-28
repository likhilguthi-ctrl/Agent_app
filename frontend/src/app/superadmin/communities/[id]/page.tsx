"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/components/AuthGuard";
import { Card, Button, Input, Badge, RefChip, Alert, Spinner } from "@/components/ui";
import RegistrationPhotoCapture from "@/components/RegistrationPhotoCapture";
import { fetchBackendApi } from "@/lib/backend-api";
import { uploadRegistrationImage, getPreviewUrl } from "@/lib/storage";
import IndianMobileInput, { extractTenDigits, isValidIndianMobile } from "@/components/IndianMobileInput";
import { requestOtpNotification, verifyOtpNotification } from "@/lib/notifications";
import WhatsAppVerificationSection from "@/components/WhatsAppVerificationSection";
import EmailVerificationSection from "@/components/EmailVerificationSection";
import StateDistrictSelector from "@/components/StateDistrictSelector";
import { SUPPORTED_STATES } from "@/lib/locations";
import ManagerLocationAssignmentSelector, { StateScopeConfig } from "@/components/ManagerLocationAssignmentSelector";
import SuperAdminHierarchyDrilldown from "@/components/SuperAdminHierarchyDrilldown";

interface DomainSummary {
  id: string;
  hostname: string;
  verification_status: string;
  is_active: boolean;
  verified_at: string | null;
}

interface CommunityDetail {
  id: string;
  slug: string;
  status: "active" | "inactive";
  created_at: string;
  updated_at: string;
  domains: DomainSummary[];
  administrators_count: number;
  photo_requirement?: string;
  default_commission_rate?: number;
  policy_version?: number;
  registration_policy?: {
    policy_version?: string | number;
    photo_requirement?: string;
    default_commission_rate?: number;
    description?: string;
  };
}

interface RegionOption {
  id: string;
  code: string;
  name: string;
  state: string;
  description?: string | null;
  status: string;
}

interface AdministratorSummary {
  user_id: string;
  username: string;
  membership_id: string;
  tenant_id: string;
  community_slug?: string | null;
  first_name?: string | null;
  last_name?: string | null;
  mobile_e164: string;
  email?: string | null;
  address?: string | null;
  state?: string | null;
  profile_photo_key?: string | null;
  aadhaar_number_masked?: string | null;
  pan_number_masked?: string | null;
  assigned_region_ids?: string[];
  assigned_region_names?: string[];
  status: "active" | "inactive";
  role: string;
  created_at: string;
}

interface AgentSummary {
  id: string;
  tenant_id: string;
  community_slug: string;
  membership_id: string;
  user_id: string;
  username: string;
  first_name: string;
  last_name: string;
  mobile_e164: string;
  email?: string | null;
  ref_id: string;
  status: string;
  effective_commission_rate: string | number;
  commission_rate_override?: string | number | null;
  profile_photo_key: string;
  aadhaar_number_masked: string;
  pan_number_masked: string;
  created_at: string;
}

interface CommissionAuditSummary {
  id: string;
  tenant_id: string;
  agent_id?: string | null;
  agent_ref_id?: string | null;
  changed_by_user_id: string;
  old_rate?: string | number | null;
  new_rate: string | number;
  reason: string;
  created_at: string;
}

const PHONE_REGEX = /^\+[1-9]\d{7,14}$/;
const AADHAAR_REGEX = /^\d{12}$/;
const PAN_REGEX = /^[A-Z]{5}[0-9]{4}[A-Z]{1}$/;

export default function CommunityDetailPage() {
  const params = useParams();
  const router = useRouter();
  const { identity } = useAuth();
  const communityId = params.id as string;

  const [community, setCommunity] = useState<CommunityDetail | null>(null);
  const [administrators, setAdministrators] = useState<AdministratorSummary[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [auditLogs, setAuditLogs] = useState<CommissionAuditSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [successBanner, setSuccessBanner] = useState<string | null>(null);

  // Status toggle confirmation modal
  const [showStatusModal, setShowStatusModal] = useState(false);
  const [isUpdatingStatus, setIsUpdatingStatus] = useState(false);

  // Add Administrator modal
  const [showAddAdminModal, setShowAddAdminModal] = useState(false);
  const [adminFirstName, setAdminFirstName] = useState("");
  const [adminLastName, setAdminLastName] = useState("");
  const [adminUsername, setAdminUsername] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminMobile, setAdminMobile] = useState("");
  const [adminMobileValid, setAdminMobileValid] = useState(false);
  const [adminMobileOtp, setAdminMobileOtp] = useState("");
  const [adminMobileChallengeId, setAdminMobileChallengeId] = useState<string | null>(null);
  const [adminMobileVerified, setAdminMobileVerified] = useState(false);
  const [adminMobileToken, setAdminMobileToken] = useState<string | undefined>(undefined);
  const [adminMobileSendingOtp, setAdminMobileSendingOtp] = useState(false);
  const [adminMobileVerifyingOtp, setAdminMobileVerifyingOtp] = useState(false);
  const [adminMobileCooldown, setAdminMobileCooldown] = useState(0);
  const [adminMobileOtpSent, setAdminMobileOtpSent] = useState(false);
  const [adminMobileFeedback, setAdminMobileFeedback] = useState<string | null>(null);
  const [adminMobileOtpError, setAdminMobileOtpError] = useState<string | null>(null);
  const [adminWhatsappSameAsMobile, setAdminWhatsappSameAsMobile] = useState(true);
  const [adminWhatsappNumber, setAdminWhatsappNumber] = useState("");
  const [adminWhatsappVerified, setAdminWhatsappVerified] = useState(true);
  const [adminWhatsappToken, setAdminWhatsappToken] = useState<string | undefined>(undefined);
  const [adminEmail, setAdminEmail] = useState("");
  const [adminEmailVerified, setAdminEmailVerified] = useState(false);
  const [adminEmailToken, setAdminEmailToken] = useState<string | undefined>(undefined);
  const [adminAddress, setAdminAddress] = useState("");
  const [adminState, setAdminState] = useState("Andhra Pradesh");
  const [adminProfilePhoto, setAdminProfilePhoto] = useState<File | null>(null);
  const [adminAadhaarNumber, setAdminAadhaarNumber] = useState("");
  const [adminAadhaarDoc, setAdminAadhaarDoc] = useState<File | null>(null);
  const [adminPanNumber, setAdminPanNumber] = useState("");
  const [adminPanDoc, setAdminPanDoc] = useState<File | null>(null);
  const [adminStatus, setAdminStatus] = useState<"active" | "inactive">("active");
  const [adminLocationScopes, setAdminLocationScopes] = useState<StateScopeConfig[]>([]);
  const [adminSelectedStateNames, setAdminSelectedStateNames] = useState<string[]>(["Andhra Pradesh"]);
  const [adminFormError, setAdminFormError] = useState<string | null>(null);
  const [adminUploadStatus, setAdminUploadStatus] = useState<string | null>(null);
  const [isCreatingAdmin, setIsCreatingAdmin] = useState(false);

  // Admin status toggle
  const [adminToToggle, setAdminToToggle] = useState<AdministratorSummary | null>(null);
  const [isUpdatingAdminStatus, setIsUpdatingAdminStatus] = useState(false);

  // View Administrator Profile Modal
  const [adminProfileToView, setAdminProfileToView] = useState<AdministratorSummary | null>(null);
  const [detailedAdminProfile, setDetailedAdminProfile] = useState<AdministratorSummary | null>(null);

  const handleOpenAdminProfile = async (admin: AdministratorSummary) => {
    setAdminProfileToView(admin);
    setDetailedAdminProfile(admin);
    try {
      const res = await fetchBackendApi(
        `api/v1/platform/communities/${communityId}/administrators/${admin.user_id}`,
        {
          credentials: "same-origin",
          cache: "no-store",
        }
      );
      if (res.ok) {
        const data = await res.json();
        setDetailedAdminProfile(data);
      }
    } catch {
      // fallback to basic summary
    }
  };

  // Photo Policy Setting
  const [photoPolicy, setPhotoPolicy] = useState<"OPTIONAL" | "REQUIRED">("OPTIONAL");
  const [isSavingPhotoPolicy, setIsSavingPhotoPolicy] = useState(false);

  // Default Commission Setting
  const [commissionRate, setCommissionRate] = useState<string>("10.00");
  const [commissionReason, setCommissionReason] = useState<string>("Community rate update");
  const [isSavingCommission, setIsSavingCommission] = useState(false);

  // Agent Commission Override Modal
  const [agentToOverride, setAgentToOverride] = useState<AgentSummary | null>(null);
  const [agentOverrideRate, setAgentOverrideRate] = useState<string>("");
  const [agentOverrideReason, setAgentOverrideReason] = useState<string>("Performance adjustment");
  const [isSavingAgentOverride, setIsSavingAgentOverride] = useState(false);

  const loadData = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [commRes, adminsRes, agentsRes, logsRes] = await Promise.all([
        fetchBackendApi(`api/v1/platform/communities/${communityId}`, {
          credentials: "same-origin",
          cache: "no-store",
        }),
        fetchBackendApi(`api/v1/platform/communities/${communityId}/administrators`, {
          credentials: "same-origin",
          cache: "no-store",
        }),
        fetchBackendApi(`api/v1/platform/communities/${communityId}/agents`, {
          credentials: "same-origin",
          cache: "no-store",
        }),
        fetchBackendApi(`api/v1/platform/communities/${communityId}/commission/audit-logs`, {
          credentials: "same-origin",
          cache: "no-store",
        }),
      ]);

      if (!commRes.ok) {
        if (commRes.status === 404) {
          setError("Community not found.");
        } else {
          setError("Failed to load community details.");
        }
        setIsLoading(false);
        return;
      }

      const commData: CommunityDetail = await commRes.json();
      setCommunity(commData);

      const pol = commData.photo_requirement || commData.registration_policy?.photo_requirement || "OPTIONAL";
      setPhotoPolicy(pol === "REQUIRED" ? "REQUIRED" : "OPTIONAL");

      const defComm = commData.default_commission_rate ?? commData.registration_policy?.default_commission_rate ?? 10.0;
      setCommissionRate(String(defComm));

      if (adminsRes.ok) {
        const adminsData = await adminsRes.json();
        setAdministrators(adminsData.items || []);
      }

      if (agentsRes.ok) {
        const agentsData = await agentsRes.json();
        setAgents(agentsData.items || []);
      }

      if (logsRes.ok) {
        const logsData = await logsRes.json();
        setAuditLogs(logsData.items || []);
      }
    } catch {
      setError("An error occurred while loading community data.");
    } finally {
      setIsLoading(false);
    }
  }, [communityId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleToggleCommunityStatus = async () => {
    if (!community || !identity?.csrf_token) return;
    setIsUpdatingStatus(true);
    const newStatus = community.status === "active" ? "inactive" : "active";

    try {
      const res = await fetchBackendApi(`api/v1/platform/communities/${communityId}`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: JSON.stringify({ status: newStatus }),
      });

      if (res.ok) {
        const updated: CommunityDetail = await res.json();
        setCommunity(updated);
        setShowStatusModal(false);
        setSuccessBanner(`Community status changed to ${newStatus.toUpperCase()}.`);
      } else {
        setError(`Failed to update community status (${res.status}).`);
      }
    } catch {
      setError("Network error while updating community status.");
    } finally {
      setIsUpdatingStatus(false);
    }
  };

  const handleSavePhotoPolicy = async (newPolicy: "OPTIONAL" | "REQUIRED") => {
    if (!identity?.csrf_token) return;
    setIsSavingPhotoPolicy(true);
    setError(null);
    try {
      const res = await fetchBackendApi(`api/v1/platform/communities/${communityId}/photo-policy`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: JSON.stringify({ photo_requirement: newPolicy }),
      });

      if (res.ok) {
        setPhotoPolicy(newPolicy);
        setSuccessBanner(`Subscriber registration photo policy set to ${newPolicy}.`);
      } else {
        setError("Failed to update photo policy.");
      }
    } catch {
      setError("Network error while updating photo policy.");
    } finally {
      setIsSavingPhotoPolicy(false);
    }
  };

  const handleSaveDefaultCommission = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!identity?.csrf_token) return;
    const rate = parseFloat(commissionRate);
    if (isNaN(rate) || rate < 0 || rate > 100) {
      setError("Commission rate must be a valid number between 0% and 100%.");
      return;
    }

    setIsSavingCommission(true);
    setError(null);
    try {
      const res = await fetchBackendApi(`api/v1/platform/communities/${communityId}/commission`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: JSON.stringify({
          default_commission_rate: rate,
          reason: commissionReason.trim() || "Community commission rate updated by Super Admin",
        }),
      });

      if (res.ok) {
        setSuccessBanner(`Default community commission rate updated to ${rate.toFixed(2)}%.`);
        loadData(); // reload audit logs and effective rates
      } else {
        setError("Failed to update default commission rate.");
      }
    } catch {
      setError("Network error while updating commission rate.");
    } finally {
      setIsSavingCommission(false);
    }
  };

  const handleSaveAgentOverride = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentToOverride || !identity?.csrf_token) return;
    let overrideRate: number | null = null;
    if (agentOverrideRate.trim() !== "") {
      const parsed = parseFloat(agentOverrideRate.trim());
      if (isNaN(parsed) || parsed < 0 || parsed > 100) {
        setError("Agent override commission must be between 0% and 100%.");
        return;
      }
      overrideRate = parsed;
    }

    setIsSavingAgentOverride(true);
    setError(null);
    try {
      const res = await fetchBackendApi(`api/v1/agents/${agentToOverride.id}`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: JSON.stringify({
          commission_rate_override: overrideRate,
        }),
      });

      if (res.ok) {
        setSuccessBanner(
          overrideRate !== null
            ? `Agent commission override set to ${overrideRate.toFixed(2)}%.`
            : `Agent override removed. Inheriting community default rate.`
        );
        setAgentToOverride(null);
        loadData();
      } else {
        setError("Failed to update agent commission override.");
      }
    } catch {
      setError("Network error while updating agent commission.");
    } finally {
      setIsSavingAgentOverride(false);
    }
  };

  // Admin Mobile OTP Cooldown timer
  useEffect(() => {
    if (adminMobileCooldown <= 0) return;
    const timer = setInterval(() => {
      setAdminMobileCooldown((c) => Math.max(0, c - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [adminMobileCooldown]);

  const handleSendAdminMobileOtp = async () => {
    setAdminMobileOtpError(null);
    setAdminMobileFeedback(null);
    const tenDigits = extractTenDigits(adminMobile);
    if (!isValidIndianMobile(tenDigits)) {
      setAdminMobileOtpError("Valid 10-digit Indian mobile number is required.");
      return;
    }
    const cleanMobile = `+91${tenDigits}`;
    setAdminMobileSendingOtp(true);
    try {
      const data = await requestOtpNotification({
        mobileNumber: cleanMobile,
        purpose: "admin_registration",
        communityId,
      });
      setAdminMobileChallengeId(data.challengeId);
      setAdminMobileCooldown(data.cooldownSeconds || 60);
      setAdminMobileOtpSent(true);
      setAdminMobileFeedback(`Verification code sent to ${cleanMobile}`);
    } catch (err: unknown) {
      setAdminMobileOtpError(err instanceof Error ? err.message : "Failed to send mobile OTP.");
    } finally {
      setAdminMobileSendingOtp(false);
    }
  };

  const handleVerifyAdminMobileOtp = async () => {
    setAdminMobileOtpError(null);
    setAdminMobileFeedback(null);
    const cleanOtp = adminMobileOtp.trim();
    if (!cleanOtp || cleanOtp.length !== 6) {
      setAdminMobileOtpError("Please enter the 6-digit verification code.");
      return;
    }
    if (!adminMobileChallengeId) {
      setAdminMobileOtpError("Please request an OTP first.");
      return;
    }
    const tenDigits = extractTenDigits(adminMobile);
    const cleanMobile = `+91${tenDigits}`;
    setAdminMobileVerifyingOtp(true);
    try {
      const data = await verifyOtpNotification({
        mobileNumber: cleanMobile,
        otp: cleanOtp,
        challengeId: adminMobileChallengeId,
        purpose: "admin_registration",
        communityId,
      });
      if (data.verificationToken) {
        setAdminMobileToken(data.verificationToken);
        setAdminMobileVerified(true);
        setAdminMobileFeedback("Mobile number successfully verified!");
      } else {
        setAdminMobileOtpError("Verification did not return a valid verification token.");
      }
    } catch (err: unknown) {
      setAdminMobileOtpError(err instanceof Error ? err.message : "Mobile OTP verification failed.");
    } finally {
      setAdminMobileVerifyingOtp(false);
    }
  };

  const handleOpenAddAdminModal = () => {
    setAdminFirstName("");
    setAdminLastName("");
    setAdminUsername("");
    setAdminPassword("");
    setAdminMobile("");
    setAdminMobileValid(false);
    setAdminMobileOtp("");
    setAdminMobileChallengeId(null);
    setAdminMobileVerified(false);
    setAdminMobileToken(undefined);
    setAdminMobileSendingOtp(false);
    setAdminMobileVerifyingOtp(false);
    setAdminMobileCooldown(0);
    setAdminMobileOtpSent(false);
    setAdminMobileFeedback(null);
    setAdminMobileOtpError(null);
    setAdminWhatsappSameAsMobile(true);
    setAdminWhatsappNumber("");
    setAdminWhatsappVerified(true);
    setAdminWhatsappToken(undefined);
    setAdminEmail("");
    setAdminEmailVerified(false);
    setAdminEmailToken(undefined);
    setAdminAddress("");
    setAdminState("Andhra Pradesh");
    setAdminAadhaarNumber("");
    setAdminPanNumber("");
    setAdminProfilePhoto(null);
    setAdminAadhaarDoc(null);
    setAdminPanDoc(null);
    setAdminStatus("active");
    setAdminFormError(null);
    setAdminUploadStatus(null);
    setShowAddAdminModal(true);
  };

  const handleCreateAdmin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!identity?.csrf_token) return;
    setAdminFormError(null);

    const cleanFirstName = adminFirstName.trim();
    const cleanLastName = adminLastName.trim();
    const cleanUsername = adminUsername.trim().toLowerCase();
    const tenDigits = extractTenDigits(adminMobile);
    const cleanAadhaar = adminAadhaarNumber.replace(/\D/g, "");
    const cleanPan = adminPanNumber.trim().toUpperCase();

    if (!cleanFirstName || !cleanLastName) {
      setAdminFormError("First Name and Last Name are required.");
      return;
    }
    if (!cleanUsername || cleanUsername.length < 3) {
      setAdminFormError("Username must be at least 3 characters.");
      return;
    }
    if (!adminPassword || adminPassword.length < 8) {
      setAdminFormError("Password must be at least 8 characters.");
      return;
    }
    if (!isValidIndianMobile(tenDigits)) {
      setAdminFormError("Valid 10-digit Indian mobile number is required (starting with 6-9).");
      return;
    }
    const cleanMobile = `+91${tenDigits}`;

    // Mobile OTP verification check (mandatory for Admin)
    if (!adminMobileVerified || !adminMobileToken) {
      setAdminFormError("Admin mobile number must be verified with an OTP before registration.");
      return;
    }

    // WhatsApp verification check
    if (!adminWhatsappSameAsMobile && !adminWhatsappVerified) {
      setAdminFormError("Alternative WhatsApp number must be verified with an OTP.");
      return;
    }

    // Email verification check (mandatory for Admin)
    if (!adminEmail.trim()) {
      setAdminFormError("Email address is mandatory for Administrator registration.");
      return;
    }
    if (!adminEmailVerified || !adminEmailToken) {
      setAdminFormError("Email address must be verified with an email OTP.");
      return;
    }

    if (!adminState) {
      setAdminFormError("Operating State is mandatory (Andhra Pradesh or Telangana).");
      return;
    }
    if (!AADHAAR_REGEX.test(cleanAadhaar)) {
      setAdminFormError("Aadhaar Number must be exactly 12 digits.");
      return;
    }
    if (!PAN_REGEX.test(cleanPan)) {
      setAdminFormError("PAN Number must follow format ABCDE1234F.");
      return;
    }
    if (!adminProfilePhoto) {
      setAdminFormError("Administrator profile photo is required.");
      return;
    }
    if (!adminAadhaarDoc) {
      setAdminFormError("Aadhaar Card document image is required.");
      return;
    }
    if (!adminPanDoc) {
      setAdminFormError("PAN Card document image is required.");
      return;
    }

    setIsCreatingAdmin(true);
    const csrfToken = identity.csrf_token;

    try {
      setAdminUploadStatus("Uploading Administrator profile photo…");
      const photoRes = await uploadRegistrationImage(adminProfilePhoto, "agent-photo", csrfToken);
      if (!photoRes || !photoRes.object_key) {
        throw new Error("Failed to upload profile photo to secure storage.");
      }

      setAdminUploadStatus("Uploading Aadhaar document…");
      const aadhaarRes = await uploadRegistrationImage(adminAadhaarDoc, "aadhaar-document", csrfToken);
      if (!aadhaarRes || !aadhaarRes.object_key) {
        throw new Error("Failed to upload Aadhaar document to secure storage.");
      }

      setAdminUploadStatus("Uploading PAN document…");
      const panRes = await uploadRegistrationImage(adminPanDoc, "pan-document", csrfToken);
      if (!panRes || !panRes.object_key) {
        throw new Error("Failed to upload PAN document to secure storage.");
      }

      setAdminUploadStatus("Finalizing manager provisioning…");
      const res = await fetchBackendApi(`api/v1/platform/communities/${communityId}/managers`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          first_name: cleanFirstName,
          last_name: cleanLastName,
          username: cleanUsername,
          password: adminPassword,
          mobile_e164: cleanMobile,
          whatsapp_number: adminWhatsappSameAsMobile ? cleanMobile : adminWhatsappNumber,
          whatsapp_same_as_mobile: adminWhatsappSameAsMobile,
          whatsapp_verification_token: adminWhatsappSameAsMobile ? null : (adminWhatsappToken || null),
          email: adminEmail.trim(),
          email_verification_token: adminEmailToken,
          address: adminAddress.trim() || null,
          state: adminSelectedStateNames[0] || adminState,
          states: adminSelectedStateNames,
          location_scopes: adminLocationScopes,
          aadhaar_number: cleanAadhaar,
          aadhaar_document_key: aadhaarRes.object_key,
          pan_number: cleanPan,
          pan_document_key: panRes.object_key,
          profile_photo_key: photoRes.object_key,
          verification_token: adminMobileToken,
          status: "active",
        }),
      });

      if (!res.ok) {
        let msg = "Failed to create manager.";
        try {
          const json = await res.json();
          msg = json?.error?.message || json?.detail?.message || `Error (${res.status})`;
        } catch {
          // ignore
        }
        setAdminFormError(msg);
        setIsCreatingAdmin(false);
        setAdminUploadStatus(null);
        return;
      }

      const newAdmin: AdministratorSummary = await res.json();
      setAdministrators((prev) => [newAdmin, ...prev]);
      setShowAddAdminModal(false);
      setSuccessBanner(`Manager "${newAdmin.username}" created and immediately activated for ${community?.slug || "community"}.`);
    } catch (err: unknown) {
      setAdminFormError(err instanceof Error ? err.message : "Failed to provision administrator.");
    } finally {
      setIsCreatingAdmin(false);
      setAdminUploadStatus(null);
    }
  };

  const handleToggleAdminStatus = async () => {
    if (!adminToToggle || !identity?.csrf_token) return;
    setIsUpdatingAdminStatus(true);
    const newStatus = adminToToggle.status === "active" ? "inactive" : "active";

    try {
      const res = await fetchBackendApi(
        `api/v1/platform/communities/${communityId}/administrators/${adminToToggle.user_id}`,
        {
          method: "PATCH",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": identity.csrf_token,
          },
          body: JSON.stringify({ status: newStatus }),
        }
      );

      if (res.ok) {
        const updated: AdministratorSummary = await res.json();
        setAdministrators((prev) =>
          prev.map((a) => (a.user_id === updated.user_id ? updated : a))
        );
        setAdminToToggle(null);
        setSuccessBanner(`Administrator "${updated.username}" status set to ${newStatus.toUpperCase()}.`);
      } else {
        setError(`Failed to update administrator status (${res.status}).`);
      }
    } catch {
      setError("Network error while updating administrator status.");
    } finally {
      setIsUpdatingAdminStatus(false);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="h-4 w-48 bg-slate-200 animate-pulse rounded" />
        <div className="h-8 w-64 bg-slate-200 animate-pulse rounded" />
        <Card className="p-8">
          <div className="space-y-4">
            <div className="h-4 w-3/4 bg-slate-100 animate-pulse rounded" />
            <div className="h-4 w-1/2 bg-slate-100 animate-pulse rounded" />
          </div>
        </Card>
      </div>
    );
  }

  if (error || !community) {
    return (
      <div className="space-y-4">
        <nav className="flex items-center gap-2 text-xs font-medium text-slate-500">
          <Link href="/superadmin/communities" className="hover:text-navy-800 transition">
            Communities
          </Link>
          <span>/</span>
          <span className="text-slate-800">Error</span>
        </nav>
        <Card className="p-8 text-center">
          <p className="text-base font-semibold text-red-700">{error || "Community not found."}</p>
          <div className="mt-4">
            <Link href="/superadmin/communities">
              <Button variant="secondary">Back to Communities</Button>
            </Link>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb Navigation */}
      <nav className="flex items-center gap-2 text-xs font-medium text-slate-500">
        <Link href="/superadmin/dashboard" className="hover:text-navy-800 transition">
          Dashboard
        </Link>
        <span>/</span>
        <Link href="/superadmin/communities" className="hover:text-navy-800 transition">
          Communities
        </Link>
        <span>/</span>
        <span className="text-slate-800 font-semibold">{community.slug}</span>
      </nav>

      {/* Success Notification */}
      {successBanner && (
        <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-4 text-sm text-emerald-800 flex justify-between items-center shadow-sm">
          <span>{successBanner}</span>
          <button
            onClick={() => setSuccessBanner(null)}
            className="text-emerald-600 hover:text-emerald-900 font-bold ml-4"
          >
            ✕
          </button>
        </div>
      )}

      {/* Page Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold font-display text-navy-900 tracking-tight">
              {community.slug}
            </h1>
            <Badge tone={community.status === "active" ? "emerald" : "amber"}>
              {community.status.toUpperCase()}
            </Badge>
          </div>
          <p className="text-xs font-mono text-slate-500 mt-1">ID: {community.id}</p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant={community.status === "active" ? "danger" : "secondary"}
            onClick={() => setShowStatusModal(true)}
          >
            {community.status === "active" ? "Deactivate Community" : "Activate Community"}
          </Button>
        </div>
      </div>

      {/* Top Grid: Overview & Configurable Policies */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Community Overview */}
        <Card className="p-6">
          <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 mb-4">
            Community Overview
          </h2>
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-xs text-slate-500 font-medium">Tenant Slug</dt>
              <dd className="font-semibold text-navy-900 mt-0.5">{community.slug}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500 font-medium">Lifecycle Status</dt>
              <dd className="mt-0.5">
                <span
                  className={`inline-flex items-center gap-1.5 text-xs font-semibold ${
                    community.status === "active" ? "text-emerald-700" : "text-amber-700"
                  }`}
                >
                  <span
                    className={`w-2 h-2 rounded-full ${
                      community.status === "active" ? "bg-emerald-500" : "bg-amber-500"
                    }`}
                  />
                  {community.status.toUpperCase()}
                </span>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500 font-medium">Created Timestamp</dt>
              <dd className="text-slate-700 mt-0.5">
                {new Date(community.created_at).toLocaleString()}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500 font-medium">Last Modified</dt>
              <dd className="text-slate-700 mt-0.5">
                {new Date(community.updated_at).toLocaleString()}
              </dd>
            </div>
          </dl>
        </Card>

        {/* Configurable Community Policies Card */}
        <Card className="p-6 border-indigo-100 bg-gradient-to-br from-white to-slate-50 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700">
              Registration & Commission Policies
            </h2>
            <span className="text-[10px] font-semibold tracking-wider uppercase px-2 py-0.5 rounded bg-emerald-100 text-emerald-800">
              Active Contract
            </span>
          </div>

          {/* Photo Policy Toggle */}
          <div className="p-4 bg-white rounded-xl border border-slate-200 space-y-2">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-semibold text-slate-800">Subscriber Photo Requirement</p>
                <p className="text-xs text-slate-500">
                  Enforces whether Agents must capture subscriber photos during user registration.
                </p>
              </div>
              <Badge tone={photoPolicy === "REQUIRED" ? "emerald" : "slate"}>
                {photoPolicy}
              </Badge>
            </div>
            <div className="flex items-center gap-2 pt-2">
              <Button
                size="sm"
                variant={photoPolicy === "OPTIONAL" ? "primary" : "secondary"}
                disabled={isSavingPhotoPolicy || photoPolicy === "OPTIONAL"}
                onClick={() => handleSavePhotoPolicy("OPTIONAL")}
              >
                Set Optional
              </Button>
              <Button
                size="sm"
                variant={photoPolicy === "REQUIRED" ? "primary" : "secondary"}
                disabled={isSavingPhotoPolicy || photoPolicy === "REQUIRED"}
                onClick={() => handleSavePhotoPolicy("REQUIRED")}
              >
                Set Mandatory (Required)
              </Button>
            </div>
          </div>

          {/* Default Commission Rate Form */}
          <form onSubmit={handleSaveDefaultCommission} className="p-4 bg-white rounded-xl border border-slate-200 space-y-3">
            <div>
              <p className="text-sm font-semibold text-slate-800">Default Agent Commission Rate (%)</p>
              <p className="text-xs text-slate-500">
                Inherited by all agents in {community.slug} unless an agent-specific override is configured.
              </p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <div>
                <label className="text-[11px] uppercase text-slate-400 font-semibold block mb-0.5">Rate (%)</label>
                <Input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  value={commissionRate}
                  onChange={(e) => setCommissionRate(e.target.value)}
                  required
                />
              </div>
              <div className="sm:col-span-2">
                <label className="text-[11px] uppercase text-slate-400 font-semibold block mb-0.5">Change Reason (Audit)</label>
                <Input
                  value={commissionReason}
                  onChange={(e) => setCommissionReason(e.target.value)}
                  placeholder="e.g. Q4 Incentive Revision"
                  required
                />
              </div>
            </div>
            <div className="flex justify-end pt-1">
              <Button type="submit" size="sm" disabled={isSavingCommission}>
                {isSavingCommission ? "Updating…" : "Update Commission Rate"}
              </Button>
            </div>
          </form>
        </Card>
      </div>

      {/* 3-Level Organizational Hierarchy Drill-Down (Level 1 Managers -> Level 2 Agents -> Level 3 Subscribers) */}
      <SuperAdminHierarchyDrilldown communityId={communityId} communitySlug={community.slug} />

      {/* Community Agents Roster */}
      <Card className="p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5">
          <div>
            <h2 className="text-base font-bold font-display text-navy-900">
              Community Field Agents ({agents.length})
            </h2>
            <p className="text-xs text-slate-600 mt-0.5">
              Field agents registered to {community.slug} with verified credentials, documents, and commission rates.
            </p>
          </div>
          <Link href={`/superadmin/employees/add?communityId=${communityId}`}>
            <Button size="sm">+ Register Agent</Button>
          </Link>
        </div>

        {agents.length === 0 ? (
          <div className="text-center py-8 border border-dashed border-slate-200 rounded-xl">
            <p className="text-sm font-medium text-slate-600">No agents registered in this community yet.</p>
            <p className="text-xs text-slate-400 mt-1">
              Add field agents to begin subscriber onboardings and track commissions.
            </p>
            <div className="mt-4">
              <Link href={`/superadmin/employees/add?communityId=${communityId}`}>
                <Button size="sm">+ Register First Agent</Button>
              </Link>
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-lg">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold text-xs uppercase">
                <tr>
                  <th className="px-4 py-3">Agent</th>
                  <th className="px-4 py-3">Reference ID</th>
                  <th className="px-4 py-3">Contact</th>
                  <th className="px-4 py-3">Aadhaar / PAN</th>
                  <th className="px-4 py-3">Commission</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {agents.map((ag) => (
                  <tr key={ag.id} className="hover:bg-slate-50 transition">
                    <td className="px-4 py-3">
                      <p className="font-semibold text-navy-900">{ag.first_name} {ag.last_name}</p>
                      <p className="text-xs font-mono text-slate-400">{ag.username}</p>
                    </td>
                    <td className="px-4 py-3">
                      <RefChip refId={ag.ref_id} />
                    </td>
                    <td className="px-4 py-3 text-xs">
                      <p className="font-medium text-slate-700">{ag.mobile_e164}</p>
                      {ag.email && <p className="text-slate-400 truncate max-w-xs">{ag.email}</p>}
                    </td>
                    <td className="px-4 py-3 text-xs font-mono text-slate-600">
                      <div>Aadhaar: {ag.aadhaar_number_masked}</div>
                      <div>PAN: {ag.pan_number_masked}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <span className="font-bold text-emerald-600">{ag.effective_commission_rate}%</span>
                        {ag.commission_rate_override !== null && (
                          <Badge tone="navy">Override</Badge>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={ag.status === "active" ? "emerald" : ag.status === "pending" ? "amber" : "slate"}>
                        {ag.status.toUpperCase()}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Link href={`/superadmin/employees/${ag.id}`}>
                          <Button size="sm" variant="outline">
                            View Profile
                          </Button>
                        </Link>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => {
                            setAgentToOverride(ag);
                            setAgentOverrideRate(
                              ag.commission_rate_override !== null ? String(ag.commission_rate_override) : ""
                            );
                          }}
                        >
                          Adjust Commission
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Community Managers Section */}
      <Card className="p-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-5">
          <div>
            <h2 className="text-base font-bold font-display text-navy-900">
              Community Managers ({administrators.length})
            </h2>
            <p className="text-xs text-slate-600 mt-0.5">
              Managers assigned to this tenant hold managerial and agent oversight privileges strictly isolated to {community.slug}.
            </p>
          </div>
          <Button size="sm" onClick={handleOpenAddAdminModal}>
            + Add Manager
          </Button>
        </div>

        {administrators.length === 0 ? (
          <div className="text-center py-8 border border-dashed border-slate-200 rounded-xl">
            <p className="text-sm font-medium text-slate-600">No managers provisioned yet.</p>
            <p className="text-xs text-slate-400 mt-1">
              Add at least one manager to delegate community operations safely.
            </p>
            <div className="mt-4">
              <Button size="sm" onClick={handleOpenAddAdminModal}>
                + Provision Manager
              </Button>
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto border border-slate-200 rounded-lg">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold text-xs uppercase">
                <tr>
                  <th className="px-4 py-3">Manager</th>
                  <th className="px-4 py-3">Contact</th>
                  <th className="px-4 py-3">Operating Territory</th>
                  <th className="px-4 py-3">Aadhaar / PAN</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Provisioned Date</th>
                  <th className="px-4 py-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {administrators.map((admin) => (
                  <tr key={admin.user_id} className="hover:bg-slate-50 transition">
                    <td className="px-4 py-3">
                      <p className="font-semibold text-navy-900">
                        {admin.first_name || admin.last_name
                          ? `${admin.first_name || ""} ${admin.last_name || ""}`.trim()
                          : admin.username}
                      </p>
                      <p className="text-xs font-mono text-slate-400">@{admin.username}</p>
                    </td>
                    <td className="px-4 py-3 text-xs">
                      <p className="font-medium text-slate-700">{admin.mobile_e164}</p>
                      {admin.email && <p className="text-slate-400 truncate max-w-xs">{admin.email}</p>}
                    </td>
                    <td className="px-4 py-3 text-xs">
                      <p className="font-medium text-slate-800">{admin.state || "Andhra Pradesh"}</p>
                      <p className="text-slate-400">
                        {admin.assigned_region_names && admin.assigned_region_names.length > 0
                          ? `${admin.assigned_region_names.length} ${admin.assigned_region_names.length === 1 ? "District" : "Districts"}`
                          : "All Districts"}
                      </p>
                    </td>
                    <td className="px-4 py-3 text-xs font-mono text-slate-600">
                      <div>Aadhaar: {admin.aadhaar_number_masked || "—"}</div>
                      <div>PAN: {admin.pan_number_masked || "—"}</div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={admin.status === "active" ? "emerald" : "amber"}>
                        {admin.status.toUpperCase()}
                      </Badge>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500">
                      {new Date(admin.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => handleOpenAdminProfile(admin)}
                        >
                          View Profile
                        </Button>
                        <Button
                          size="sm"
                          variant={admin.status === "active" ? "danger" : "secondary"}
                          onClick={() => setAdminToToggle(admin)}
                        >
                          {admin.status === "active" ? "Deactivate" : "Activate"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Commission Audit History */}
      {auditLogs.length > 0 && (
        <Card className="p-6">
          <h2 className="text-base font-bold font-display text-navy-900 mb-4">
            Commission Rate Audit Trail
          </h2>
          <div className="overflow-x-auto border border-slate-200 rounded-lg">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 border-b border-slate-200 text-slate-600 uppercase font-semibold">
                <tr>
                  <th className="px-4 py-2.5">Timestamp</th>
                  <th className="px-4 py-2.5">Scope</th>
                  <th className="px-4 py-2.5">Rate Change</th>
                  <th className="px-4 py-2.5">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 font-mono">
                {auditLogs.map((log) => (
                  <tr key={log.id} className="hover:bg-slate-50">
                    <td className="px-4 py-2.5 text-slate-500">{new Date(log.created_at).toLocaleString()}</td>
                    <td className="px-4 py-2.5 text-slate-700 font-sans">
                      {log.agent_ref_id ? `Agent (${log.agent_ref_id})` : "Community Default"}
                    </td>
                    <td className="px-4 py-2.5 font-bold text-navy-900">
                      {log.old_rate !== null ? `${log.old_rate}%` : "Default"} → {log.new_rate}%
                    </td>
                    <td className="px-4 py-2.5 text-slate-600 font-sans">{log.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* Agent Override Commission Modal */}
      {agentToOverride && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-md w-full p-6 shadow-xl">
            <div className="flex items-center justify-between pb-3 border-b border-slate-100">
              <div>
                <h3 className="text-lg font-bold font-display text-navy-900">
                  Agent Commission Override
                </h3>
                <p className="text-xs text-slate-500">
                  Agent: <span className="font-semibold text-navy-800">{agentToOverride.first_name} {agentToOverride.last_name}</span> ({agentToOverride.ref_id})
                </p>
              </div>
              <button
                type="button"
                onClick={() => setAgentToOverride(null)}
                className="text-slate-400 hover:text-slate-600 text-lg font-bold"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveAgentOverride} className="space-y-4 mt-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-1">
                  Commission Rate (%)
                </label>
                <Input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  placeholder={`Leave blank to inherit community default (${commissionRate}%)`}
                  value={agentOverrideRate}
                  onChange={(e) => setAgentOverrideRate(e.target.value)}
                />
                <p className="mt-1 text-xs text-slate-500">
                  Enter a percentage to override the community default, or clear it to revert to default.
                </p>
              </div>

              <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
                <Button
                  type="button"
                  variant="secondary"
                  disabled={isSavingAgentOverride}
                  onClick={() => setAgentToOverride(null)}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={isSavingAgentOverride}>
                  {isSavingAgentOverride ? "Saving…" : "Save Override"}
                </Button>
              </div>
            </form>
          </Card>
        </div>
      )}

      {/* Community Status Toggle Confirmation Modal */}
      {showStatusModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-md w-full p-6 shadow-xl">
            <h3 className="text-lg font-bold font-display text-navy-900">
              {community.status === "active" ? "Deactivate Community?" : "Activate Community?"}
            </h3>
            <p className="text-sm text-slate-600 mt-2">
              {community.status === "active"
                ? `Deactivating "${community.slug}" will immediately revoke all active administrator and member sessions in PostgreSQL. Historical records, configurations, and accounts will be safely preserved.`
                : `Activating "${community.slug}" will allow authorized community administrators and subscribers to access their services again.`}
            </p>
            <div className="flex items-center justify-end gap-3 mt-6">
              <Button
                variant="secondary"
                disabled={isUpdatingStatus}
                onClick={() => setShowStatusModal(false)}
              >
                Cancel
              </Button>
              <Button
                variant={community.status === "active" ? "danger" : "primary"}
                disabled={isUpdatingStatus}
                onClick={handleToggleCommunityStatus}
              >
                {isUpdatingStatus
                  ? "Updating..."
                  : community.status === "active"
                  ? "Confirm Deactivation"
                  : "Confirm Activation"}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Administrator Status Toggle Confirmation Modal */}
      {adminToToggle && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-md w-full p-6 shadow-xl">
            <h3 className="text-lg font-bold font-display text-navy-900">
              {adminToToggle.status === "active" ? "Deactivate Administrator?" : "Activate Administrator?"}
            </h3>
            <p className="text-sm text-slate-600 mt-2">
              {adminToToggle.status === "active"
                ? `Deactivating administrator "${adminToToggle.username}" will immediately revoke their active sessions in PostgreSQL.`
                : `Activating administrator "${adminToToggle.username}" will restore their ability to log in and manage this community.`}
            </p>
            <div className="flex items-center justify-end gap-3 mt-6">
              <Button
                variant="secondary"
                disabled={isUpdatingAdminStatus}
                onClick={() => setAdminToToggle(null)}
              >
                Cancel
              </Button>
              <Button
                variant={adminToToggle.status === "active" ? "danger" : "primary"}
                disabled={isUpdatingAdminStatus}
                onClick={handleToggleAdminStatus}
              >
                {isUpdatingAdminStatus
                  ? "Updating..."
                  : adminToToggle.status === "active"
                  ? "Confirm Deactivation"
                  : "Confirm Activation"}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Add Manager Modal */}
      {showAddAdminModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-3xl w-full p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between pb-3 border-b border-slate-100">
              <div>
                <h3 className="text-lg font-bold font-display text-navy-900">
                  Provision Community Manager
                </h3>
                <p className="text-xs text-slate-500">
                  Tenant Community: <span className="font-semibold text-navy-800">{community.slug}</span>
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowAddAdminModal(false)}
                className="text-slate-400 hover:text-slate-600 text-lg font-bold"
              >
                ✕
              </button>
            </div>

            {adminFormError && (
              <div className="rounded-lg bg-red-50 border border-red-200 p-3 my-3 text-xs text-red-700">
                {adminFormError}
              </div>
            )}

            <form onSubmit={handleCreateAdmin} className="space-y-6 mt-4">
              {/* Section 1: Manager Details & Credentials */}
              <div className="space-y-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 pb-1 border-b border-slate-100">
                  1. Manager Details & Credentials
                </h4>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      First Name <span className="text-red-500">*</span>
                    </label>
                    <Input
                      required
                      placeholder="e.g. Suresh"
                      value={adminFirstName}
                      onChange={(e) => setAdminFirstName(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Last Name <span className="text-red-500">*</span>
                    </label>
                    <Input
                      required
                      placeholder="e.g. Reddy"
                      value={adminLastName}
                      onChange={(e) => setAdminLastName(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Account Username <span className="text-red-500">*</span>
                    </label>
                    <Input
                      required
                      placeholder="e.g. admin_suresh"
                      value={adminUsername}
                      onChange={(e) => setAdminUsername(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Initial Password <span className="text-red-500">*</span>
                    </label>
                    <Input
                      type="password"
                      required
                      placeholder="•••••••• (minimum 8 characters)"
                      value={adminPassword}
                      onChange={(e) => setAdminPassword(e.target.value)}
                    />
                  </div>
                  <div className="sm:col-span-2">
                    <IndianMobileInput
                      label="Mobile Phone (India)"
                      required
                      value={adminMobile}
                      onChange={(e164, tenDigits, valid) => {
                        setAdminMobile(e164);
                        setAdminMobileValid(valid);
                        setAdminMobileVerified(false);
                        setAdminMobileToken(undefined);
                        setAdminMobileOtp("");
                        setAdminMobileOtpSent(false);
                        setAdminMobileFeedback(null);
                        setAdminMobileOtpError(null);
                        if (adminWhatsappSameAsMobile) {
                          setAdminWhatsappNumber(e164);
                        }
                      }}
                    />

                    {/* Mobile OTP Verification Controls */}
                    <div className="mt-2 space-y-2">
                      {!adminMobileVerified ? (
                        <div className="space-y-2">
                          <div className="flex items-center gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={handleSendAdminMobileOtp}
                              disabled={adminMobileSendingOtp || adminMobileCooldown > 0 || !isValidIndianMobile(extractTenDigits(adminMobile))}
                              className="text-xs"
                            >
                              {adminMobileSendingOtp ? (
                                <span className="flex items-center gap-1.5">
                                  <Spinner className="w-3.5 h-3.5" /> Sending OTP…
                                </span>
                              ) : adminMobileCooldown > 0 ? (
                                `Resend in ${adminMobileCooldown}s`
                              ) : adminMobileOtpSent ? (
                                "Resend Mobile OTP"
                              ) : (
                                "Send Mobile OTP"
                              )}
                            </Button>
                          </div>

                          {adminMobileOtpSent && (
                            <div className="flex items-center gap-2 mt-2">
                              <input
                                type="text"
                                inputMode="numeric"
                                maxLength={6}
                                placeholder="Enter 6-digit OTP"
                                value={adminMobileOtp}
                                onChange={(e) => setAdminMobileOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                                className="w-40 px-3 py-1.5 text-sm border border-slate-300 rounded-md focus:outline-none focus:ring-1 focus:ring-emerald-500 font-mono tracking-widest text-center"
                              />
                              <Button
                                type="button"
                                size="sm"
                                onClick={handleVerifyAdminMobileOtp}
                                disabled={adminMobileVerifyingOtp || adminMobileOtp.length !== 6}
                                className="bg-emerald-600 hover:bg-emerald-700 text-white text-xs px-3 py-1.5"
                              >
                                {adminMobileVerifyingOtp ? "Verifying…" : "Verify OTP"}
                              </Button>
                            </div>
                          )}

                          {adminMobileFeedback && (
                            <p className="text-xs text-emerald-600 font-medium">{adminMobileFeedback}</p>
                          )}
                          {adminMobileOtpError && (
                            <p className="text-xs text-red-600 font-medium">{adminMobileOtpError}</p>
                          )}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 px-3 py-1.5 rounded-lg w-fit">
                          <svg className="w-4 h-4 text-emerald-600" fill="currentColor" viewBox="0 0 20 20">
                            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                          </svg>
                          <span>Mobile Number Verified</span>
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="sm:col-span-2">
                    <WhatsAppVerificationSection
                      primaryMobile={adminMobile}
                      whatsappNumber={adminWhatsappNumber}
                      sameAsMobile={adminWhatsappSameAsMobile}
                      isVerified={adminWhatsappVerified}
                      verificationToken={adminWhatsappToken}
                      communityId={communityId}
                      onChange={(d) => {
                        setAdminWhatsappNumber(d.whatsappNumber);
                        setAdminWhatsappSameAsMobile(d.sameAsMobile);
                        setAdminWhatsappVerified(d.isVerified);
                        setAdminWhatsappToken(d.verificationToken);
                      }}
                    />
                  </div>

                  <div className="sm:col-span-2">
                    <EmailVerificationSection
                      email={adminEmail}
                      isVerified={adminEmailVerified}
                      verificationToken={adminEmailToken}
                      required={true}
                      communityId={communityId}
                      onChange={(d) => {
                        setAdminEmail(d.email);
                        setAdminEmailVerified(d.isVerified);
                        setAdminEmailToken(d.verificationToken);
                      }}
                    />
                  </div>

                  <div className="sm:col-span-2">
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Physical Address <span className="text-slate-400 font-normal">(optional)</span>
                    </label>
                    <Input
                      placeholder="Street, City, PIN code"
                      value={adminAddress}
                      onChange={(e) => setAdminAddress(e.target.value)}
                    />
                  </div>
                </div>
              </div>

              {/* Section 2: Authorized Operating Location Scope */}
              <div className="space-y-4 pt-2">
                <div className="pb-1 border-b border-slate-100 flex items-center justify-between">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    2. Manager Geographic Territory & Scope
                  </h4>
                </div>

                <ManagerLocationAssignmentSelector
                  onChange={(scopes, names) => {
                    setAdminLocationScopes(scopes);
                    setAdminSelectedStateNames(names);
                    if (names.length > 0) setAdminState(names[0]);
                  }}
                />
              </div>

              {/* Section 3: Identity Verification & Government Documents */}
              <div className="space-y-4 pt-2">
                <h4 className="text-xs font-bold uppercase tracking-wider text-slate-500 pb-1 border-b border-slate-100">
                  3. Identity Verification & Government Documents
                </h4>
                <div className="space-y-4">
                  <RegistrationPhotoCapture
                    label="Manager Profile Photo"
                    description="Clear front-facing profile photo (mandatory)."
                    required={true}
                    onPhotoChange={(file) => setAdminProfilePhoto(file)}
                  />

                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      Aadhaar Number (12 Digits) <span className="text-red-500">*</span>
                    </label>
                    <Input
                      required
                      placeholder="123456789012"
                      maxLength={12}
                      value={adminAadhaarNumber}
                      onChange={(e) => setAdminAadhaarNumber(e.target.value.replace(/\D/g, ""))}
                    />
                    {adminAadhaarNumber.length === 12 && (
                      <p className="mt-1 text-xs text-emerald-600 font-mono">
                        Masked preview: XXXXXXXX{adminAadhaarNumber.slice(-4)}
                      </p>
                    )}
                  </div>

                  <RegistrationPhotoCapture
                    label="Aadhaar Card Document Image"
                    description="Front scan or photo of Aadhaar card (mandatory)."
                    required={true}
                    onPhotoChange={(file) => setAdminAadhaarDoc(file)}
                  />

                  <div>
                    <label className="block text-xs font-semibold text-slate-700 mb-1">
                      PAN Card Number (10 alphanumeric uppercase) <span className="text-red-500">*</span>
                    </label>
                    <Input
                      required
                      placeholder="ABCDE1234F"
                      maxLength={10}
                      value={adminPanNumber}
                      onChange={(e) => setAdminPanNumber(e.target.value.toUpperCase())}
                    />
                    {PAN_REGEX.test(adminPanNumber.trim()) && (
                      <p className="mt-1 text-xs text-emerald-600 font-mono">
                        Masked preview: XXXXX{adminPanNumber.slice(-4)}
                      </p>
                    )}
                  </div>

                  <RegistrationPhotoCapture
                    label="PAN Card Document Image"
                    description="Front scan or photo of PAN card (mandatory)."
                    required={true}
                    onPhotoChange={(file) => setAdminPanDoc(file)}
                  />
                </div>
              </div>

              {/* Section 4: Governance Callout */}
              <div className="rounded-lg bg-emerald-50/80 border border-emerald-200/80 p-3.5 text-xs text-emerald-950 flex items-start gap-2.5">
                <span className="text-base leading-none">⚡</span>
                <div>
                  <p className="font-semibold text-emerald-900">Immediate Activation Protocol</p>
                  <p className="text-emerald-800 mt-0.5 leading-relaxed">
                    Administrators created directly by a Super Admin are provisioned and activated immediately. Account credentials, encrypted identity records, and regional permissions take effect upon submission without secondary appointment review.
                  </p>
                </div>
              </div>

              {/* Form Actions */}
              <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-100">
                <Button
                  type="button"
                  variant="secondary"
                  disabled={isCreatingAdmin}
                  onClick={() => setShowAddAdminModal(false)}
                >
                  Cancel
                </Button>
                <Button type="submit" disabled={isCreatingAdmin}>
                  {isCreatingAdmin ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="w-3.5 h-3.5" />
                      <span>{adminUploadStatus || "Provisioning Administrator..."}</span>
                    </span>
                  ) : (
                    "Provision & Activate Administrator"
                  )}
                </Button>
              </div>
            </form>
          </Card>
        </div>
      )}

      {/* View Administrator Profile Modal */}
      {adminProfileToView && detailedAdminProfile && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
          <Card className="max-w-lg w-full p-6 shadow-2xl animate-in fade-in zoom-in-95">
            <div className="flex items-start justify-between pb-4 border-b border-slate-100">
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 rounded-full overflow-hidden bg-navy-100 flex items-center justify-center border border-slate-200 shrink-0">
                  {detailedAdminProfile.profile_photo_key ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={getPreviewUrl(detailedAdminProfile.profile_photo_key)}
                      alt={detailedAdminProfile.username}
                      className="w-full h-full object-cover"
                      onError={(e) => {
                        (e.target as HTMLElement).style.display = "none";
                      }}
                    />
                  ) : (
                    <span className="text-xl">👤</span>
                  )}
                </div>
                <div>
                  <h3 className="text-lg font-bold font-display text-navy-900">
                    {detailedAdminProfile.first_name || detailedAdminProfile.last_name
                      ? `${detailedAdminProfile.first_name || ""} ${detailedAdminProfile.last_name || ""}`.trim()
                      : detailedAdminProfile.username}
                  </h3>
                  <p className="text-xs text-slate-500 font-mono">
                    @{detailedAdminProfile.username}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setAdminProfileToView(null);
                  setDetailedAdminProfile(null);
                }}
                className="text-slate-400 hover:text-slate-600 text-lg font-bold p-1"
                aria-label="Close"
              >
                ✕
              </button>
            </div>

            <div className="py-4 space-y-4 text-sm">
              <div className="grid grid-cols-2 gap-3">
                <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 block mb-1">
                    Status
                  </span>
                  <Badge tone={detailedAdminProfile.status === "active" ? "emerald" : "amber"}>
                    {detailedAdminProfile.status.toUpperCase()}
                  </Badge>
                </div>

                <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 block mb-1">
                    Role & Scope
                  </span>
                  <span className="text-xs font-bold text-navy-800 uppercase">
                    {detailedAdminProfile.role} • {community.slug}
                  </span>
                </div>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between py-1.5 border-b border-slate-100">
                  <span className="text-slate-500 font-medium">Mobile Phone (E.164)</span>
                  <span className="font-mono text-slate-800 font-semibold">{detailedAdminProfile.mobile_e164}</span>
                </div>
                <div className="flex justify-between py-1.5 border-b border-slate-100">
                  <span className="text-slate-500 font-medium">Email Address</span>
                  <span className="text-slate-800">{detailedAdminProfile.email || "Not provided"}</span>
                </div>
                <div className="flex justify-between py-1.5 border-b border-slate-100">
                  <span className="text-slate-500 font-medium">Assigned Community</span>
                  <span className="font-semibold text-slate-800">{community.slug}</span>
                </div>
                <div className="flex justify-between py-1.5 border-b border-slate-100">
                  <span className="text-slate-500 font-medium">Assigned State</span>
                  <span className="font-semibold text-slate-800">{detailedAdminProfile.state || "Andhra Pradesh"}</span>
                </div>
                <div className="py-1.5 border-b border-slate-100">
                  <span className="text-slate-500 font-medium block mb-1">Assigned Regions</span>
                  {detailedAdminProfile.assigned_region_names && detailedAdminProfile.assigned_region_names.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5 mt-1">
                      {detailedAdminProfile.assigned_region_names.map((rn, idx) => (
                        <span key={idx} className="bg-navy-50 text-navy-700 px-2 py-0.5 rounded text-[11px] font-medium border border-navy-100">
                          {rn}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-slate-500 italic">All Community Regions</span>
                  )}
                </div>
                {detailedAdminProfile.address && (
                  <div className="py-1.5 border-b border-slate-100">
                    <span className="text-slate-500 font-medium block mb-0.5">Physical Address</span>
                    <span className="text-slate-700">{detailedAdminProfile.address}</span>
                  </div>
                )}
                {(detailedAdminProfile.aadhaar_number_masked || detailedAdminProfile.pan_number_masked) && (
                  <div className="flex justify-between py-1.5 border-b border-slate-100">
                    <span className="text-slate-500 font-medium">Identity Documents</span>
                    <span className="font-mono text-slate-700">
                      {detailedAdminProfile.aadhaar_number_masked ? `Aadhaar: ${detailedAdminProfile.aadhaar_number_masked} ` : ""}
                      {detailedAdminProfile.pan_number_masked ? `PAN: ${detailedAdminProfile.pan_number_masked}` : ""}
                    </span>
                  </div>
                )}
                <div className="flex justify-between py-1.5">
                  <span className="text-slate-500 font-medium">Provisioned On</span>
                  <span className="text-slate-700">{new Date(detailedAdminProfile.created_at).toLocaleString()}</span>
                </div>
              </div>

              <div className="rounded-lg bg-sand-50/60 border border-slate-200/60 p-3 text-[11px] text-slate-500 leading-relaxed">
                🔒 <strong>RBAC Policy Enforced:</strong> Sensitive credentials (passwords, hashes, and OTP challenges) are strictly withheld server-side.
              </div>
            </div>

            <div className="flex justify-end pt-3 border-t border-slate-100">
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setAdminProfileToView(null);
                  setDetailedAdminProfile(null);
                }}
              >
                Close Profile
              </Button>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
