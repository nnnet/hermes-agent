import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `hermes_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Hermes Teal",
  description: "Classic dark teal — the canonical Hermes look",
  palette: {
    background: { hex: "#041c1c", alpha: 1 },
    midground: { hex: "#ffe6cb", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 189, 56, 0.35)",
    noiseOpacity: 1,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
};

/**
 * Mission Control — flat dark dashboard styled to match the Mission
 * Control project (github.com/builderz-labs/mission-control) as closely
 * as Hermes's theme system allows.
 *
 * Compared with the other Hermes presets this one is *aggressively
 * de-Hermes-ified*:
 *
 *   - midground is a near-white (#e6ebf0), not the accent — Hermes's
 *     DS cascade derives `--color-foreground` from midground, so body
 *     text needs to be readable greyscale. The cyan accent is bolted
 *     on via `colorOverrides` (primary/ring/accent/secondary).
 *   - All decorative chrome that ships in the default Hermes look is
 *     killed via `componentStyles` overrides: the diagonal clip-path
 *     borders on the sidebar/header/cards/tabs, the warm-glow vignette
 *     (set warmGlow: "transparent"), the SVG noise overlay
 *     (noiseOpacity: 0), and the filler-bg jpeg in `<Backdrop>` (set
 *     --component-backdrop-filler-opacity: 0).
 *   - `customCSS` retargets `--font-mondwest` from the bundled
 *     decorative display face to plain Inter, so sidebar nav items and
 *     section labels drop the retro-display vibe.
 *
 * Palette borrowed verbatim from MC's globals.css `.dark` block.
 */
export const missionControlTheme: DashboardTheme = {
  name: "mission-control",
  label: "Mission Control",
  description: "Flat dark — copies the Mission Control look (no glow, no grain, no chrome)",
  palette: {
    // MC --background (215 27% 4%)
    background: { hex: "#080b10", alpha: 1 },
    // MC's body text colour (--foreground, 210 20% 92%). Hermes drives
    // `--color-foreground` off `--midground`, so this MUST be a readable
    // greyscale — not the accent — or every label turns cyan.
    midground: { hex: "#e6ebf0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    // No warm vignette. Backdrop still renders the z-99 div but a
    // transparent gradient means there's nothing to see.
    warmGlow: "transparent",
    // Backdrop's SVG noise layer reads this multiplier; 0 disables.
    noiseOpacity: 0,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    // MC's Next.js stack defaults to Inter; JetBrains Mono is its
    // canonical mono face. Both pulled from Google Fonts on demand —
    // fontUrl is injected as a <link> by ThemeProvider.
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontDisplay: `"Inter", ${SYSTEM_SANS}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    baseSize: "14px",
    lineHeight: "1.5",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.5rem",
    density: "comfortable",
  },
  // Kill every Hermes-DS decorative override. `clip-path: none` /
  // `border-image: none` resolve cleanly through the inline styles on
  // App.tsx's sidebar/header and Card. Background overrides pin flat
  // shades so we don't inherit teal-tinted cascade defaults.
  componentStyles: {
    sidebar: {
      background: "#0a0d12",
      clipPath: "none",
      borderImage: "none",
    },
    header: {
      background: "#0a0d12",
      clipPath: "none",
      borderImage: "none",
    },
    card: {
      background: "#10141c",
      clipPath: "none",
      borderImage: "none",
      boxShadow: "none",
    },
    tab: {
      clipPath: "none",
    },
    backdrop: {
      // Hide the filler jpeg by zeroing its z-2 div opacity. Combined
      // with warmGlow: "transparent" and noiseOpacity: 0, the canvas
      // reduces to a single flat `--background-base` fill underneath.
      fillerOpacity: "0",
    },
  },
  // Mondwest is the retro display face Hermes uses for nav/section labels.
  // Retarget the CSS var to Inter so MC theme reads as one consistent face.
  // Also force the active-state nav indicator (a 1px column) into the
  // shadcn ring color so it picks up the cyan accent instead of the
  // greyscale midground.
  customCSS: `
    :root {
      --font-mondwest: "Inter", system-ui, sans-serif;
    }
    /* The default theme uses mix-blend-mode: plus-lighter on the brand
       title; against a flat dark fill that washes the cyan glow away —
       remove it so the heading reads as plain greyscale Inter. */
    aside [style*="mix-blend-mode"] {
      mix-blend-mode: normal !important;
    }
  `,
  colorOverrides: {
    // The cyan accent — every focus ring, primary button, active tab
    // indicator. Matches MC's --void-cyan (187 82% 53%).
    ring: "#22d3ee",
    primary: "#22d3ee",
    primaryForeground: "#04141a",
    accent: "#1e3a44",
    accentForeground: "#22d3ee",
    secondary: "#162028",
    secondaryForeground: "#e6ebf0",
    // Card / surface tones — pinned because the cascade would derive
    // them as a 4% midground mix, which against a flat-dark background
    // gives a barely-visible separation. MC's --card (220 30% 8%) is
    // explicitly cooler than the surrounding background.
    card: "#10141c",
    cardForeground: "#e6ebf0",
    popover: "#10141c",
    popoverForeground: "#e6ebf0",
    muted: "#161b25",
    mutedForeground: "#94a3b8",
    border: "#1c2230",
    input: "#1c2230",
    destructive: "#ef4444",
    destructiveForeground: "#ffffff",
    success: "#22c55e",
    warning: "#f59e0b",
  },
};

/**
 * Same look as ``defaultTheme`` but with a larger root font size, looser
 * line-height, and ``spacious`` density so every rem-based size in the
 * dashboard scales up. For users who find the default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  name: "default-large",
  label: "Hermes Teal (Large)",
  description: "Hermes Teal with bigger fonts and roomier spacing",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    baseSize: "18px",
    lineHeight: "1.65",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  "default-large": defaultLargeTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
  "mission-control": missionControlTheme,
};
