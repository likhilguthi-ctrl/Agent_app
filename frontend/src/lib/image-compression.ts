/**
 * Client-side image compression and resizing utility for Cloudflare R2 uploads.
 *
 * Enforces:
 * - Max dimension bound of 1280px (preserves aspect ratio, no upscaling)
 * - Conversion to standard JPEG format (with white background for transparency)
 * - Iterative quality stepping from initial 0.70 down to 0.35 targeting <= 300 KB
 * - Hard limit enforcement of 500 KB
 * - Bypasses compression for non-image files
 * - Computes byte savings and dimension statistics for user-facing badges
 */

export interface ImageCompressionOptions {
  maxDimension?: number;
  targetSizeBytes?: number;
  maxSizeBytes?: number;
  initialQuality?: number;
  minQuality?: number;
  qualityStep?: number;
}

export interface CompressedImageResult {
  file: File;
  previewUrl: string;
  originalWidth: number;
  originalHeight: number;
  width: number;
  height: number;
  originalBytes: number;
  compressedBytes: number;
  savingsPercent: number;
  mimeType: string;
}

export const DEFAULT_COMPRESSION_OPTIONS: Required<ImageCompressionOptions> = {
  maxDimension: 1280,
  targetSizeBytes: 300 * 1024, // 300 KB
  maxSizeBytes: 500 * 1024,    // 500 KB
  initialQuality: 0.70,
  minQuality: 0.35,
  qualityStep: 0.10,
};

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);

    img.onload = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(img);
    };

    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("Failed to load image for compression. The file may be corrupt or an unsupported format."));
    };

    img.src = objectUrl;
  });
}

function canvasToBlob(
  canvas: HTMLCanvasElement,
  mimeType: string,
  quality: number
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) resolve(blob);
        else reject(new Error("Failed to encode canvas to image blob"));
      },
      mimeType,
      quality
    );
  });
}

function calculateTargetDimensions(
  naturalWidth: number,
  naturalHeight: number,
  maxDimension: number
): { width: number; height: number } {
  if (naturalWidth <= 0 || naturalHeight <= 0) {
    return { width: 1, height: 1 };
  }

  if (naturalWidth <= maxDimension && naturalHeight <= maxDimension) {
    return { width: naturalWidth, height: naturalHeight };
  }

  if (naturalWidth >= naturalHeight) {
    const width = maxDimension;
    const height = Math.max(1, Math.round((naturalHeight * maxDimension) / naturalWidth));
    return { width, height };
  } else {
    const height = maxDimension;
    const width = Math.max(1, Math.round((naturalWidth * maxDimension) / naturalHeight));
    return { width, height };
  }
}

function renderToCanvas(
  img: HTMLImageElement,
  targetWidth: number,
  targetHeight: number
): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = targetWidth;
  canvas.height = targetHeight;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    throw new Error("Browser does not support 2D canvas context for image processing.");
  }

  // Fill with solid white for transparent PNG/WebP conversions to JPEG
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, targetWidth, targetHeight);
  ctx.drawImage(img, 0, 0, targetWidth, targetHeight);
  return canvas;
}

/**
 * Compresses and scales an image File on the client side.
 */
export async function compressImage(
  file: File,
  customOptions?: ImageCompressionOptions
): Promise<CompressedImageResult> {
  const options = { ...DEFAULT_COMPRESSION_OPTIONS, ...customOptions };

  // If not an image (e.g., PDF document), bypass compression
  if (!file.type.startsWith("image/")) {
    const previewUrl = typeof URL !== "undefined" && URL.createObjectURL ? URL.createObjectURL(file) : "";
    return {
      file,
      previewUrl,
      originalWidth: 0,
      originalHeight: 0,
      width: 0,
      height: 0,
      originalBytes: file.size,
      compressedBytes: file.size,
      savingsPercent: 0,
      mimeType: file.type,
    };
  }

  const img = await loadImage(file);
  const originalWidth = img.naturalWidth;
  const originalHeight = img.naturalHeight;

  let { width, height } = calculateTargetDimensions(
    originalWidth,
    originalHeight,
    options.maxDimension
  );

  let canvas = renderToCanvas(img, width, height);

  // Stepped quality search from initialQuality down to minQuality
  let currentQuality = options.initialQuality;
  let blob: Blob | null = null;

  while (currentQuality >= options.minQuality - 0.001) {
    blob = await canvasToBlob(canvas, "image/jpeg", currentQuality);
    if (blob.size <= options.targetSizeBytes) {
      break;
    }
    if (currentQuality <= options.minQuality + 0.001) {
      break;
    }
    currentQuality = Math.max(options.minQuality, Number((currentQuality - options.qualityStep).toFixed(2)));
  }

  if (!blob) {
    throw new Error("Compression failed to produce output image.");
  }

  // If still exceeding hard limit of 500 KB, try secondary scaling down by 25%
  if (blob.size > options.maxSizeBytes) {
    const reducedWidth = Math.max(1, Math.round(width * 0.75));
    const reducedHeight = Math.max(1, Math.round(height * 0.75));
    canvas = renderToCanvas(img, reducedWidth, reducedHeight);
    width = reducedWidth;
    height = reducedHeight;

    currentQuality = options.minQuality;
    blob = await canvasToBlob(canvas, "image/jpeg", currentQuality);
  }

  if (blob.size > options.maxSizeBytes) {
    throw new Error(
      `Image size (${formatFileSize(blob.size)}) exceeds the maximum allowed limit of ${formatFileSize(options.maxSizeBytes)} after compression. Please choose a smaller photo.`
    );
  }

  const baseName = file.name.replace(/\.[^/.]+$/, "");
  const compressedFileName = `${baseName || "photo"}.jpg`;
  const compressedFile = new File([blob], compressedFileName, {
    type: "image/jpeg",
    lastModified: Date.now(),
  });

  const previewUrl = URL.createObjectURL(blob);
  const savingsPercent = file.size > blob.size
    ? Math.round(((file.size - blob.size) / file.size) * 100)
    : 0;

  return {
    file: compressedFile,
    previewUrl,
    originalWidth,
    originalHeight,
    width,
    height,
    originalBytes: file.size,
    compressedBytes: blob.size,
    savingsPercent,
    mimeType: "image/jpeg",
  };
}

