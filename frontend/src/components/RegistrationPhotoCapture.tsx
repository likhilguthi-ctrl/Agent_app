"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  compressImage,
  formatFileSize,
  type CompressedImageResult,
} from "@/lib/image-compression";

const MAX_EDGE = 1600;
const MAX_CAPTURE_BYTES = 5 * 1024 * 1024; // 5 MB

function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

function cameraErrorMessage(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Camera access was denied. Allow camera access in your browser settings, then try again.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "No camera was found on this device.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "The camera could not start. It may already be in use by another app.";
  }
  return "The camera could not be started. Check your browser and device settings or use file upload.";
}

interface RegistrationPhotoCaptureProps {
  label?: string;
  description?: string;
  required?: boolean;
  onPhotoChange: (photo: File | null) => void;
}

export default function RegistrationPhotoCapture({
  label = "Photo",
  description,
  required = false,
  onPhotoChange,
}: RegistrationPhotoCaptureProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const mountedRef = useRef(false);
  const cameraRequestRef = useRef(0);
  const previewUrlRef = useRef<string | null>(null);

  const [stream, setStream] = useState<MediaStream | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);
  const [previewVisible, setPreviewVisible] = useState(true);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const [compressing, setCompressing] = useState(false);
  const [compressionStats, setCompressionStats] = useState<CompressedImageResult | null>(null);

  const stopCamera = useCallback(() => {
    cameraRequestRef.current += 1;
    const current = streamRef.current;
    streamRef.current = null;
    stopStream(current);
    setStream(null);
    setStarting(false);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopStream(streamRef.current);
      streamRef.current = null;
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!stream || !video) return;
    video.srcObject = stream;
    void video.play().catch(() => {
      if (mountedRef.current) setError("The live camera preview could not be displayed.");
    });
    return () => {
      video.srcObject = null;
    };
  }, [stream]);

  function clearPhoto() {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = null;
    setPhotoPreview(null);
    setCompressionStats(null);
    onPhotoChange(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function processImageFile(rawFile: File) {
    setCompressing(true);
    setError("");
    try {
      const result = await compressImage(rawFile);
      clearPhoto();
      previewUrlRef.current = result.previewUrl;
      setPhotoPreview(result.previewUrl);
      setCompressionStats(result);
      setPreviewVisible(true);
      onPhotoChange(result.file);
    } catch (compressionErr) {
      const msg = compressionErr instanceof Error ? compressionErr.message : "Failed to process photo";
      setError(msg);
      clearPhoto();
    } finally {
      setCompressing(false);
    }
  }

  async function startCamera() {
    setError("");
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("Camera access is not supported by this browser. Please use the file upload option instead.");
      return;
    }
    const localHosts = new Set(["localhost", "127.0.0.1", "[::1]"]);
    if (!window.isSecureContext && !localHosts.has(window.location.hostname)) {
      setError("Camera access requires HTTPS or localhost development context. Please use the file upload option.");
      return;
    }

    stopCamera();
    const requestId = ++cameraRequestRef.current;
    setStarting(true);
    try {
      const acquired = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "user" } },
        audio: false,
      });
      if (!mountedRef.current || cameraRequestRef.current !== requestId) {
        stopStream(acquired);
        return;
      }
      streamRef.current = acquired;
      setStream(acquired);
    } catch (cameraError) {
      if (mountedRef.current && cameraRequestRef.current === requestId) {
        setError(cameraErrorMessage(cameraError));
      }
    } finally {
      if (mountedRef.current && cameraRequestRef.current === requestId) setStarting(false);
    }
  }

  async function capturePhoto() {
    const video = videoRef.current;
    if (!video || video.videoWidth <= 0 || video.videoHeight <= 0) {
      setError("The camera preview is not ready. Wait for the picture, then capture again.");
      return;
    }

    const scale = Math.min(1, MAX_EDGE / Math.max(video.videoWidth, video.videoHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) {
      setError("This browser could not prepare the photo. Please use file upload.");
      return;
    }
    context.drawImage(video, 0, 0, canvas.width, canvas.height);

    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob((result) => {
          if (result) resolve(result);
          else reject(new Error("Photo encoding failed"));
        }, "image/jpeg", 0.92);
      });
      if (blob.size > MAX_CAPTURE_BYTES) {
        setError("The captured photo is too large to process safely. Please try again.");
        return;
      }
      const rawFile = new File([blob], "camera-capture.jpg", {
        type: "image/jpeg",
        lastModified: Date.now(),
      });
      stopCamera();
      await processImageFile(rawFile);
    } catch {
      setError("The photo could not be prepared. Please try again.");
    }
  }

  async function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    setError("");
    const file = e.target.files?.[0];
    if (!file) return;

    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      setError("Only JPEG, PNG, and WebP images are supported.");
      return;
    }

    if (file.size > MAX_CAPTURE_BYTES) {
      setError("Image file size must be less than 5 MB.");
      return;
    }

    stopCamera();
    await processImageFile(file);
  }

  return (
    <section className="space-y-3" aria-label={label}>
      <div>
        <h3 className="text-sm font-medium text-slate-700">
          {label} {required ? <span className="text-red-500 font-semibold">*</span> : <span className="font-normal text-slate-400">(optional)</span>}
        </h3>
        {description ? (
          <p className="mt-1 text-xs text-slate-500">{description}</p>
        ) : (
          <p className="mt-1 text-xs text-slate-500">
            Take a live photo using your camera or upload an image file (JPG, PNG, WebP up to 5MB).
          </p>
        )}
      </div>

      {stream && (
        <div className="space-y-2">
          <video
            ref={videoRef}
            autoPlay
            muted
            playsInline
            className="w-full max-h-72 rounded-lg bg-slate-950 object-contain shadow-inner"
            aria-label="Live camera preview"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={capturePhoto}
              className="rounded-lg bg-navy-700 px-4 py-2 text-sm font-medium text-white hover:bg-navy-800 transition shadow-sm"
            >
              Capture
            </button>
            <button
              type="button"
              onClick={() => { stopCamera(); setError(""); }}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 transition"
            >
              Cancel camera
            </button>
          </div>
        </div>
      )}

      {compressing && (
        <div className="flex items-center gap-2 text-xs text-navy-700 bg-navy-50 border border-navy-100 rounded-lg p-2.5">
          <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-navy-600 border-t-transparent" />
          <span>Optimizing image for secure Cloudflare R2 storage...</span>
        </div>
      )}

      {photoPreview && (
        <div className="space-y-2">
          {previewVisible ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={photoPreview}
              alt="Photo preview"
              className="max-h-72 w-full rounded-lg bg-slate-100 object-contain border border-slate-200 shadow-sm"
            />
          ) : (
            <p className="text-sm text-slate-600">A photo is selected for preview.</p>
          )}

          {compressionStats && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-md px-2.5 py-1.5">
              <span className="font-semibold text-emerald-700">
                {formatFileSize(compressionStats.compressedBytes)}
              </span>
              {compressionStats.savingsPercent > 0 && (
                <span className="text-slate-500">
                  (was {formatFileSize(compressionStats.originalBytes)}, {compressionStats.savingsPercent}% saved)
                </span>
              )}
              <span className="text-slate-400">&bull;</span>
              <span>
                {compressionStats.width} &times; {compressionStats.height} JPEG
              </span>
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setPreviewVisible((visible) => !visible)}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              {previewVisible ? "Hide preview" : "Show preview"}
            </button>
            <button
              type="button"
              onClick={() => { clearPhoto(); void startCamera(); }}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              Retake with camera
            </button>
            <button
              type="button"
              onClick={() => { clearPhoto(); fileInputRef.current?.click(); }}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              Choose different file
            </button>
            <button
              type="button"
              onClick={clearPhoto}
              className="rounded-lg border border-red-200 text-red-600 px-3 py-1.5 text-xs font-medium hover:bg-red-50"
            >
              Remove
            </button>
          </div>
        </div>
      )}

      {!stream && !photoPreview && (
        <div className="flex flex-wrap gap-2 items-center">
          <button
            type="button"
            onClick={() => void startCamera()}
            disabled={starting || compressing}
            className="rounded-lg bg-navy-700 hover:bg-navy-800 text-white px-3.5 py-2 text-sm font-medium disabled:opacity-60 transition shadow-sm"
          >
            {starting ? "Starting camera…" : "📷 Take photo"}
          </button>

          <span className="text-xs text-slate-400 font-medium">or</span>

          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={starting || compressing}
            className="rounded-lg border border-slate-300 px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 transition shadow-sm disabled:opacity-60"
          >
            📁 Upload image
          </button>

          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="hidden"
            onChange={handleFileSelect}
          />
        </div>
      )}

      {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    </section>
  );
}
