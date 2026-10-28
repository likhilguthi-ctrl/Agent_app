import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        navy: {
          50: "#eef1f7",
          100: "#d6ddec",
          400: "#3d537e",
          600: "#233657",
          700: "#1b2a4a",
          800: "#141f38",
          900: "#0d1526",
        },
        emerald: {
          50: "#e9f9f3",
          100: "#c8f0e0",
          500: "#0f9d77",
          600: "#0c7e5f",
          700: "#0a6349",
        },
        sand: {
          50: "#f7f8fa",
          100: "#eef0f3",
        },
        bronze: "#b08d57",
        silver: "#9aa3ad",
        gold: "#c99a2e",
      },
      fontFamily: {
        display: ["var(--font-display)"],
        body: ["var(--font-body)"],
        mono: ["var(--font-mono)"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(16,24,40,0.04), 0 1px 3px rgba(16,24,40,0.06)",
        soft: "0 8px 24px rgba(16,24,40,0.06)",
      },
      borderRadius: {
        xl2: "1.1rem",
      },
    },
  },
  plugins: [],
};
export default config;
