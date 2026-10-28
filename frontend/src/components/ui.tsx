"use client";

import React from "react";

export function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`bg-white border border-slate-200 rounded-xl2 shadow-card ${className}`}>
      {children}
    </div>
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "outline";
  size?: "sm" | "md" | "lg";
}) {
  const base =
    "inline-flex items-center justify-center gap-2 font-medium rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed";
  const sizes = {
    sm: "px-3 py-1.5 text-sm",
    md: "px-4 py-2.5 text-sm",
    lg: "px-5 py-3 text-base",
  };
  const variants = {
    primary: "bg-navy-700 text-white hover:bg-navy-800",
    secondary: "bg-emerald-500 text-white hover:bg-emerald-600",
    ghost: "bg-transparent text-navy-700 hover:bg-navy-50",
    danger: "bg-red-50 text-red-700 hover:bg-red-100 border border-red-200",
    outline: "bg-white text-navy-700 border border-slate-300 hover:bg-slate-50",
  };
  return (
    <button className={`${base} ${sizes[size]} ${variants[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function Input({
  label,
  error,
  hint,
  className = "",
  ...props
}: React.InputHTMLAttributes<HTMLInputElement> & { label?: string; error?: string; hint?: string }) {
  return (
    <label className="block">
      {label && (
        <span className="block text-sm font-medium text-slate-700 mb-1.5">
          {label}
          {props.required && <span className="text-emerald-600"> *</span>}
        </span>
      )}
      <input
        className={`w-full rounded-lg border ${
          error ? "border-red-300 focus:border-red-400" : "border-slate-300 focus:border-navy-400"
        } bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:ring-2 focus:ring-navy-100 transition ${className}`}
        {...props}
      />
      {hint && !error && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-red-600">{error}</span>}
    </label>
  );
}

export function Textarea({
  label,
  error,
  className = "",
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string; error?: string }) {
  return (
    <label className="block">
      {label && <span className="block text-sm font-medium text-slate-700 mb-1.5">{label}</span>}
      <textarea
        className={`w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:ring-2 focus:ring-navy-100 focus:border-navy-400 transition ${className}`}
        {...props}
      />
      {error && <span className="mt-1 block text-xs text-red-600">{error}</span>}
    </label>
  );
}

export function Badge({
  children,
  tone = "slate",
}: {
  children: React.ReactNode;
  tone?: "slate" | "emerald" | "red" | "amber" | "navy";
}) {
  const tones = {
    slate: "bg-slate-100 text-slate-600",
    emerald: "bg-emerald-50 text-emerald-700",
    red: "bg-red-50 text-red-700",
    amber: "bg-amber-50 text-amber-700",
    navy: "bg-navy-50 text-navy-700",
  };
  return (
    <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function RefChip({ refId }: { refId: string }) {
  return (
    <span className="chip-ref inline-flex items-center gap-1.5 bg-navy-700 text-emerald-50 px-3 py-1 rounded-full text-xs font-semibold">
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
      {refId}
    </span>
  );
}

export function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <Card className="p-5">
      <p className="text-sm text-slate-500">{label}</p>
      <p className="font-display text-3xl font-semibold text-navy-700 mt-1">{value}</p>
      {sub && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
    </Card>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="text-center py-14 px-6">
      <div className="w-12 h-12 rounded-full bg-slate-100 mx-auto mb-4" />
      <p className="font-display text-lg font-semibold text-slate-800">{title}</p>
      <p className="text-sm text-slate-500 mt-1 max-w-sm mx-auto">{description}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

function safeRenderChild(child: React.ReactNode): React.ReactNode {
  if (child === null || child === undefined || typeof child === "boolean") {
    return null;
  }
  if (typeof child === "string" || typeof child === "number") {
    return child;
  }
  if (React.isValidElement(child)) {
    return child;
  }
  if (Array.isArray(child)) {
    return child.map((item, idx) => (
      <React.Fragment key={idx}>{safeRenderChild(item)}</React.Fragment>
    ));
  }
  if (child instanceof Error) {
    return child.message;
  }
  if (typeof child === "object") {
    if ("then" in (child as unknown as { then?: unknown }) && typeof (child as unknown as { then?: unknown }).then === "function") {
      return null;
    }
    const rec = child as unknown as Record<string, unknown>;
    if (typeof rec.message === "string" && rec.message.trim()) {
      return rec.message;
    }
    if (typeof rec.error === "string" && rec.error.trim()) {
      return rec.error;
    }
    if (rec.error && typeof rec.error === "object") {
      const inner = rec.error as Record<string, unknown>;
      if (typeof inner.message === "string" && inner.message.trim()) {
        return inner.message;
      }
      if (typeof inner.code === "string" && inner.code.trim()) {
        return inner.code;
      }
    }
    if (typeof rec.detail === "string" && rec.detail.trim()) {
      return rec.detail;
    }
    if (Array.isArray(rec.detail)) {
      const parts = rec.detail
        .map((d: any) => {
          if (typeof d === "string") return d;
          if (d && typeof d === "object" && typeof d.msg === "string") {
            const loc = Array.isArray(d.loc) ? d.loc.filter((l: any) => l !== "body").join(".") : "";
            return loc ? `${loc}: ${d.msg}` : d.msg;
          }
          return null;
        })
        .filter(Boolean);
      if (parts.length > 0) return parts.join("; ");
    }
    try {
      const str = JSON.stringify(child);
      if (str && str !== "{}") return str;
    } catch {
      // ignore
    }
    return String(child);
  }
  return String(child);
}

export function Alert({ tone = "red", children }: { tone?: "red" | "emerald" | "amber"; children: React.ReactNode }) {
  const tones = {
    red: "bg-red-50 text-red-700 border-red-200",
    emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
    amber: "bg-amber-50 text-amber-700 border-amber-200",
  };
  return <div className={`rounded-lg border px-4 py-3 text-sm ${tones[tone]}`}>{safeRenderChild(children)}</div>;
}

export function Modal({
  title,
  description,
  onClose,
  children,
}: {
  title: string;
  description?: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4">
      <div className="absolute inset-0 bg-navy-900/40" onClick={onClose} />
      <div className="relative bg-white rounded-xl2 shadow-soft w-full max-w-md p-6">
        <h3 className="font-display text-lg font-semibold text-navy-800">{title}</h3>
        {description && <p className="text-sm text-slate-500 mt-1">{description}</p>}
        <div className="mt-4">{children}</div>
      </div>
    </div>
  );
}

/**
 * A confirm dialog that requires a short reason before the destructive action
 * (e.g. deactivating an employee) is allowed to proceed.
 */
export function ReasonConfirmModal({
  title,
  description,
  confirmLabel = "Confirm",
  placeholder = "Add a reason…",
  loading = false,
  onCancel,
  onConfirm,
}: {
  title: string;
  description?: string;
  confirmLabel?: string;
  placeholder?: string;
  loading?: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = React.useState("");
  const [touched, setTouched] = React.useState(false);
  const isEmpty = !reason.trim();

  return (
    <Modal title={title} description={description} onClose={onCancel}>
      <Textarea
        rows={3}
        autoFocus
        placeholder={placeholder}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        onBlur={() => setTouched(true)}
        error={touched && isEmpty ? "Please add a reason before confirming." : undefined}
      />
      <div className="flex justify-end gap-3 mt-5">
        <Button variant="outline" onClick={onCancel} disabled={loading}>
          Cancel
        </Button>
        <Button
          variant="danger"
          onClick={() => {
            setTouched(true);
            if (!isEmpty) onConfirm(reason.trim());
          }}
          disabled={loading}
        >
          {loading ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
