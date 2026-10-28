"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "./AuthGuard";
import { Badge, Spinner } from "./ui";
import { fetchBackendApi } from "@/lib/backend-api";

export interface NotificationItem {
  id: string;
  recipient_operator_id: string;
  tenant_id?: string | null;
  appointment_id?: string | null;
  agent_profile_id?: string | null;
  event_type: string;
  title: string;
  message: string;
  is_read: boolean;
  read_at?: string | null;
  resolution_status: "PENDING" | "APPROVED" | "REJECTED" | "CANCELLED" | string;
  resolved_at?: string | null;
  metadata_json?: Record<string, unknown>;
  created_at: string;
}

export function SuperAdminNotifications() {
  const router = useRouter();
  const { identity } = useAuth();
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [markingAll, setMarkingAll] = useState<boolean>(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const fetchUnreadCount = useCallback(async () => {
    try {
      const res = await fetchBackendApi("api/v1/platform/notifications/unread-count", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        setUnreadCount(typeof data.unread_count === "number" ? data.unread_count : 0);
      }
    } catch {
      // silently tolerate transient network errors in background polling
    }
  }, []);

  const fetchNotifications = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchBackendApi("api/v1/platform/notifications?limit=25", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.ok) {
        const data = await res.json();
        setNotifications(data.items || []);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll for unread count every 25 seconds when page is visible
  useEffect(() => {
    fetchUnreadCount();

    const interval = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        fetchUnreadCount();
      }
    }, 25000);

    const handleFocus = () => {
      fetchUnreadCount();
    };

    window.addEventListener("focus", handleFocus);
    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", handleFocus);
    };
  }, [fetchUnreadCount]);

  // When dropdown opens, load recent notifications and refresh count
  useEffect(() => {
    if (isOpen) {
      fetchNotifications();
      fetchUnreadCount();
    }
  }, [isOpen, fetchNotifications, fetchUnreadCount]);

  // Close dropdown on outside click or Escape
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setIsOpen(false);
      }
    }

    if (isOpen) {
      document.addEventListener("mousedown", handleClickOutside);
      document.addEventListener("keydown", handleKeyDown);
    }
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  async function handleMarkAllAsRead() {
    if (!identity?.csrf_token || markingAll) return;
    setMarkingAll(true);
    try {
      const res = await fetchBackendApi("api/v1/platform/notifications/mark-all-read", {
        method: "POST",
        headers: {
          "X-CSRF-Token": identity.csrf_token,
          Origin: window.location.origin,
        },
        credentials: "same-origin",
      });
      if (res.ok) {
        setUnreadCount(0);
        setNotifications((prev) => prev.map((n) => ({ ...n, is_read: true })));
      }
    } catch {
      // ignore
    } finally {
      setMarkingAll(false);
    }
  }

  async function handleNotificationClick(item: NotificationItem) {
    if (!item.is_read && identity?.csrf_token) {
      try {
        await fetchBackendApi(`api/v1/platform/notifications/${item.id}/read`, {
          method: "PATCH",
          headers: {
            "X-CSRF-Token": identity.csrf_token,
            Origin: window.location.origin,
          },
          credentials: "same-origin",
        });
        setUnreadCount((prev) => Math.max(0, prev - 1));
        setNotifications((prev) =>
          prev.map((n) => (n.id === item.id ? { ...n, is_read: true } : n))
        );
      } catch {
        // continue navigation anyway
      }
    }

    setIsOpen(false);

    if (item.agent_profile_id) {
      router.push(`/superadmin/employees/${item.agent_profile_id}`);
    }
  }

  function renderStatusBadge(status: string) {
    switch (status) {
      case "PENDING":
        return <Badge tone="amber">Pending Approval</Badge>;
      case "APPROVED":
        return <Badge tone="emerald">Approved</Badge>;
      case "REJECTED":
        return <Badge tone="red">Rejected</Badge>;
      case "CANCELLED":
        return <Badge tone="slate">Cancelled</Badge>;
      default:
        return <Badge tone="slate">{status}</Badge>;
    }
  }

  return (
    <div className="relative" ref={dropdownRef}>
      {/* Bell Button */}
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-label="Super Admin Notifications"
        aria-expanded={isOpen}
        className={`relative p-2 rounded-lg transition-colors border ${
          isOpen
            ? "bg-navy-50 text-navy-800 border-navy-200"
            : "text-slate-600 hover:text-navy-800 hover:bg-slate-50 border-slate-200"
        }`}
      >
        <svg
          className="w-5 h-5"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
            d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
          />
        </svg>

        {unreadCount > 0 && (
          <span className="absolute -top-1 -right-1 flex h-5 min-w-[20px] items-center justify-center rounded-full bg-amber-500 px-1 text-[11px] font-bold text-white shadow-sm ring-2 ring-white">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {/* Notifications Dropdown Panel */}
      {isOpen && (
        <div className="absolute right-0 mt-2 w-80 sm:w-96 rounded-xl border border-slate-200 bg-white shadow-xl z-50 overflow-hidden">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-slate-100 bg-sand-50/70 px-4 py-3">
            <div className="flex items-center gap-2">
              <h3 className="font-display text-sm font-bold text-navy-800">
                Approval Notifications
              </h3>
              {unreadCount > 0 && (
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-800">
                  {unreadCount} unread
                </span>
              )}
            </div>
            {unreadCount > 0 && (
              <button
                type="button"
                onClick={handleMarkAllAsRead}
                disabled={markingAll}
                className="text-xs font-semibold text-emerald-700 hover:text-emerald-800 disabled:opacity-50 transition"
              >
                {markingAll ? "Marking..." : "Mark all read"}
              </button>
            )}
          </div>

          {/* List Content */}
          <div className="max-h-[360px] overflow-y-auto divide-y divide-slate-100">
            {loading ? (
              <div className="py-8 flex flex-col items-center justify-center gap-2 text-slate-400">
                <Spinner className="w-5 h-5 text-slate-500" />
                <span className="text-xs">Loading notifications...</span>
              </div>
            ) : notifications.length === 0 ? (
              <div className="py-10 text-center px-4">
                <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center mx-auto mb-2 text-slate-400">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="1.5"
                      d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
                    />
                  </svg>
                </div>
                <p className="text-sm font-medium text-slate-700">No notifications yet</p>
                <p className="text-xs text-slate-400 mt-0.5">
                  Agent creation and appointment approval requests will appear here.
                </p>
              </div>
            ) : (
              notifications.map((item) => (
                <div
                  key={item.id}
                  onClick={() => handleNotificationClick(item)}
                  className={`p-3.5 hover:bg-slate-50 transition-colors cursor-pointer flex items-start gap-3 ${
                    !item.is_read ? "bg-amber-50/40" : ""
                  }`}
                >
                  <div className="mt-0.5 shrink-0">
                    {!item.is_read ? (
                      <span className="block w-2 h-2 rounded-full bg-amber-500 mt-1" />
                    ) : (
                      <span className="block w-2 h-2 rounded-full bg-transparent mt-1" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <p
                        className={`text-xs font-semibold truncate ${
                          !item.is_read ? "text-navy-900 font-bold" : "text-slate-800"
                        }`}
                      >
                        {item.title}
                      </p>
                      {renderStatusBadge(item.resolution_status)}
                    </div>
                    <p className="text-xs text-slate-600 line-clamp-2 leading-relaxed">
                      {item.message}
                    </p>
                    <div className="mt-2 flex items-center justify-between text-[11px] text-slate-400">
                      <span>{new Date(item.created_at).toLocaleString()}</span>
                      {item.agent_profile_id && (
                        <span className="text-emerald-700 font-medium hover:underline">
                          Review profile →
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
