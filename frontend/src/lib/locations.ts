export const SUPPORTED_STATES = ["Andhra Pradesh", "Telangana"] as const;
export type SupportedState = (typeof SUPPORTED_STATES)[number];

export const CANONICAL_LOCATIONS: Record<SupportedState, { code: string; name: string }[]> = {
  "Andhra Pradesh": [
    { code: "AP-ALLURI-SITHARAMA-RAJU", name: "Alluri Sitharama Raju" },
    { code: "AP-ANAKAPALLI", name: "Anakapalli" },
    { code: "AP-ANANTHAPURAMU", name: "Ananthapuramu" },
    { code: "AP-ANNAMAYYA", name: "Annamayya" },
    { code: "AP-BAPATLA", name: "Bapatla" },
    { code: "AP-CHITTOOR", name: "Chittoor" },
    { code: "AP-DR-BR-AMBEDKAR-KONASEEMA", name: "Dr. B. R. Ambedkar Konaseema" },
    { code: "AP-EAST-GODAVARI", name: "East Godavari" },
    { code: "AP-ELURU", name: "Eluru" },
    { code: "AP-GUNTUR", name: "Guntur" },
    { code: "AP-KAKINADA", name: "Kakinada" },
    { code: "AP-KRISHNA", name: "Krishna" },
    { code: "AP-KURNOOL", name: "Kurnool" },
    { code: "AP-NANDYAL", name: "Nandyal" },
    { code: "AP-NTR", name: "NTR" },
    { code: "AP-PALNADU", name: "Palnadu" },
    { code: "AP-PARVATHIPURAM-MANYAM", name: "Parvathipuram Manyam" },
    { code: "AP-PRAKASAM", name: "Prakasam" },
    { code: "AP-SRI-POTTI-SRIRAMULU-NELLORE", name: "Sri Potti Sriramulu Nellore" },
    { code: "AP-SRI-SATHYA-SAI", name: "Sri Sathya Sai" },
    { code: "AP-SRIKAKULAM", name: "Srikakulam" },
    { code: "AP-TIRUPATI", name: "Tirupati" },
    { code: "AP-VISAKHAPATNAM", name: "Visakhapatnam" },
    { code: "AP-VIZIANAGARAM", name: "Vizianagaram" },
    { code: "AP-WEST-GODAVARI", name: "West Godavari" },
    { code: "AP-YSR-KADAPA", name: "YSR Kadapa" },
  ],
  "Telangana": [
    { code: "TS-ADILABAD", name: "Adilabad" },
    { code: "TS-BHADRADRI-KOTHAGUDEM", name: "Bhadradri Kothagudem" },
    { code: "TS-HANUMAKONDA", name: "Hanumakonda" },
    { code: "TS-HYDERABAD", name: "Hyderabad" },
    { code: "TS-JAGTIAL", name: "Jagtial" },
    { code: "TS-JANGAON", name: "Jangaon" },
    { code: "TS-JAYASHANKAR-BHUPALPALLY", name: "Jayashankar Bhupalpally" },
    { code: "TS-JOGULAMBA-GADWAL", name: "Jogulamba Gadwal" },
    { code: "TS-KAMAREDDY", name: "Kamareddy" },
    { code: "TS-KARIMNAGAR", name: "Karimnagar" },
    { code: "TS-KHAMMAM", name: "Khammam" },
    { code: "TS-KUMURAM-BHEEM-ASIFABAD", name: "Kumuram Bheem Asifabad" },
    { code: "TS-MAHABUBABAD", name: "Mahabubabad" },
    { code: "TS-MAHABUBNAGAR", name: "Mahabubnagar" },
    { code: "TS-MANCHERIAL", name: "Mancherial" },
    { code: "TS-MEDAK", name: "Medak" },
    { code: "TS-MEDCHAL-MALKAJGIRI", name: "Medchal-Malkajgiri" },
    { code: "TS-MULUGU", name: "Mulugu" },
    { code: "TS-NAGARKURNOOL", name: "Nagarkurnool" },
    { code: "TS-NALGONDA", name: "Nalgonda" },
    { code: "TS-NARAYANPET", name: "Narayanpet" },
    { code: "TS-NIRMAL", name: "Nirmal" },
    { code: "TS-NIZAMABAD", name: "Nizamabad" },
    { code: "TS-PEDDAPALLI", name: "Peddapalli" },
    { code: "TS-RAJANNA-SIRCILLA", name: "Rajanna Sircilla" },
    { code: "TS-RANGA-REDDY", name: "Rangareddy" },
    { code: "TS-SANGAREDDY", name: "Sangareddy" },
    { code: "TS-SIDDIPET", name: "Siddipet" },
    { code: "TS-SURYAPET", name: "Suryapet" },
    { code: "TS-VIKARABAD", name: "Vikarabad" },
    { code: "TS-WANAPARTHY", name: "Wanaparthy" },
    { code: "TS-WARANGAL", name: "Warangal" },
    { code: "TS-YADADRI-BHUVANAGIRI", name: "Yadadri Bhuvanagiri" },
  ],
};

const DISTRICT_ALIASES: Record<string, string> = {
  "ranga reddy": "Rangareddy",
  "dr. b.r. ambedkar konaseema": "Dr. B. R. Ambedkar Konaseema",
  "dr. b. r. ambedkar konaseema": "Dr. B. R. Ambedkar Konaseema",
};

export function getDistrictsForState(state: string): string[] {
  const list = CANONICAL_LOCATIONS[state as SupportedState];
  if (!list) return [];
  return list.map((item) => item.name);
}

export function validateStateDistrict(state: string, district: string): boolean {
  const districts = getDistrictsForState(state);
  if (!districts.length) return false;
  const normalized = district.trim().toLowerCase();
  const canonical = DISTRICT_ALIASES[normalized] || district.trim();
  return districts.some((d) => d.toLowerCase() === canonical.toLowerCase());
}

