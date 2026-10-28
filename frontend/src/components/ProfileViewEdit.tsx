"use client";

import React, { useEffect, useState, useRef } from "react";
import Link from "next/link";
import { useAuth } from "@/components/AuthGuard";
import { Card, Badge, Button, Spinner, Alert } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { getPreviewUrl, uploadRegistrationImage } from "@/lib/storage";
import { extractResponseError } from "@/lib/api-error";
import { Icon, icons } from "@/components/icons";

export interface AssignedLocation {
  assignment_id: string;
  state_id?: number | null;
  state_name?: string | null;
  district_id?: number | null;
  district_name?: string | null;
  mandal_id?: number | null;
  mandal_name?: string | null;
}

export interface HierarchyPerson {
  user_id?: string | null;
  membership_id?: string | null;
  name?: string | null;
  ref_id?: string | null;
  mobile_masked?: string | null;
}

export interface SubscriberPlanInfo {
  subscription_id?: string | null;
  plan_id?: string | null;
  plan_name?: string | null;
  plan_price?: number | string | null;
  currency?: string | null;
  plan_duration_days?: number | null;
  subscription_status?: string | null;
  starts_at?: string | null;
  expires_at?: string | null;
  payment_order_id?: string | null;
  payment_status?: string | null;
  amount_paid?: number | string | null;
}

export interface ProfileData {
  profile_id: string;
  role: string;
  target_model: string;
  first_name?: string | null;
  last_name?: string | null;
  full_name?: string | null;
  mobile_e164?: string | null;
  mobile_masked?: string | null;
  email?: string | null;
  whatsapp_number?: string | null;
  address?: string | null;
  profile_photo_key?: string | null;
  status: string;
  tenant_id?: string | null;
  community_slug?: string | null;
  community_name?: string | null;
  ref_id?: string | null;
  membership_id?: string | null;
  user_id?: string | null;
  created_at?: string | null;
  assigned_locations?: AssignedLocation[];
  state_id?: number | null;
  state_name?: string | null;
  district_id?: number | null;
  district_name?: string | null;
  mandal_id?: number | null;
  mandal_name?: string | null;
  manager?: HierarchyPerson | null;
  parent_agent?: HierarchyPerson | null;
  registering_agent?: HierarchyPerson | null;
  subscriber_plan?: SubscriberPlanInfo | null;
  can_edit: boolean;
  editable_fields: string[];
}

export interface ProfileViewEditProps {
  targetProfileId?: string;
  title?: string;
  subtitle?: string;
  backHref?: string;
  onProfileUpdated?: (updated: ProfileData) => void;
}

