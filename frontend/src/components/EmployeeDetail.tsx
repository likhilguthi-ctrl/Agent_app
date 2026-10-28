"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "./AuthGuard";
import { Card, Badge, RefChip, Button, Spinner, EmptyState, Alert, ReasonConfirmModal } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";
import { getPreviewUrl } from "@/lib/storage";
import { extractResponseError } from "@/lib/api-error";
import { EmployeeTargetSummary } from "@/components/targets/EmployeeTargetSummary";
import { Icon, icons } from "@/components/icons";

interface AgentProfileDetail {
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
  address?: string | null;
  ref_id: string;
  status: "active" | "inactive" | "pending";
  effective_commission_rate: number | string;
  commission_rate_override?: number | string | null;
  profile_photo_key: string;
  aadhaar_number_masked: string;
  aadhaar_document_key: string;
  pan_number_masked: string;
  pan_document_key: string;
  created_at: string;
  pending_appointment_id?: string | null;
}

export default function EmployeeDetail({ basePath, id }: { basePath: string; id: string }) {
  const { identity } = useAuth();
  const [agent, setAgent] = useState<AgentProfileDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [showDeactivateModal, setShowDeactivateModal] = useState(false);
  const [statusError, setStatusError] = useState("");
  const [photoError, setPhotoError] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await fetchBackendApi(`api/v1/agents/${id}`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!res.ok) {
        setNotFound(true);
        setLoading(false);
        return;
      }
      const data = await res.json();
      setAgent(data);
      setLoading(false);
    } catch {
      setNotFound(true);
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function handleApproveAppointment() {
    if (!agent || !agent.pending_appointment_id || !identity?.csrf_token) return;
    setToggling(true);
    setStatusError("");
    try {
      const res = await fetchBackendApi(
        `api/v1/agents/appointments/${agent.pending_appointment_id}/approve`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": identity.csrf_token,
            Origin: window.location.origin,
          },
          body: JSON.stringify({
            reason: "Initial appointment approved via Agent Detail view",
          }),
          credentials: "same-origin",
        }
      );
      if (res.ok) {
        setStatusError("");
        alert(
          `Agent appointment for ${agent.first_name} ${agent.last_name} approved successfully. Credentials are now active.`
        );
        await load();
      } else {
        const errorMsg = await extractResponseError(res, "Failed to approve agent appointment.");
        if (
          res.status === 403 &&
          (errorMsg.includes("SELF_APPROVAL") ||
            errorMsg.includes("cannot approve appointments they requested") ||
            errorMsg.includes("Reviewers cannot approve"))
        ) {
          setStatusError(
            "Self-approval prohibited: You created this agent appointment. As per security policy (SEC-03 / dual custody), initial appointments must be approved by another administrator or Super Admin."
          );
        } else {
          setStatusError(errorMsg);
        }
      }
    } catch {
      setStatusError("Network error while approving agent appointment.");
    } finally {
      setToggling(false);
    }
  }

  async function setStatus(newStatus: "active" | "inactive", reason?: string) {
    if (!agent || !identity?.csrf_token) return;
    if (agent.status === "pending" && newStatus === "active") {
      setStatusError(
        "Cannot reactivate an agent whose initial appointment was never approved. Please approve the initial appointment first."
      );
      return;
    }
    setToggling(true);
    setStatusError("");
    try {
      const res = await fetchBackendApi(`api/v1/agents/${id}/lifecycle-requests`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
          Origin: window.location.origin,
        },
        body: JSON.stringify({
          action: newStatus === "active" ? "REACTIVATE" : "DEACTIVATE",
          reason:
            reason?.trim() ||
            (newStatus === "active"
              ? "Reactivation requested by Community Admin"
              : "Deactivation requested by Community Admin"),
        }),
        credentials: "same-origin",
      });
      if (res.ok) {
        setShowDeactivateModal(false);
        setStatusError("");
        alert(
          `Lifecycle request (${newStatus === "active" ? "REACTIVATE" : "DEACTIVATE"}) submitted successfully for Super Admin review.`
        );
        await load();
      } else {
        const errorMsg = await extractResponseError(res, "Lifecycle request submission failed.");
        setStatusError(errorMsg);
      }
    } catch {
      setStatusError("Network error while submitting lifecycle request.");
    } finally {
      setToggling(false);
    }
  }

  function handleStatusButtonClick() {
    if (!agent) return;
    if (agent.status === "pending") {
      handleApproveAppointment();
      return;
    }
    if (agent.status === "active") {
      setStatusError("");
      setShowDeactivateModal(true);
    } else {
      setStatus("active");
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-24 text-slate-400">
        <Spinner className="w-6 h-6" />
      </div>
    );
  }

  if (notFound || !agent) {
    return (
      <Card className="p-8">
        <EmptyState
          title="Agent not found"
          description="This agent record may not exist or belongs to another community scope."
        />
        <div className="mt-4 text-center">
          <Link href={`${basePath}/employees`}>
            <Button variant="secondary">Back to Agents list</Button>
          </Link>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <Link href={`${basePath}/employees`} className="text-sm text-slate-500 hover:text-navy-700">
        ← Back to employees & agents
      </Link>

      <div className="flex items-start justify-between flex-wrap gap-4 mt-2">
        <div className="flex items-center gap-4">
          {/* Agent Photo Preview */}
          <div className="w-16 h-16 rounded-full bg-slate-100 border border-slate-200 overflow-hidden flex-shrink-0 flex items-center justify-center">
            {!photoError && agent.profile_photo_key ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={getPreviewUrl(agent.profile_photo_key)}
                alt="Agent photo"
                className="w-full h-full object-cover"
                onError={() => setPhotoError(true)}
              />
            ) : (
              <span className="text-2xl">👤</span>
            )}
          </div>

          <div>
            <div className="flex items-center gap-3">
              <h1 className="font-display text-2xl font-semibold text-navy-800">
                {agent.first_name} {agent.last_name}
              </h1>
              <Badge
                tone={
                  agent.status === "active"
                    ? "emerald"
                    : agent.status === "pending"
                    ? "amber"
                    : "slate"
                }
              >
                {agent.status === "active"
                  ? "Active"
                  : agent.status === "pending"
                  ? "Pending Approval"
                  : "Inactive"}
              </Badge>
            </div>
            <p className="text-xs text-slate-400 mt-1">
              @{agent.username} • Territory: <span className="font-semibold text-slate-600">{agent.community_slug}</span>
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Link href={`${basePath}/employees/${id}/edit`}>
            <Button variant="secondary" className="inline-flex items-center gap-1.5">
              <Icon path={icons.edit} />
              <span>Edit Profile</span>
            </Button>
          </Link>
          {agent.status === "pending" ? (
            <Button
              variant="primary"
              onClick={handleApproveAppointment}
              disabled={toggling || !agent.pending_appointment_id}
            >
              {toggling ? "Approving…" : "Approve Appointment"}
            </Button>
          ) : (
            <Button
              variant={agent.status === "active" ? "danger" : "secondary"}
              onClick={handleStatusButtonClick}
              disabled={toggling}
            >
              {toggling ? "Updating…" : agent.status === "active" ? "Deactivate Agent" : "Activate Agent"}
            </Button>
          )}
        </div>
      </div>

      {agent.status === "pending" && (
        <Alert tone="amber">
          <strong>Pending Initial Appointment Approval:</strong> This agent was newly registered and requires appointment approval before credentials become active.
          As per security policy (SEC-03 / dual custody), initial appointments must be approved by another administrator or Super Admin.
        </Alert>
      )}

      {statusError && <Alert tone="red">{statusError}</Alert>}

      {/* Main Details Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Card className="p-6 space-y-4">
          <h2 className="font-semibold text-slate-800 text-sm uppercase tracking-wider">
            Agent Credentials & Contact
          </h2>
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-xs text-slate-400 font-medium">Permanent Ref ID</dt>
              <dd className="mt-1"><RefChip refId={agent.ref_id} /></dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">Status</dt>
              <dd className="mt-1 text-slate-700 font-semibold uppercase">{agent.status}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">Mobile (E.164)</dt>
              <dd className="mt-1 font-mono text-slate-800">{agent.mobile_e164}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">Email</dt>
              <dd className="mt-1 text-slate-700">{agent.email || "—"}</dd>
            </div>
            <div className="col-span-2">
              <dt className="text-xs text-slate-400 font-medium">Address</dt>
              <dd className="mt-1 text-slate-700">{agent.address || "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">Registered Date</dt>
              <dd className="mt-1 text-slate-700 text-xs">{new Date(agent.created_at).toLocaleString()}</dd>
            </div>
          </dl>
        </Card>

        <Card className="p-6 space-y-4">
          <h2 className="font-semibold text-slate-800 text-sm uppercase tracking-wider">
            Identity & Documents (Cloudflare R2)
          </h2>
          <dl className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <dt className="text-xs text-slate-400 font-medium">Aadhaar (Encrypted at Rest)</dt>
              <dd className="mt-1 font-mono font-medium text-slate-800">{agent.aadhaar_number_masked}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">Aadhaar Document</dt>
              <dd className="mt-1">
                <a
                  href={getPreviewUrl(agent.aadhaar_document_key)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs font-semibold text-navy-700 hover:underline"
                >
                  View Document ↗
                </a>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">PAN (Encrypted at Rest)</dt>
              <dd className="mt-1 font-mono font-medium text-slate-800">{agent.pan_number_masked}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-400 font-medium">PAN Document</dt>
              <dd className="mt-1">
                <a
                  href={getPreviewUrl(agent.pan_document_key)}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs font-semibold text-navy-700 hover:underline"
                >
                  View Document ↗
                </a>
              </dd>
            </div>
            <div className="col-span-2 border-t border-slate-100 pt-3">
              <dt className="text-xs text-slate-400 font-medium">Effective Commission Rate</dt>
              <dd className="mt-1 flex items-center gap-2">
                <span className="text-xl font-bold text-emerald-600">{agent.effective_commission_rate}%</span>
                {agent.commission_rate_override !== null && (
                  <Badge tone="navy">Custom Override: {agent.commission_rate_override}%</Badge>
                )}
              </dd>
            </div>
          </dl>
        </Card>
      </div>

      {/* Target & KPI Performance */}
      <EmployeeTargetSummary userId={agent.user_id} />

      {showDeactivateModal && (
        <ReasonConfirmModal
          title="Deactivate Agent?"
          description={`Are you sure you want to deactivate ${agent.first_name} ${agent.last_name}? Their login sessions will be immediately invalidated.`}
          confirmLabel="Deactivate"
          loading={toggling}
          onConfirm={(reason) => setStatus("inactive", reason)}
          onCancel={() => setShowDeactivateModal(false)}
        />
      )}
    </div>
  );
}
