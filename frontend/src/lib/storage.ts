import { fetchBackendApi } from "./backend-api";
import { normalizeApiError } from "./api-error";

export type ImageCategory =
  | "agent-photo"
  | "aadhaar-document"
  | "pan-document"
  | "user-photo";

export interface StorageUploadResult {
  object_key: string;
  category: string;
  content_type: string;
  byte_size: number;
}

const MAX_UPLOAD_BYTES = 500 * 1024; // 500 KB hard limit

export async function uploadRegistrationImage(
  file: File,
  category: ImageCategory,
  csrfToken: string,
  verificationToken?: string | null
): Promise<StorageUploadResult> {
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error(
      `Image file size (${(file.size / 1024).toFixed(1)} KB) exceeds the maximum allowed limit of 500 KB. Please compress before uploading.`
    );
  }

  const formData = new FormData();
  formData.append("category", category);
  formData.append("file", file);
  if (verificationToken) {
    formData.append("verification_token", verificationToken.trim());
  }

  const headers: Record<string, string> = {
    "X-CSRF-Token": csrfToken,
    Origin: typeof window !== "undefined" ? window.location.origin : "",
  };
  if (verificationToken) {
    headers["X-Registration-Verification-Token"] = verificationToken.trim();
  }

  const res = await fetchBackendApi("api/v1/storage/upload", {
    method: "POST",
    headers,
    body: formData,
    credentials: "same-origin",
  });

  if (!res.ok) {
    let errMessage = `Image upload failed (HTTP ${res.status})`;
    try {
      const data = await res.json();
      errMessage = normalizeApiError(data, errMessage);
    } catch {
      // ignore
    }
    throw new Error(errMessage);
  }

  return (await res.json()) as StorageUploadResult;
}

export function getPreviewUrl(objectKey: string, verificationToken?: string | null): string {
  const base = `/backend-api/api/v1/storage/${objectKey}/preview`;
  if (verificationToken) {
    return `${base}?verification_token=${encodeURIComponent(verificationToken.trim())}`;
  }
  return base;
}

