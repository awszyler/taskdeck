/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx,js,jsx}",
  ],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        info: {
          DEFAULT: "hsl(var(--info))",
          foreground: "hsl(var(--info-foreground))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        // Awaiting-input cards: gentle amber halo (no border flicker, no
        // contrast jumps — just a soft glow that draws the eye).
        "pulse-amber": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgb(251 191 36 / 0)" },
          "50%": { boxShadow: "0 0 16px 0 rgb(251 191 36 / 0.4)" },
        },
        // Running cards: subtle horizontal shimmer along the bottom edge,
        // signals "something is happening" without being noisy.
        "shimmer-slow": {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        // Newly-arrived awaiting cards flash once when they appear so the
        // user notices the state transition without needing a toast.
        "flash-once": {
          "0%": { backgroundColor: "rgb(251 191 36 / 0.4)" },
          "100%": { backgroundColor: "rgb(254 243 199 / 0)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "pulse-amber": "pulse-amber 2s ease-in-out infinite",
        "shimmer-slow": "shimmer-slow 5s linear infinite",
        "flash-once": "flash-once 0.6s ease-out 1",
      },
    },
  },
  plugins: [
    require("@tailwindcss/typography"),
  ],
}
