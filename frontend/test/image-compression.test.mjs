import assert from "node:assert/strict";
import { test } from "node:test";
import {
  formatFileSize,
  DEFAULT_COMPRESSION_OPTIONS,
  compressImage,
} from "../src/lib/image-compression.ts";

test("Image Compression - formatFileSize formats bytes into readable units", () => {
  assert.equal(formatFileSize(0), "0 B");
  assert.equal(formatFileSize(512), "512 B");
  assert.equal(formatFileSize(1024), "1.0 KB");
  assert.equal(formatFileSize(300 * 1024), "300.0 KB");
  assert.equal(formatFileSize(500 * 1024), "500.0 KB");
  assert.equal(formatFileSize(1.5 * 1024 * 1024), "1.50 MB");
  assert.equal(formatFileSize(5 * 1024 * 1024), "5.00 MB");
});

test("Image Compression - DEFAULT_COMPRESSION_OPTIONS conforms to Cloudflare R2 specifications", () => {
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.maxDimension, 1280, "Max dimension bound must be 1280px");
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.targetSizeBytes, 300 * 1024, "Target size must be 300 KB");
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.maxSizeBytes, 500 * 1024, "Hard maximum size must be 500 KB");
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.initialQuality, 0.70, "Initial JPEG quality must start at 0.70");
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.minQuality, 0.35, "Minimum stepped quality must reach 0.35");
  assert.equal(DEFAULT_COMPRESSION_OPTIONS.qualityStep, 0.10, "Stepping interval must be 0.10");
});

test("Image Compression - Non-image documents (e.g. PDF) bypass compression safely", async () => {
  const mockPdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]); // %PDF
  const mockPdfFile = new File([mockPdfBytes], "document.pdf", {
    type: "application/pdf",
  });

  const result = await compressImage(mockPdfFile);
  assert.equal(result.file.name, "document.pdf");
  assert.equal(result.file.type, "application/pdf");
  assert.equal(result.originalBytes, 4);
  assert.equal(result.compressedBytes, 4);
  assert.equal(result.savingsPercent, 0);
  assert.equal(result.width, 0);
  assert.equal(result.height, 0);
});

