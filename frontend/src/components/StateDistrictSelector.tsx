"use client";

import React from "react";
import {
  SUPPORTED_STATES,
  SupportedState,
  getDistrictsForState,
} from "@/lib/locations";

interface StateDistrictSelectorProps {
  mode?: "admin" | "agent" | "general";
  stateValue: string;
  districtValue?: string;
  onStateChange: (state: string) => void;
  onDistrictChange?: (district: string) => void;
  stateError?: string;
  districtError?: string;
  disabled?: boolean;
  required?: boolean;
  className?: string;
}

export default function StateDistrictSelector({
  mode = "agent",
  stateValue,
  districtValue = "",
  onStateChange,
  onDistrictChange,
  stateError,
  districtError,
  disabled = false,
  required = true,
  className = "",
}: StateDistrictSelectorProps) {
  const showDistrict = mode === "agent" || (mode === "general" && !!onDistrictChange);
  const districts = stateValue ? getDistrictsForState(stateValue) : [];

  const handleStateSelect = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newState = e.target.value;
    onStateChange(newState);
    if (onDistrictChange) {
      onDistrictChange(""); // Clear district on state change as required by Phase 2
    }
  };

  const handleDistrictSelect = (e: React.ChangeEvent<HTMLSelectElement>) => {
    if (onDistrictChange) {
      onDistrictChange(e.target.value);
    }
  };

  return (
    <div className={`space-y-4 ${className}`}>
      {/* State Selection */}
      <div>
        <label className="block text-sm font-medium text-slate-700 mb-1.5">
          Operating State
          {required && <span className="text-emerald-600"> *</span>}
        </label>
        <select
          value={stateValue}
          onChange={handleStateSelect}
          disabled={disabled}
          className={`w-full rounded-lg border ${
            stateError
              ? "border-red-300 focus:border-red-400 focus:ring-red-100"
              : "border-slate-300 focus:border-navy-400 focus:ring-navy-100"
          } bg-white px-3.5 py-2.5 text-sm text-slate-900 outline-none focus:ring-2 transition disabled:bg-slate-50 disabled:text-slate-500`}
        >
          <option value="">Select State</option>
          {SUPPORTED_STATES.map((st) => (
            <option key={st} value={st}>
              {st}
            </option>
          ))}
        </select>
        {stateError && (
          <span className="mt-1 block text-xs text-red-600">{stateError}</span>
        )}
      </div>

      {/* District Selection (Visible for Agent, hidden for Admin) */}
      {showDistrict && (
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1.5">
            Operating District
            {required && <span className="text-emerald-600"> *</span>}
          </label>
          <select
            value={districtValue}
            onChange={handleDistrictSelect}
            disabled={disabled || !stateValue}
            className={`w-full rounded-lg border ${
              districtError
                ? "border-red-300 focus:border-red-400 focus:ring-red-100"
                : "border-slate-300 focus:border-navy-400 focus:ring-navy-100"
            } bg-white px-3.5 py-2.5 text-sm text-slate-900 outline-none focus:ring-2 transition disabled:bg-slate-50 disabled:text-slate-500`}
          >
            <option value="">
              {!stateValue ? "Please select a state first" : "Select District"}
            </option>
            {districts.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          {districtError && (
            <span className="mt-1 block text-xs text-red-600">{districtError}</span>
          )}
          {stateValue && districts.length > 0 && !districtError && (
            <span className="mt-1 block text-xs text-slate-400">
              {districts.length} canonical districts available for {stateValue}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

