/**
 * Normalizes any error value (Error instances, FastAPI JSON payloads,
 * nested error objects, Pydantic validation lists, or unknown throws)
 * into a safe, human-readable string for React rendering.
 *
 * Guarantees that the returned value is always a non-empty string,
 * preventing React runtime crashes when objects are accidentally
 * passed as React children to components like Alert.
 */

export function normalizeApiError(
  err: unknown,
  fallback: string = "An unexpected error occurred."
): string {
  if (err === null || err === undefined) {
    return fallback;
  }

  if (typeof err === "string") {
    const trimmed = err.trim();
    return trimmed || fallback;
  }

  if (typeof err === "number" || typeof err === "boolean") {
    return String(err);
  }

  if (err instanceof Error) {
    return err.message?.trim() || fallback;
  }

  if (typeof err === "object") {
    // If it's a Promise
    if ("then" in err && typeof (err as { then?: unknown }).then === "function") {
      return fallback;
    }

    const rec = err as Record<string, unknown>;

    // Case 1: { error: { message: "...", code: "..." } } (Standard FastAPI sanitized error)
    if (rec.error && typeof rec.error === "object" && !Array.isArray(rec.error)) {
      const inner = rec.error as Record<string, unknown>;
      if (typeof inner.message === "string" && inner.message.trim()) {
        return inner.message.trim();
      }
      if (typeof inner.code === "string" && inner.code.trim()) {
        return inner.code.trim();
      }
    }

    // Case 2: { error: "..." }
    if (typeof rec.error === "string" && rec.error.trim()) {
      return rec.error.trim();
    }

    // Case 3: { message: "..." }
    if (typeof rec.message === "string" && rec.message.trim()) {
      return rec.message.trim();
    }

    // Case 4: { detail: "..." }
    if (typeof rec.detail === "string" && rec.detail.trim()) {
      return rec.detail.trim();
    }

    // Case 5: { detail: { message: "...", error: "..." } }
    if (rec.detail && typeof rec.detail === "object" && !Array.isArray(rec.detail)) {
      const inner = rec.detail as Record<string, unknown>;
      if (typeof inner.message === "string" && inner.message.trim()) {
        return inner.message.trim();
      }
      if (typeof inner.error === "string" && inner.error.trim()) {
        return inner.error.trim();
      }
    }

    // Case 6: FastAPI / Pydantic validation error array: { detail: [ { loc: [...], msg: "..." } ] }
    if (Array.isArray(rec.detail)) {
      const parts = rec.detail
        .map((item) => {
          if (typeof item === "string" && item.trim()) {
            return item.trim();
          }
          if (item && typeof item === "object") {
            const locArr = Array.isArray(item.loc)
              ? item.loc.filter((p: unknown) => p !== "body")
              : [];
            const locStr = locArr.join(".");
            const msg = typeof item.msg === "string" ? item.msg.trim() : "";
            if (locStr && msg) {
              return `${locStr}: ${msg}`;
            }
            if (msg) {
              return msg;
            }
          }
          return null;
        })
        .filter((p): p is string => Boolean(p));

      if (parts.length > 0) {
        return parts.join("; ");
      }
    }

    // Case 7: { statusText: "..." }
    if (typeof rec.statusText === "string" && rec.statusText.trim()) {
      return rec.statusText.trim();
    }

    // Case 8: Try JSON representation if safe, or fallback
    try {
      const json = JSON.stringify(err);
      if (json && json !== "{}") {
        return json;
      }
    } catch {
      // ignore serialization errors
    }
  }

  return fallback;
}

/**
 * Extracts and normalizes an error message from an unsuccessful fetch Response.
 */
export async function extractResponseError(
  response: Response,
  fallback?: string
): Promise<string> {
  const defaultFallback = fallback || `Request failed (HTTP ${response.status})`;
  try {
    const data = await response.json();
    return normalizeApiError(data, defaultFallback);
  } catch {
    return defaultFallback;
  }
}