export default function ProfileViewEdit({
  targetProfileId,
  title,
  subtitle,
  backHref,
  onProfileUpdated,
}: ProfileViewEditProps) {
  const { identity } = useAuth();
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [photoError, setPhotoError] = useState(false);

  // Form edit states
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [whatsappNumber, setWhatsappNumber] = useState("");
  const [address, setAddress] = useState("");
  const [photoKey, setPhotoKey] = useState("");
  const [uploadingPhoto, setUploadingPhoto] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Mobile Change Modal State
  const [showMobileModal, setShowMobileModal] = useState(false);
  const [newMobile, setNewMobile] = useState("");
  const [otpCode, setOtpCode] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [sendingOtp, setSendingOtp] = useState(false);
  const [verifyingOtp, setVerifyingOtp] = useState(false);
  const [mobileError, setMobileError] = useState<string | null>(null);
  const [verifiedToken, setVerifiedToken] = useState<string | null>(null);
  const [pendingVerifiedMobile, setPendingVerifiedMobile] = useState<string | null>(null);

  const isSelf = !targetProfileId || targetProfileId === "me";
  const apiEndpoint = isSelf
    ? "api/v1/profile/me"
    : `api/v1/profile/${encodeURIComponent(targetProfileId)}`;

  async function loadProfile() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchBackendApi(apiEndpoint, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) {
        const msg = await extractResponseError(res, "Failed to load profile.");
        setError(msg);
        setLoading(false);
        return;
      }
      const data: ProfileData = await res.json();
      setProfile(data);
      initializeForm(data);
      setLoading(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Network error loading profile.");
      setLoading(false);
    }
  }

  function initializeForm(data: ProfileData) {
    setFirstName(data.first_name || "");
    setLastName(data.last_name || "");
    setFullName(data.full_name || (data.first_name ? `${data.first_name} ${data.last_name || ""}`.trim() : ""));
    setEmail(data.email || "");
    setWhatsappNumber(data.whatsapp_number || "");
    setAddress(data.address || "");
    setPhotoKey(data.profile_photo_key || "");
    setPhotoError(false);
    setVerifiedToken(null);
    setPendingVerifiedMobile(null);
  }

  useEffect(() => {
    void loadProfile();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetProfileId]);

  function handleStartEdit() {
    if (!profile) return;
    initializeForm(profile);
    setIsEditing(true);
    setSuccessMsg(null);
    setError(null);
  }

  function handleCancelEdit() {
    if (profile) initializeForm(profile);
    setIsEditing(false);
    setError(null);
  }

  // Handle Photo File Upload
  async function handlePhotoFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadingPhoto(true);
    setError(null);
    try {
      const category = profile?.role === "MEMBER" ? "user-photo" : "agent-photo";
      const res = await uploadRegistrationImage(file, category, identity.csrf_token || "");
      setPhotoKey(res.object_key);
      setPhotoError(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to upload photo.");
    } finally {
      setUploadingPhoto(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  // Mobile Change Handlers
  async function handleRequestMobileOtp() {
    setMobileError(null);
    const cleanMobile = newMobile.trim();
    if (!cleanMobile) {
      setMobileError("Please enter a valid 10-digit mobile number.");
      return;
    }
    const formatted = cleanMobile.startsWith("+") ? cleanMobile : `+91${cleanMobile.replace(/\D/g, "").slice(-10)}`;
    if (formatted.length !== 13) {
      setMobileError("Please enter a valid 10-digit Indian mobile number.");
      return;
    }

    setSendingOtp(true);
    try {
      const res = await fetchBackendApi("api/v1/profile/mobile-change/request", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token || "",
          Origin: window.location.origin,
        },
        body: JSON.stringify({
          mobile_e164: formatted,
          target_profile_id: isSelf ? undefined : profile?.profile_id,
        }),
        credentials: "same-origin",
      });
      if (!res.ok) {
        const msg = await extractResponseError(res, "Failed to send OTP.");
        setMobileError(msg);
      } else {
        setOtpSent(true);
      }
    } catch (err: unknown) {
      setMobileError(err instanceof Error ? err.message : "Failed to send OTP.");
    } finally {
      setSendingOtp(false);
    }
  }

  async function handleVerifyMobileOtp() {
    setMobileError(null);
    const cleanOtp = otpCode.trim();
    if (!cleanOtp || cleanOtp.length < 4) {
      setMobileError("Please enter the OTP received.");
      return;
    }
    const cleanMobile = newMobile.trim();
    const formatted = cleanMobile.startsWith("+") ? cleanMobile : `+91${cleanMobile.replace(/\D/g, "").slice(-10)}`;

    setVerifyingOtp(true);
    try {
      const res = await fetchBackendApi("api/v1/profile/mobile-change/verify", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token || "",
          Origin: window.location.origin,
        },
        body: JSON.stringify({
          mobile_e164: formatted,
          otp_code: cleanOtp,
        }),
        credentials: "same-origin",
      });
      if (!res.ok) {
        const msg = await extractResponseError(res, "Failed to verify OTP.");
        setMobileError(msg);
      } else {
        const data = await res.json();
        setVerifiedToken(data.verification_token);
        setPendingVerifiedMobile(formatted);
        setShowMobileModal(false);
        setOtpSent(false);
        setOtpCode("");
        setNewMobile("");
        setSuccessMsg(`Mobile number ${formatted} successfully verified! Click "Save Changes" to apply.`);
      }
    } catch (err: unknown) {
      setMobileError(err instanceof Error ? err.message : "Failed to verify OTP.");
    } finally {
      setVerifyingOtp(false);
    }
  }

  // Save Profile Changes
  async function handleSaveProfile() {
    if (!profile) return;
    setSaving(true);
    setError(null);
    setSuccessMsg(null);

    const payload: Record<string, unknown> = {};

    if (profile.editable_fields.includes("first_name") && firstName !== (profile.first_name || "")) {
      payload.first_name = firstName.trim();
    }
    if (profile.editable_fields.includes("last_name") && lastName !== (profile.last_name || "")) {
      payload.last_name = lastName.trim();
    }
    if (profile.editable_fields.includes("full_name") && fullName !== (profile.full_name || "")) {
      payload.full_name = fullName.trim();
    }
    if (profile.editable_fields.includes("email") && email !== (profile.email || "")) {
      payload.email = email.trim() || null;
    }
    if (profile.editable_fields.includes("whatsapp_number") && whatsappNumber !== (profile.whatsapp_number || "")) {
      payload.whatsapp_number = whatsappNumber.trim() || null;
    }
    if (profile.editable_fields.includes("address") && address !== (profile.address || "")) {
      payload.address = address.trim() || null;
    }
    if (profile.editable_fields.includes("profile_photo_key") && photoKey !== (profile.profile_photo_key || "")) {
      payload.profile_photo_key = photoKey.trim() || null;
    }

    if (pendingVerifiedMobile && verifiedToken) {
      payload.mobile_e164 = pendingVerifiedMobile;
      payload.mobile_verification_token = verifiedToken;
    }

    if (Object.keys(payload).length === 0) {
      setIsEditing(false);
      setSaving(false);
      return;
    }

    try {
      const res = await fetchBackendApi(apiEndpoint, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token || "",
          Origin: window.location.origin,
        },
        body: JSON.stringify(payload),
        credentials: "same-origin",
      });

      if (!res.ok) {
        const msg = await extractResponseError(res, "Failed to update profile.");
        setError(msg);
      } else {
        const updated: ProfileData = await res.json();
        setProfile(updated);
        initializeForm(updated);
        setIsEditing(false);
        setSuccessMsg("Profile updated successfully.");
        onProfileUpdated?.(updated);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Network error updating profile.");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center p-12">
        <Spinner className="w-8 h-8 text-emerald-600" />
        <span className="ml-3 text-sm text-slate-500 font-medium">Loading profile details...</span>
      </div>
    );
  }

  if (error && !profile) {
    return (
      <Card className="p-8 max-w-2xl mx-auto">
        <div className="mb-4">
          <Alert tone="red">
            {error}
          </Alert>
        </div>
        {backHref && (
          <Link href={backHref}>
            <Button variant="secondary">Go Back</Button>
          </Link>
        )}
      </Card>
    );
  }

  if (!profile) return null;

  const currentPhotoKey = isEditing ? photoKey : profile.profile_photo_key;
  const previewUrl = currentPhotoKey ? getPreviewUrl(currentPhotoKey) : null;
  const displayRole =
    profile.role === "SUPER_ADMIN"
      ? "Super Admin"
      : profile.role === "ADMIN"
      ? "Manager"
      : profile.role === "CHILD_AGENT"
      ? "Child Agent"
      : profile.role === "AGENT"
      ? "Agent"
      : profile.role === "MEMBER"
      ? "Subscriber"
      : profile.role;

  return (
    <div className="space-y-6 max-w-5xl mx-auto">
      {backHref && (
        <div>
          <Link href={backHref} className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-navy-800 transition-colors">
            ← Back
          </Link>
        </div>
      )}

      {/* Top Header & Overview */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-slate-200 pb-5">
        <div>
          <h1 className="font-display text-2xl font-bold text-navy-800">
            {title || (isSelf ? "My Profile" : `${profile.first_name || profile.full_name || "User"}'s Profile`)}
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            {subtitle || "View and manage identity, contact details, assigned territory, and role permissions."}
          </p>
        </div>

        {profile.can_edit && (
          <div className="flex items-center gap-3">
            {isEditing ? (
              <>
                <Button variant="secondary" onClick={handleCancelEdit} disabled={saving}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleSaveProfile} disabled={saving}>
                  {saving ? (
                    <span className="flex items-center gap-2">
                      <Spinner className="w-4 h-4" /> Saving...
                    </span>
                  ) : (
                    "Save Changes"
                  )}
                </Button>
              </>
            ) : (
              <Button variant="primary" onClick={handleStartEdit} className="inline-flex items-center gap-2">
                <Icon path={icons.edit} />
                Edit Profile
              </Button>
            )}
          </div>
        )}
      </div>

      {/* Alerts */}
      {successMsg && (
        <div className="rounded-lg bg-emerald-50 border border-emerald-200 p-4 text-sm text-emerald-800 flex items-center justify-between">
          <span>{successMsg}</span>
          <button onClick={() => setSuccessMsg(null)} className="text-emerald-600 hover:text-emerald-900 font-bold ml-3">✕</button>
        </div>
      )}
      {error && (
        <div className="mb-2">
          <Alert tone="red">
            {error}
          </Alert>
        </div>
      )}

      {/* Profile Card */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column: Photo & Core Identifiers */}
        <Card className="p-6 flex flex-col items-center text-center">
          <div className="relative w-28 h-28 rounded-full bg-slate-100 border-2 border-slate-200 overflow-hidden flex items-center justify-center shadow-inner">
            {!photoError && previewUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={previewUrl}
                alt="Profile photo"
                className="w-full h-full object-cover"
                onError={() => setPhotoError(true)}
              />
            ) : (
              <span className="text-5xl text-slate-400 select-none">👤</span>
            )}

            {isEditing && profile.editable_fields.includes("profile_photo_key") && (
              <label className="absolute inset-0 bg-black/40 hover:bg-black/50 text-white flex flex-col items-center justify-center cursor-pointer transition-colors text-xs font-medium">
                <span>{uploadingPhoto ? "Uploading..." : "Change Photo"}</span>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  className="hidden"
                  onChange={handlePhotoFileSelected}
                  disabled={uploadingPhoto}
                />
              </label>
            )}
          </div>

          <h2 className="mt-4 font-display text-lg font-semibold text-navy-800">
            {profile.full_name || `${profile.first_name || ""} ${profile.last_name || ""}`.trim() || "User Profile"}
          </h2>

          <div className="mt-2 flex flex-wrap gap-2 justify-center">
            <Badge tone="emerald">{displayRole}</Badge>
            <Badge tone={profile.status === "active" ? "emerald" : "amber"}>
              {profile.status.toUpperCase()}
            </Badge>
          </div>

          {profile.community_name && (
            <p className="mt-3 text-xs text-slate-500 font-medium">
              Community: <span className="text-navy-700 font-semibold">{profile.community_name}</span> ({profile.community_slug})
            </p>
          )}

          <div className="mt-6 w-full border-t border-slate-100 pt-4 text-left space-y-2 text-xs">
            {profile.ref_id && (
              <div className="flex justify-between py-1 border-b border-slate-50">
                <span className="text-slate-500">Ref ID:</span>
                <span className="font-mono font-medium text-slate-800">{profile.ref_id}</span>
              </div>
            )}
            {profile.membership_id && (
              <div className="flex justify-between py-1 border-b border-slate-50">
                <span className="text-slate-500">Membership:</span>
                <span className="font-mono text-slate-700 truncate max-w-[150px]" title={profile.membership_id}>
                  {profile.membership_id.slice(0, 10)}...
                </span>
              </div>
            )}
            {profile.user_id && (
              <div className="flex justify-between py-1 border-b border-slate-50">
                <span className="text-slate-500">User ID:</span>
                <span className="font-mono text-slate-700 truncate max-w-[150px]" title={profile.user_id}>
                  {profile.user_id.slice(0, 10)}...
                </span>
              </div>
            )}
            {profile.created_at && (
              <div className="flex justify-between py-1">
                <span className="text-slate-500">Registered:</span>
                <span className="text-slate-700">
                  {new Date(profile.created_at).toLocaleDateString("en-IN", {
                    day: "numeric",
                    month: "short",
                    year: "numeric",
                  })}
                </span>
              </div>
            )}
          </div>
        </Card>

        {/* Middle & Right Column: Personal & Contact Information */}
        <div className="lg:col-span-2 space-y-6">
          <Card className="p-6">
            <h3 className="text-sm font-semibold text-navy-800 uppercase tracking-wider mb-4 border-b border-slate-100 pb-2">
              Personal & Contact Information
            </h3>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {/* Operator Full Name */}
              {profile.editable_fields.includes("full_name") && (
                <div className="sm:col-span-2">
                  <label className="block text-xs font-medium text-slate-600 mb-1">Full Name</label>
                  {isEditing ? (
                    <input
                      type="text"
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      placeholder="Enter full name"
                    />
                  ) : (
                    <div className="text-sm font-medium text-navy-800">{profile.full_name || "—"}</div>
                  )}
                </div>
              )}

              {/* First Name & Last Name */}
              {profile.editable_fields.includes("first_name") && (
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1">First Name</label>
                  {isEditing ? (
                    <input
                      type="text"
                      value={firstName}
                      onChange={(e) => setFirstName(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    />
                  ) : (
                    <div className="text-sm font-medium text-navy-800">{profile.first_name || "—"}</div>
                  )}
                </div>
              )}

              {profile.editable_fields.includes("last_name") && (
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1">Last Name</label>
                  {isEditing ? (
                    <input
                      type="text"
                      value={lastName}
                      onChange={(e) => setLastName(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    />
                  ) : (
                    <div className="text-sm font-medium text-navy-800">{profile.last_name || "—"}</div>
                  )}
                </div>
              )}

              {/* Mobile Phone (Protected by OTP) */}
              <div className="sm:col-span-2">
                <label className="block text-xs font-medium text-slate-600 mb-1">Mobile Phone (Login & OTP)</label>
                <div className="flex items-center justify-between gap-3 bg-slate-50 border border-slate-200 rounded-lg p-2.5">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-slate-800">
                      {pendingVerifiedMobile || profile.mobile_e164 || profile.mobile_masked || "—"}
                    </span>
                    {pendingVerifiedMobile && (
                      <Badge tone="emerald">Verified (Pending Save)</Badge>
                    )}
                  </div>
                  {profile.can_edit && isEditing && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        setShowMobileModal(true);
                        setMobileError(null);
                        setNewMobile("");
                        setOtpSent(false);
                      }}
                    >
                      Change Number
                    </Button>
                  )}
                </div>
              </div>

              {/* Email */}
              <div className="sm:col-span-2">
                <label className="block text-xs font-medium text-slate-600 mb-1">Email Address</label>
                {isEditing && profile.editable_fields.includes("email") ? (
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                    placeholder="name@example.com"
                  />
                ) : (
                  <div className="text-sm text-slate-800">{profile.email || "—"}</div>
                )}
              </div>

              {/* WhatsApp Number */}
              {profile.editable_fields.includes("whatsapp_number") && (
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1">WhatsApp Number</label>
                  {isEditing ? (
                    <input
                      type="text"
                      value={whatsappNumber}
                      onChange={(e) => setWhatsappNumber(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      placeholder="+91..."
                    />
                  ) : (
                    <div className="text-sm text-slate-800">{profile.whatsapp_number || "—"}</div>
                  )}
                </div>
              )}

              {/* Address */}
              {profile.editable_fields.includes("address") && (
                <div className="sm:col-span-2">
                  <label className="block text-xs font-medium text-slate-600 mb-1">Address / Landmark</label>
                  {isEditing ? (
                    <textarea
                      rows={2}
                      value={address}
                      onChange={(e) => setAddress(e.target.value)}
                      className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                      placeholder="Street, locality, landmarks"
                    />
                  ) : (
                    <div className="text-sm text-slate-800">{profile.address || "—"}</div>
                  )}
                </div>
              )}
            </div>
          </Card>

          {/* Territory & Assigned Locations (Read-Only) */}
          {(profile.assigned_locations?.length || profile.mandal_name || profile.district_name || profile.state_name) ? (
            <Card className="p-6">
              <h3 className="text-sm font-semibold text-navy-800 uppercase tracking-wider mb-3 border-b border-slate-100 pb-2">
                Territory & Geographic Scope (State → District → Mandal)
              </h3>

              {/* Manager Assigned Locations */}
              {profile.assigned_locations && profile.assigned_locations.length > 0 ? (
                <div className="space-y-2">
                  <p className="text-xs text-slate-500 mb-2">
                    Authorized geographic territories assigned to this manager:
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {profile.assigned_locations.map((loc) => (
                      <div
                        key={loc.assignment_id}
                        className="bg-slate-50 border border-slate-200 rounded-lg p-2.5 text-xs text-slate-700"
                      >
                        <span className="font-semibold text-navy-800">{loc.state_name || "State"}</span>
                        {loc.district_name && (
                          <span> → <strong className="text-slate-800">{loc.district_name}</strong></span>
                        )}
                        {loc.mandal_name && (
                          <span> → <strong className="text-emerald-700">{loc.mandal_name}</strong> (Mandal)</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                /* Agent Single Location */
                <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 text-sm text-slate-700">
                  <span className="font-semibold text-navy-800">{profile.state_name || "State"}</span>
                  {profile.district_name && (
                    <span> → <strong className="text-slate-800">{profile.district_name}</strong></span>
                  )}
                  {profile.mandal_name && (
                    <span> → <strong className="text-emerald-700">{profile.mandal_name}</strong> (Mandal)</span>
                  )}
                </div>
              )}
            </Card>
          ) : null}

          {/* Hierarchy & Lineage (Read-Only) */}
          {(profile.manager || profile.parent_agent || profile.registering_agent) && (
            <Card className="p-6">
              <h3 className="text-sm font-semibold text-navy-800 uppercase tracking-wider mb-3 border-b border-slate-100 pb-2">
                Operational Hierarchy
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
                {profile.manager && (
                  <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                    <span className="text-slate-400 block mb-1">Assigned Manager</span>
                    <strong className="text-navy-800 block">{profile.manager.name || "Manager"}</strong>
                    {profile.manager.ref_id && (
                      <span className="text-slate-500 font-mono block mt-0.5">{profile.manager.ref_id}</span>
                    )}
                  </div>
                )}
                {profile.parent_agent && (
                  <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                    <span className="text-slate-400 block mb-1">Parent Agent</span>
                    <strong className="text-navy-800 block">{profile.parent_agent.name || "Agent"}</strong>
                    {profile.parent_agent.ref_id && (
                      <span className="text-slate-500 font-mono block mt-0.5">{profile.parent_agent.ref_id}</span>
                    )}
                  </div>
                )}
                {profile.registering_agent && (
                  <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                    <span className="text-slate-400 block mb-1">Registered By</span>
                    <strong className="text-navy-800 block">{profile.registering_agent.name || "Agent"}</strong>
                    {profile.registering_agent.ref_id && (
                      <span className="text-slate-500 font-mono block mt-0.5">{profile.registering_agent.ref_id}</span>
                    )}
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* Subscriber Plan & Billing (Read-Only) */}
          {profile.subscriber_plan && (
            <Card className="p-6">
              <h3 className="text-sm font-semibold text-navy-800 uppercase tracking-wider mb-3 border-b border-slate-100 pb-2">
                Subscription Plan & Entitlements
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
                <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                  <span className="text-slate-400 block mb-1">Current Plan</span>
                  <strong className="text-navy-800 text-sm block">
                    {profile.subscriber_plan.plan_name || "Community Plan"}
                  </strong>
                  <span className="text-slate-600 block mt-1">
                    {profile.subscriber_plan.currency || "INR"} {profile.subscriber_plan.plan_price || 0} /{" "}
                    {profile.subscriber_plan.plan_duration_days || 30} days
                  </span>
                </div>

                <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                  <span className="text-slate-400 block mb-1">Subscription Status</span>
                  <Badge tone={profile.subscriber_plan.subscription_status === "active" ? "emerald" : "amber"}>
                    {(profile.subscriber_plan.subscription_status || "PENDING").toUpperCase()}
                  </Badge>
                  {profile.subscriber_plan.expires_at && (
                    <span className="text-slate-500 block mt-2">
                      Expires: {new Date(profile.subscriber_plan.expires_at).toLocaleDateString()}
                    </span>
                  )}
                </div>

                <div className="bg-slate-50 border border-slate-200 rounded-lg p-3">
                  <span className="text-slate-400 block mb-1">Payment Settlement</span>
                  <Badge tone={profile.subscriber_plan.payment_status === "paid" ? "emerald" : "amber"}>
                    {(profile.subscriber_plan.payment_status || "PENDING").toUpperCase()}
                  </Badge>
                  {profile.subscriber_plan.payment_order_id && (
                    <span className="text-slate-500 font-mono block mt-2 truncate" title={profile.subscriber_plan.payment_order_id}>
                      Order: {profile.subscriber_plan.payment_order_id.slice(0, 12)}...
                    </span>
                  )}
                </div>
              </div>
            </Card>
          )}
        </div>
      </div>

      {/* Mobile Change Modal */}
      {showMobileModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full p-6 space-y-4">
            <h3 className="font-display text-lg font-semibold text-navy-800">
              Verify New Mobile Number
            </h3>
            <p className="text-xs text-slate-500">
              To update the mobile number used for authentication, verify ownership via a one-time passcode (OTP).
            </p>

            {mobileError && (
              <Alert tone="red">
                {mobileError}
              </Alert>
            )}

            {!otpSent ? (
              <div className="space-y-3">
                <label className="block text-xs font-medium text-slate-700">New 10-Digit Mobile Number</label>
                <div className="flex rounded-lg border border-slate-300 overflow-hidden focus-within:ring-2 focus-within:ring-emerald-500">
                  <span className="bg-slate-100 text-slate-600 px-3 py-2 text-sm font-semibold border-r border-slate-200">+91</span>
                  <input
                    type="tel"
                    maxLength={10}
                    value={newMobile}
                    onChange={(e) => setNewMobile(e.target.value.replace(/\D/g, ""))}
                    placeholder="9876543210"
                    className="flex-1 px-3 py-2 text-sm text-slate-800 focus:outline-none"
                  />
                </div>
                <div className="flex justify-end gap-2 pt-2">
                  <Button variant="secondary" size="sm" onClick={() => setShowMobileModal(false)}>
                    Cancel
                  </Button>
                  <Button variant="primary" size="sm" onClick={handleRequestMobileOtp} disabled={sendingOtp || newMobile.length !== 10}>
                    {sendingOtp ? <Spinner className="w-4 h-4" /> : "Send OTP"}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="bg-emerald-50 text-emerald-800 p-2.5 rounded-lg text-xs">
                  OTP sent to <strong>+91 {newMobile}</strong>. Enter the 6-digit code below:
                </div>
                <input
                  type="text"
                  maxLength={6}
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value.replace(/\D/g, ""))}
                  placeholder="Enter 6-digit OTP"
                  className="w-full text-center tracking-widest text-lg font-mono rounded-lg border border-slate-300 px-3 py-2 text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
                <div className="flex justify-between items-center pt-2">
                  <button
                    type="button"
                    onClick={handleRequestMobileOtp}
                    disabled={sendingOtp}
                    className="text-xs text-emerald-600 hover:text-emerald-800 font-medium"
                  >
                    Resend OTP
                  </button>
                  <div className="flex gap-2">
                    <Button variant="secondary" size="sm" onClick={() => setOtpSent(false)}>
                      Change Number
                    </Button>
                    <Button variant="primary" size="sm" onClick={handleVerifyMobileOtp} disabled={verifyingOtp || otpCode.length < 4}>
                      {verifyingOtp ? <Spinner className="w-4 h-4" /> : "Verify Code"}
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
