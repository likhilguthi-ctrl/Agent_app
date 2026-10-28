"use client";

import React, { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/AuthGuard";
import { Card, Button, Input } from "@/components/ui";
import { fetchBackendApi } from "@/lib/backend-api";

const SLUG_REGEX = /^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]?$/;

export default function CreateCommunityPage() {
  const router = useRouter();
  const { identity } = useAuth();

  const [slug, setSlug] = useState("");
  const [domainHostname, setDomainHostname] = useState("");
  const [status, setStatus] = useState<"active" | "inactive">("active");

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [slugError, setSlugError] = useState<string | null>(null);

  const handleSlugChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
    setSlug(val);
    if (val && !SLUG_REGEX.test(val)) {
      setSlugError("Slug must start and end with alphanumeric characters and contain only lowercase letters, digits, and hyphens (max 63 chars).");
    } else {
      setSlugError(null);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    const cleanSlug = slug.trim().toLowerCase();
    if (!cleanSlug || !SLUG_REGEX.test(cleanSlug)) {
      setSlugError("A valid community slug is required.");
      return;
    }

    setIsSubmitting(true);
    try {
      const response = await fetchBackendApi("api/v1/platform/communities", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: JSON.stringify({
          slug: cleanSlug,
          domain_hostname: domainHostname.trim() || undefined,
          status,
        }),
      });

      if (!response.ok) {
        let errorDetail = "Failed to create community.";
        try {
          const errorJson = await response.json();
          errorDetail =
            errorJson?.error?.message ||
            errorJson?.detail?.message ||
            `Error (${response.status}): The community could not be created.`;
        } catch {
          // fallback errorDetail
        }
        setErrorMessage(errorDetail);
        setIsSubmitting(false);
        return;
      }

      const created = await response.json();
      router.push(`/superadmin/communities/${created.id}`);
    } catch {
      setErrorMessage("Network connection failed. Please verify the backend is running.");
      setIsSubmitting(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
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
        <span className="text-slate-800 font-semibold">Create Community</span>
      </nav>

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold font-display text-navy-900 tracking-tight">
          Create New Community
        </h1>
        <p className="text-sm text-slate-600 mt-1">
          Provision an isolated tenant community space, assign its system identifier, and register its primary domain.
        </p>
      </div>

      {/* Error Alert */}
      {errorMessage && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          <p className="font-semibold">Creation Failed</p>
          <p className="mt-0.5 text-xs text-red-600">{errorMessage}</p>
        </div>
      )}

      {/* Form Card */}
      <Card className="p-6">
        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <Input
              label="Community Slug"
              required
              placeholder="e.g. green-valley"
              value={slug}
              onChange={handleSlugChange}
              error={slugError ?? undefined}
              hint="Unique identifier used in URLs and tenant routing (lowercase, numbers, and hyphens)."
            />
            {slug && (
              <p className="text-[11px] text-slate-500 mt-1">
                Resolved domain preview: <span className="font-mono text-navy-700">{slug}.avanflix.com</span>
              </p>
            )}
          </div>

          <div>
            <Input
              label="Primary Domain Hostname (Optional)"
              placeholder="e.g. greenvalley.ott-platform.local"
              value={domainHostname}
              onChange={(e) => setDomainHostname(e.target.value.toLowerCase())}
              hint="Custom canonical hostname if this community uses a branded domain."
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              Initial Operational Status
            </label>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setStatus("active")}
                className={`flex flex-col items-start p-3.5 rounded-xl border text-left transition ${
                  status === "active"
                    ? "border-emerald-500 bg-emerald-50/50 ring-1 ring-emerald-500"
                    : "border-slate-200 bg-white hover:border-slate-300"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
                  <span className="text-sm font-semibold text-slate-800">Active</span>
                </div>
                <span className="text-xs text-slate-500 mt-1">
                  Community will be immediately accessible to authorized administrators.
                </span>
              </button>

              <button
                type="button"
                onClick={() => setStatus("inactive")}
                className={`flex flex-col items-start p-3.5 rounded-xl border text-left transition ${
                  status === "inactive"
                    ? "border-amber-500 bg-amber-50/50 ring-1 ring-amber-500"
                    : "border-slate-200 bg-white hover:border-slate-300"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-amber-500" />
                  <span className="text-sm font-semibold text-slate-800">Inactive</span>
                </div>
                <span className="text-xs text-slate-500 mt-1">
                  Provision configuration without allowing tenant logins or operations yet.
                </span>
              </button>
            </div>
          </div>

          <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-100">
            <Link href="/superadmin/communities">
              <Button type="button" variant="outline" disabled={isSubmitting}>
                Cancel
              </Button>
            </Link>
            <Button type="submit" variant="primary" disabled={isSubmitting || !!slugError || !slug}>
              {isSubmitting ? "Creating Community..." : "Create Community"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

