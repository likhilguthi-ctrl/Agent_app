"use client";

import React, { useEffect, useState } from "react";
import { fetchBackendApi } from "@/lib/backend-api";

export interface MandalScopeConfig {
  district_id: string;
  all_mandals: boolean;
  mandal_ids: string[];
}

export interface StateScopeConfig {
  state_id: string;
  all_districts: boolean;
  district_ids: string[];
  mandal_configs: MandalScopeConfig[];
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

interface ManagerLocationAssignmentSelectorProps {
  onChange: (scopes: StateScopeConfig[], selectedStateNames: string[]) => void;
  disabled?: boolean;
}

export default function ManagerLocationAssignmentSelector({
  onChange,
  disabled = false,
}: ManagerLocationAssignmentSelectorProps) {
  const [availableStates, setAvailableStates] = useState<StateItem[]>([]);
  const [selectedStates, setSelectedStates] = useState<Record<string, boolean>>({});
  const [allDistrictsByState, setAllDistrictsByState] = useState<Record<string, boolean>>({});
  const [districtsByState, setDistrictsByState] = useState<Record<string, DistrictItem[]>>({});
  const [selectedDistrictsByState, setSelectedDistrictsByState] = useState<Record<string, string[]>>({});
  const [loadingDistricts, setLoadingDistricts] = useState<Record<string, boolean>>({});

  // Fetch States on load
  useEffect(() => {
    fetchBackendApi("api/v1/locations/states", { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error("Failed to load states");
        const data = await res.json();
        const items: StateItem[] = Array.isArray(data) ? data : data.items || [];
        setAvailableStates(items);
        // Default select the first state
        if (items.length > 0) {
          const first = items[0];
          setSelectedStates({ [first.id]: true });
          setAllDistrictsByState({ [first.id]: true });
          setSelectedDistrictsByState({ [first.id]: [] });
        }
      })
      .catch(() => {
        const fallback = [
          { id: "33333333-3333-3333-3333-333333333301", name: "Andhra Pradesh", code: "AP" },
          { id: "33333333-3333-3333-3333-333333333302", name: "Telangana", code: "TG" },
        ];
        setAvailableStates(fallback);
        setSelectedStates({ [fallback[0].id]: true });
        setAllDistrictsByState({ [fallback[0].id]: true });
        setSelectedDistrictsByState({ [fallback[0].id]: [] });
      });
  }, []);

  // Fetch districts when state is toggled on and allDistricts is false
  const fetchDistrictsForState = (stateId: string) => {
    if (districtsByState[stateId]) return;
    setLoadingDistricts((prev) => ({ ...prev, [stateId]: true }));
    fetchBackendApi(`api/v1/locations/districts?state_id=${stateId}`, { credentials: "same-origin" })
      .then(async (res) => {
        if (!res.ok) throw new Error("Failed to load districts");
        const data = await res.json();
        setDistrictsByState((prev) => ({ ...prev, [stateId]: Array.isArray(data) ? data : data.items || [] }));
      })
      .catch(() => {
        setDistrictsByState((prev) => ({ ...prev, [stateId]: [] }));
      })
      .finally(() => {
        setLoadingDistricts((prev) => ({ ...prev, [stateId]: false }));
      });
  };

  // Recompute and fire onChange
  useEffect(() => {
    const scopes: StateScopeConfig[] = [];
    const stateNames: string[] = [];

    for (const st of availableStates) {
      if (selectedStates[st.id]) {
        stateNames.push(st.name);
        const isAllDistricts = allDistrictsByState[st.id] ?? true;
        const selectedDists = selectedDistrictsByState[st.id] || [];

        scopes.push({
          state_id: st.id,
          all_districts: isAllDistricts,
          district_ids: isAllDistricts ? [] : selectedDists,
          mandal_configs: [],
        });
      }
    }
    onChange(scopes, stateNames);
  }, [selectedStates, allDistrictsByState, selectedDistrictsByState, availableStates]);

  const toggleState = (stateId: string) => {
    setSelectedStates((prev) => {
      const next = { ...prev, [stateId]: !prev[stateId] };
      if (next[stateId] && allDistrictsByState[stateId] === undefined) {
        setAllDistrictsByState((a) => ({ ...a, [stateId]: true }));
        setSelectedDistrictsByState((d) => ({ ...d, [stateId]: [] }));
      }
      return next;
    });
  };

  const toggleAllDistricts = (stateId: string) => {
    setAllDistrictsByState((prev) => {
      const nextVal = !prev[stateId];
      if (!nextVal) {
        fetchDistrictsForState(stateId);
      }
      return { ...prev, [stateId]: nextVal };
    });
  };

  const toggleDistrictSelection = (stateId: string, distId: string) => {
    setSelectedDistrictsByState((prev) => {
      const current = prev[stateId] || [];
      const next = current.includes(distId)
        ? current.filter((id) => id !== distId)
        : [...current, distId];
      return { ...prev, [stateId]: next };
    });
  };

  return (
    <div className="space-y-4 rounded-xl border border-slate-200 bg-sand-50/40 p-4">
      <div>
        <label className="block text-xs font-bold uppercase tracking-wider text-slate-700 mb-1">
          Assigned Operating States & Geographic Authority
        </label>
        <p className="text-xs text-slate-500 mb-3">
          Assign Manager to Andhra Pradesh, Telangana, or both states. You can grant access to all districts or restrict to selected districts.
        </p>

        {/* State Checkboxes */}
        <div className="flex flex-wrap gap-3">
          {availableStates.map((st) => {
            const isChecked = Boolean(selectedStates[st.id]);
            return (
              <button
                key={st.id}
                type="button"
                disabled={disabled}
                onClick={() => toggleState(st.id)}
                className={`flex items-center gap-2 px-3.5 py-2 rounded-lg border text-xs font-semibold transition ${
                  isChecked
                    ? "bg-navy-800 border-navy-900 text-white shadow-sm"
                    : "bg-white border-slate-300 text-slate-700 hover:border-slate-400"
                }`}
              >
                <span>{isChecked ? "✓" : "○"}</span>
                <span>{st.name} ({st.code})</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Per-State District Scoping Panel */}
      {availableStates.filter((st) => selectedStates[st.id]).map((st) => {
        const isAllDistricts = allDistrictsByState[st.id] ?? true;
        const stateDistricts = districtsByState[st.id] || [];
        const selectedDists = selectedDistrictsByState[st.id] || [];
        const isLoading = loadingDistricts[st.id];

        return (
          <div key={st.id} className="mt-3 p-3.5 bg-white rounded-lg border border-slate-200 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2">
              <span className="text-xs font-bold text-navy-900 flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-emerald-500" />
                {st.name} District Access Scope
              </span>
              <label className="flex items-center gap-2 cursor-pointer text-xs font-medium text-slate-700">
                <input
                  type="checkbox"
                  checked={isAllDistricts}
                  disabled={disabled}
                  onChange={() => toggleAllDistricts(st.id)}
                  className="rounded text-navy-700 focus:ring-navy-500"
                />
                <span>All Districts (Statewide Access)</span>
              </label>
            </div>

            {!isAllDistricts && (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>Select authorized districts ({selectedDists.length} selected):</span>
                  {selectedDists.length === 0 && (
                    <span className="text-amber-600 font-semibold">* At least one district required</span>
                  )}
                </div>

                {isLoading ? (
                  <p className="text-xs text-slate-400 py-2">Loading districts for {st.name}...</p>
                ) : (
                  <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2 max-h-48 overflow-y-auto p-1 border border-slate-100 rounded-lg bg-slate-50/50">
                    {stateDistricts.map((d) => {
                      const isDistChecked = selectedDists.includes(d.id);
                      return (
                        <label
                          key={d.id}
                          className={`flex items-center gap-1.5 px-2 py-1.5 rounded border text-xs cursor-pointer select-none transition ${
                            isDistChecked
                              ? "bg-navy-50 border-navy-300 text-navy-800 font-semibold"
                              : "bg-white border-slate-200 text-slate-600 hover:border-slate-300"
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={isDistChecked}
                            disabled={disabled}
                            onChange={() => toggleDistrictSelection(st.id, d.id)}
                            className="rounded text-navy-700 focus:ring-navy-400"
                          />
                          <span className="truncate">{d.name}</span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
