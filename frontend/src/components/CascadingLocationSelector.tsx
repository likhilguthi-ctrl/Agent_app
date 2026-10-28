"use client";

import React, { useEffect, useState } from "react";
import { fetchBackendApi } from "@/lib/backend-api";

export interface LocationSelection {
  state: string;
  district: string;
  mandal: string;
  state_id?: string;
  district_id?: string;
  mandal_id?: string;
}

interface StateItem {
  id: string;
  name: string;
  code: string;
}

interface DistrictItem {
  id: string;
  name: string;
  state_id: string;
}

interface MandalItem {
  id: string;
  name: string;
  district_id: string;
}

interface CascadingLocationSelectorProps {
  value?: Partial<LocationSelection>;
  onChange: (val: LocationSelection) => void;
  required?: boolean;
  disabled?: boolean;
  restrictedStateId?: string;
  restrictedStateIds?: string[];
  restrictedDistrictIds?: string[];
  restrictedMandalIds?: string[];
  className?: string;
}

export default function CascadingLocationSelector({
  value,
  onChange,
  required = true,
  disabled = false,
  restrictedStateId,
  restrictedStateIds,
  restrictedDistrictIds,
  restrictedMandalIds,
  className = "",
}: CascadingLocationSelectorProps) {
  const [states, setStates] = useState<StateItem[]>([]);
  const [districts, setDistricts] = useState<DistrictItem[]>([]);
  const [mandals, setMandals] = useState<MandalItem[]>([]);

  const [selectedStateId, setSelectedStateId] = useState<string>(value?.state_id || "");
  const [selectedDistrictId, setSelectedDistrictId] = useState<string>(value?.district_id || "");
  const [selectedMandalId, setSelectedMandalId] = useState<string>(value?.mandal_id || "");

  const [loadingStates, setLoadingStates] = useState(false);
  const [loadingDistricts, setLoadingDistricts] = useState(false);
  const [loadingMandals, setLoadingMandals] = useState(false);

  // 1. Fetch available states
  useEffect(() => {
    setLoadingStates(true);
    fetchBackendApi("api/v1/locations/states", { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error("Failed to load states");
        const data = await res.json();
        let items: StateItem[] = Array.isArray(data) ? data : data.items || [];
        if (restrictedStateIds && restrictedStateIds.length > 0) {
          items = items.filter((s) => restrictedStateIds.includes(s.id));
        } else if (restrictedStateId) {
          items = items.filter((s) => s.id === restrictedStateId);
        }
        setStates(items);
        if (items.length === 1 && !selectedStateId) {
          setSelectedStateId(items[0].id);
        }
      })
      .catch(() => {
        setStates([
          { id: "33333333-3333-3333-3333-333333333301", name: "Andhra Pradesh", code: "AP" },
          { id: "33333333-3333-3333-3333-333333333302", name: "Telangana", code: "TG" },
        ]);
      })
      .finally(() => setLoadingStates(false));
  }, [restrictedStateId, restrictedStateIds]);

  // 2. Fetch districts when state changes
  useEffect(() => {
    if (!selectedStateId) {
      setDistricts([]);
      setSelectedDistrictId("");
      return;
    }
    setLoadingDistricts(true);
    fetchBackendApi(`api/v1/locations/districts?state_id=${selectedStateId}`, { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error("Failed to load districts");
        const data = await res.json();
        let items: DistrictItem[] = Array.isArray(data) ? data : data.items || [];
        if (restrictedDistrictIds && restrictedDistrictIds.length > 0) {
          items = items.filter((d) => restrictedDistrictIds.includes(d.id));
        }
        setDistricts(items);
      })
      .catch(() => setDistricts([]))
      .finally(() => setLoadingDistricts(false));
  }, [selectedStateId, restrictedDistrictIds]);

  // 3. Fetch mandals when district changes
  useEffect(() => {
    if (!selectedDistrictId) {
      setMandals([]);
      setSelectedMandalId("");
      return;
    }
    setLoadingMandals(true);
    fetchBackendApi(`api/v1/locations/mandals?district_id=${selectedDistrictId}`, { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error("Failed to load mandals");
        const data = await res.json();
        let items: MandalItem[] = Array.isArray(data) ? data : data.items || [];
        if (restrictedMandalIds && restrictedMandalIds.length > 0) {
          items = items.filter((m) => restrictedMandalIds.includes(m.id));
        }
        setMandals(items);
      })
      .catch(() => setMandals([]))
      .finally(() => setLoadingMandals(false));
  }, [selectedDistrictId, restrictedMandalIds]);

  const handleStateChange = (stId: string) => {
    setSelectedStateId(stId);
    setSelectedDistrictId("");
    setSelectedMandalId("");
    const stObj = states.find((s) => s.id === stId);
    onChange({
      state: stObj?.name || "",
      district: "",
      mandal: "",
      state_id: stId || undefined,
      district_id: undefined,
      mandal_id: undefined,
    });
  };

  const handleDistrictChange = (distId: string) => {
    setSelectedDistrictId(distId);
    setSelectedMandalId("");
    const stObj = states.find((s) => s.id === selectedStateId);
    const distObj = districts.find((d) => d.id === distId);
    onChange({
      state: stObj?.name || "",
      district: distObj?.name || "",
      mandal: "",
      state_id: selectedStateId || undefined,
      district_id: distId || undefined,
      mandal_id: undefined,
    });
  };

  const handleMandalChange = (mId: string) => {
    setSelectedMandalId(mId);
    const stObj = states.find((s) => s.id === selectedStateId);
    const distObj = districts.find((d) => d.id === selectedDistrictId);
    const mObj = mandals.find((m) => m.id === mId);
    onChange({
      state: stObj?.name || "",
      district: distObj?.name || "",
      mandal: mObj?.name || "",
      state_id: selectedStateId || undefined,
      district_id: selectedDistrictId || undefined,
      mandal_id: mId || undefined,
    });
  };

  return (
    <div className={`grid grid-cols-1 sm:grid-cols-3 gap-4 ${className}`}>
      {/* State Dropdown */}
      <div>
        <label className="block text-xs font-semibold text-slate-700 mb-1">
          State {required && <span className="text-red-500">*</span>}
        </label>
        <select
          value={selectedStateId}
          disabled={disabled || loadingStates}
          onChange={(e) => handleStateChange(e.target.value)}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs sm:text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-navy-400 disabled:bg-slate-100 disabled:text-slate-400"
        >
          <option value="">{loadingStates ? "Loading states..." : "-- Select State --"}</option>
          {states.map((st) => (
            <option key={st.id} value={st.id}>
              {st.name} ({st.code})
            </option>
          ))}
        </select>
      </div>

      {/* District Dropdown */}
      <div>
        <label className="block text-xs font-semibold text-slate-700 mb-1">
          District {required && <span className="text-red-500">*</span>}
        </label>
        <select
          value={selectedDistrictId}
          disabled={disabled || !selectedStateId || loadingDistricts}
          onChange={(e) => handleDistrictChange(e.target.value)}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs sm:text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-navy-400 disabled:bg-slate-100 disabled:text-slate-400"
        >
          <option value="">
            {!selectedStateId
              ? "Select State First"
              : loadingDistricts
              ? "Loading districts..."
              : "-- Select District --"}
          </option>
          {districts.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </div>

      {/* Mandal Dropdown */}
      <div>
        <label className="block text-xs font-semibold text-slate-700 mb-1">
          Mandal (Sub-District) {required && <span className="text-red-500">*</span>}
        </label>
        <select
          value={selectedMandalId}
          disabled={disabled || !selectedDistrictId || loadingMandals}
          onChange={(e) => handleMandalChange(e.target.value)}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs sm:text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-navy-400 disabled:bg-slate-100 disabled:text-slate-400"
        >
          <option value="">
            {!selectedDistrictId
              ? "Select District First"
              : loadingMandals
              ? "Loading mandals..."
              : "-- Select Mandal --"}
          </option>
          {mandals.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
