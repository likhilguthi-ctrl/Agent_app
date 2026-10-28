"use client";

import React, { useId, useState, useEffect } from "react";

interface IndianMobileInputProps {
  label?: string;
  value: string; // Accepts "+91...", 10-digit number, or empty string
  onChange: (e164Value: string, tenDigits: string, isValid: boolean) => void;
  error?: string;
  hint?: string;
  disabled?: boolean;
  required?: boolean;
  id?: string;
  name?: string;
  placeholder?: string;
  className?: string;
}

/**
 * Extracts exactly up to 10 Indian mobile digits from any raw format.
 * Handles:
 *  - "9876543210" -> "9876543210"
 *  - "+919876543210" -> "9876543210"
 *  - "+91 98765 43210" -> "9876543210"
 *  - "+91" or "" -> ""
 *  - "919876543210" (12 digits starting with 91 followed by 6-9) -> "9876543210"
 *  - "09876543210" (11 digits starting with 0 followed by 6-9) -> "9876543210"
 */
export function extractTenDigits(raw: string): string {
  if (!raw) return "";
  const trimmed = raw.trim();

  // If starts with +91, strip +91 first
  let candidate = trimmed;
  if (candidate.startsWith("+91")) {
    candidate = candidate.slice(3);
  } else if (candidate.startsWith("+")) {
    candidate = candidate.slice(1);
  }

  // Strip all non-digit characters
  const digits = candidate.replace(/\D/g, "");

  // 12 digits starting with 91 followed by valid mobile starting digit [6-9]
  if (digits.length === 12 && digits.startsWith("91") && /^[6-9]/.test(digits.slice(2))) {
    return digits.slice(2, 12);
  }

  // 11 digits starting with 0 followed by valid mobile starting digit [6-9]
  if (digits.length === 11 && digits.startsWith("0") && /^[6-9]/.test(digits.slice(1))) {
    return digits.slice(1, 11);
  }

  return digits.slice(0, 10);
}

/**
 * Checks if the 10-digit number represents a valid Indian mobile number:
 * Exactly 10 numeric digits starting with 6, 7, 8, or 9.
 */
export function isValidIndianMobile(tenDigits: string): boolean {
  return /^[6-9]\d{9}$/.test(tenDigits);
}

export default function IndianMobileInput({
  label,
  value,
  onChange,
  error,
  hint,
  disabled = false,
  required = false,
  id,
  name,
  placeholder = "9876543210",
  className = "",
}: IndianMobileInputProps) {
  const generatedId = useId();
  const inputId = id || generatedId;

  // Local state strictly holds only the 10 local digits
  const [localDigits, setLocalDigits] = useState<string>(() => extractTenDigits(value));
  const [pasteError, setPasteError] = useState<string | null>(null);

  // Synchronize local input state whenever the external value prop changes
  useEffect(() => {
    const extracted = extractTenDigits(value);
    setLocalDigits((prev) => (extracted !== prev ? extracted : prev));
  }, [value]);

  const isValid = isValidIndianMobile(localDigits);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setPasteError(null);
    const raw = e.target.value;
    // Keep only digits, maximum 10 digits
    const cleaned = raw.replace(/\D/g, "").slice(0, 10);
    setLocalDigits(cleaned);

    const valid = isValidIndianMobile(cleaned);
    const e164 = cleaned.length === 10 ? `+91${cleaned}` : (cleaned ? `+91${cleaned}` : "");
    onChange(e164, cleaned, valid);
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
    e.preventDefault();
    setPasteError(null);

    const pastedText = e.clipboardData.getData("text").trim();
    if (!pastedText) return;

    // Check for foreign country codes (e.g. +1, +44)
    if (pastedText.startsWith("+") && !pastedText.startsWith("+91")) {
      setPasteError("Invalid country code. Only Indian (+91) mobile numbers are supported.");
      return;
    }

    let candidate = pastedText;
    if (candidate.startsWith("+91")) {
      candidate = candidate.slice(3);
    }

    const digitsOnly = candidate.replace(/\D/g, "");
    let normalized = digitsOnly;

    // 12-digit prefixed with 91 followed by [6-9]
    if (digitsOnly.length === 12 && digitsOnly.startsWith("91") && /^[6-9]/.test(digitsOnly.slice(2))) {
      normalized = digitsOnly.slice(2);
    } else if (digitsOnly.length === 11 && digitsOnly.startsWith("0") && /^[6-9]/.test(digitsOnly.slice(1))) {
      normalized = digitsOnly.slice(1);
    }

    // Reject malformed or invalid lengths rather than silently slicing
    if (normalized.length !== 10) {
      setPasteError(
        digitsOnly.length > 10
          ? `Pasted number is too long (${digitsOnly.length} digits). Please paste a 10-digit Indian mobile number.`
          : `Pasted number is too short (${digitsOnly.length} digits). Please paste a 10-digit Indian mobile number.`
      );
      return;
    }

    const valid = isValidIndianMobile(normalized);
    setLocalDigits(normalized);
    const e164 = `+91${normalized}`;
    onChange(e164, normalized, valid);
  };

  const activeError = pasteError || error;

  return (
    <div className={`block ${className}`}>
      {label && (
        <label htmlFor={inputId} className="block text-sm font-medium text-slate-700 mb-1.5">
          {label}
          {required && <span className="text-emerald-600"> *</span>}
        </label>
      )}

      <div className="flex rounded-lg shadow-sm">
        <span className="inline-flex items-center px-3.5 rounded-l-lg border border-r-0 border-slate-300 bg-slate-100 text-slate-700 font-semibold text-sm select-none">
          🇮🇳 +91
        </span>
        <input
          id={inputId}
          name={name}
          type="tel"
          inputMode="numeric"
          autoComplete="tel-national"
          pattern="[6-9][0-9]{9}"
          maxLength={10}
          value={localDigits}
          onChange={handleInputChange}
          onPaste={handlePaste}
          disabled={disabled}
          placeholder={placeholder}
          className={`flex-1 min-w-0 rounded-none rounded-r-lg border ${
            activeError
              ? "border-red-300 focus:border-red-400 focus:ring-red-100"
              : "border-slate-300 focus:border-navy-400 focus:ring-navy-100"
          } bg-white px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 outline-none focus:ring-2 transition disabled:bg-slate-50 disabled:text-slate-500`}
        />
      </div>

      {hint && !activeError && (
        <span className="mt-1 block text-xs text-slate-500">{hint}</span>
      )}
      {activeError && (
        <span className="mt-1 block text-xs text-red-600 font-medium">{activeError}</span>
      )}
      {!activeError && localDigits.length > 0 && localDigits.length < 10 && (
        <span className="mt-1 block text-xs text-amber-600">
          Enter 10-digit mobile number ({10 - localDigits.length} remaining)
        </span>
      )}
      {!activeError && localDigits.length === 10 && !isValid && (
        <span className="mt-1 block text-xs text-red-600">
          Indian mobile numbers must start with 6, 7, 8, or 9
        </span>
      )}
    </div>
  );
}
