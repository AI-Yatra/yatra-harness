import type { MetricDef, Module, Range } from './data';

// A five-stop warm scale: terracotta, amber, straw, sage, tide. Low values sit
// at the terracotta end, so on every metric "more" reads as cooler and calmer.
const STOPS: [number, number, number, number][] = [
  [0.0, 0.79, 0.1, 38],
  [0.35, 0.86, 0.088, 68],
  [0.6, 0.9, 0.072, 104],
  [0.8, 0.87, 0.06, 162],
  [1.0, 0.85, 0.056, 222],
];

export const HATCH =
  'repeating-linear-gradient(45deg,var(--hatch-a),var(--hatch-a) 3px,var(--hatch-b) 3px,var(--hatch-b) 6px)';

export function ramp(t: number): string {
  const k = Math.max(0, Math.min(1, t));
  let a = STOPS[0];
  let b = STOPS[STOPS.length - 1];
  for (let i = 0; i < STOPS.length - 1; i++) {
    if (k >= STOPS[i][0] && k <= STOPS[i + 1][0]) {
      a = STOPS[i];
      b = STOPS[i + 1];
    }
  }
  const f = (k - a[0]) / (b[0] - a[0] || 1);
  const at = (i: number) => (a[i] + (b[i] - a[i]) * f).toFixed(3);
  return `oklch(${at(1)} ${at(2)} ${at(3)})`;
}

export function rampCSS(): string {
  return `linear-gradient(90deg,${STOPS.map((s) => `oklch(${s[1]} ${s[2]} ${s[3]})`).join(',')})`;
}

/** Position of a value on the 0..1 scale, or null when the metric does not apply. */
export function tOf(value: number | null, def: MetricDef, range: Range): number | null {
  if (value == null || !isFinite(value)) return null;
  const t = (def.transform(value) - range.lo) / (range.hi - range.lo || 1);
  return Math.max(0, Math.min(1, t));
}

export function colorOf(value: number | null, def: MetricDef, range: Range): string {
  const t = tOf(value, def, range);
  return t == null ? HATCH : ramp(t);
}

export function colorOfModule(m: Module, def: MetricDef, range: Range): string {
  return colorOf(def.get(m), def, range);
}

/** Ink dark enough to read on the fill behind it. */
export function inkOn(value: number | null, def: MetricDef, range: Range): string {
  const t = tOf(value, def, range);
  return t == null ? 'var(--ghost)' : t > 0.55 ? '#25221c' : '#fdf6ec';
}

// ── stable identity colours ────────────────────────────────────────────────
// Layers and risk classes keep the same colour everywhere on the canvas, so a
// module is recognisable in the wall, the graph and the panel at once.

export interface Swatch {
  bg: string;
  fg: string;
  solid: string;
  bd: string;
}

export const LAYER_COLOR: Record<string, Swatch> = {
  intake: { bg: 'rgba(214,228,242,0.62)', fg: '#456a8e', solid: '#7f9cb8', bd: 'rgba(120,150,180,0.34)' },
  context: { bg: 'rgba(210,231,228,0.66)', fg: '#3a6f69', solid: '#8fb2bd', bd: 'rgba(130,175,170,0.36)' },
  model: { bg: 'rgba(247,225,201,0.7)', fg: '#95651f', solid: '#d3a05a', bd: 'rgba(200,160,105,0.38)' },
  authority: { bg: 'rgba(255,216,186,0.72)', fg: '#a3521c', solid: '#e0955c', bd: 'rgba(220,155,100,0.4)' },
  execution: { bg: 'rgba(226,214,240,0.7)', fg: '#63478a', solid: '#a891cf', bd: 'rgba(160,135,195,0.36)' },
  evidence: { bg: 'rgba(212,229,214,0.7)', fg: '#3f6f52', solid: '#8fbb9c', bd: 'rgba(130,175,145,0.36)' },
  support: { bg: 'rgba(228,222,208,0.8)', fg: '#6b6459', solid: '#b7ac96', bd: 'var(--line-strong)' },
};

export function layerColor(key: string): Swatch {
  return LAYER_COLOR[key] ?? LAYER_COLOR.support;
}

export const RISK_COLOR: Record<string, Swatch> = {
  read: { bg: 'rgba(210,231,228,0.7)', fg: '#3a6f69', solid: '#8fb2bd', bd: 'rgba(130,175,170,0.4)' },
  write: { bg: 'rgba(247,225,201,0.75)', fg: '#95651f', solid: '#d3a05a', bd: 'rgba(200,160,105,0.42)' },
  execute: { bg: 'rgba(255,214,182,0.8)', fg: '#a3521c', solid: '#e0955c', bd: 'rgba(220,155,100,0.44)' },
  network: { bg: 'rgba(246,207,196,0.8)', fg: '#a8442a', solid: '#d8836a', bd: 'rgba(210,125,100,0.44)' },
  control: { bg: 'rgba(226,214,240,0.75)', fg: '#63478a', solid: '#a891cf', bd: 'rgba(160,135,195,0.4)' },
};

export function riskColor(key: string): Swatch {
  return RISK_COLOR[key] ?? LAYER_COLOR.support;
}

/** Terminal statuses read as a verdict; the rest are in-flight. */
export const STATUS_TONE: Record<string, 'good' | 'bad' | 'warn' | 'live'> = {
  CREATED: 'live',
  RUNNING: 'live',
  WAITING_APPROVAL: 'warn',
  VERIFYING: 'live',
  COMPLETED: 'good',
  FAILED: 'bad',
  BLOCKED: 'bad',
  BUDGET_EXHAUSTED: 'warn',
  CANCELLED: 'warn',
};
