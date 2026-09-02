// The shape of what scripts/scan_harness.py writes. Nothing here is typed
// more loosely than the scanner emits it, so a scanner change that breaks the
// canvas fails `npm run check` rather than at runtime.

export interface ApiEntry {
  kind: 'class' | 'function';
  name: string;
  line: number;
  doc: string;
  methods: string[];
}

export interface Module {
  name: string;
  path: string;
  layer: string;
  side: 'harness' | 'model';
  doc: string;
  doc_full: string;
  lines: number;
  sloc: number;
  imports: string[];
  imported_by: string[];
  fan_out: number;
  fan_in: number;
  external: string[];
  api: ApiEntry[];
  classes: number;
  functions: number;
  api_count: number;
  tests: string[];
  test_sloc: number;
  test_cases: number;
  commits: number;
  last_touched: string;
  in_boundary: boolean;
}

export interface Layer {
  key: string;
  title: string;
  blurb: string;
  side: 'harness' | 'model';
  modules: string[];
}

export interface BoundaryStage {
  stage: string;
  module: string;
  note: string;
  present: boolean;
}

export interface Tool {
  name: string;
  description: string;
  risk: string;
  line: number;
  arguments: string[];
  required: string[];
}

export interface Command {
  name: string;
  help: string;
  group: string;
  line: number;
}

export interface EventType {
  type: string;
  writers: string[];
  terminal: boolean;
}

export interface BudgetField {
  name: string;
  type: string;
  default: string;
}

export interface TestFile {
  lines: number;
  sloc: number;
  covers: string[];
  cases: number;
}

export interface Totals {
  modules: number;
  lines: number;
  sloc: number;
  api: number;
  edges: number;
  tools: number;
  commands: number;
  test_files: number;
  test_cases: number;
  test_sloc: number;
  commits: number;
}

export interface Cell {
  modules: string[];
  missing: string[];
  sloc: number;
  tests: number;
  api: number;
}

export interface Primitive {
  key: string;
  name: string;
  asks: string;
  batch: Cell;
  repl: Cell;
  sloc: number;
}

export interface Lane {
  key: string;
  name: string;
  side: 'human' | 'harness' | 'model' | 'world';
}

export interface Step {
  n: number;
  at: string;
  to: string;
  label: string;
  module: string;
  emits: string;
  loops: string[];
  present: boolean;
}

export interface Gate {
  gate: string;
  module: string;
  loop: string;
  rule: string;
  verdict: string;
  final: boolean;
  present: boolean;
}

export interface Transition {
  from: string;
  to: string;
  on: string;
}

export interface Loop {
  key: string;
  name: string;
  entry: string;
  shape: string;
  ends: string;
  workspace: string;
  root: string;
  present: boolean;
}

export interface Atlas {
  generated_by: string;
  repo: string;
  head: { sha?: string; date?: string; subject?: string };
  totals: Totals;
  layers: Layer[];
  modules: Module[];
  boundary: BoundaryStage[];
  tools: Tool[];
  commands: Command[];
  events: EventType[];
  statuses: string[];
  actions: string[];
  risks: string[];
  budgets: BudgetField[];
  tests: Record<string, TestFile>;
  primitives: Primitive[];
  lanes: Lane[];
  steps: Step[];
  gates: Gate[];
  transitions: Transition[];
  state_columns: string[][];
  loops: Loop[];
  shared: string[];
}

export async function loadAtlas(): Promise<Atlas> {
  const res = await fetch(`${import.meta.env.BASE_URL}atlas.json`);
  if (!res.ok) throw new Error(`atlas.json ${res.status}: run scripts/scan_harness.py`);
  return (await res.json()) as Atlas;
}

// ── metrics ────────────────────────────────────────────────────────────────
// Every metric is a real count off the repository. `get` returns null when the
// number does not apply to a module, and the canvas hatches it rather than
// quietly drawing a zero.

export interface MetricDef {
  key: string;
  label: string;
  unit: string;
  blurb: string;
  get(m: Module): number | null;
  format(v: number): string;
  /** Compress long tails so one outlier does not flatten the rest. */
  transform(v: number): number;
}

const log1p = (v: number) => Math.log(1 + Math.max(0, v));
const plain = (v: number) => v;

export const METRICS: MetricDef[] = [
  {
    key: 'sloc',
    label: 'size',
    unit: 'sloc',
    blurb: 'Lines that are neither blank nor a whole-line comment.',
    get: (m) => m.sloc,
    format: (v) => `${v.toLocaleString()} sloc`,
    transform: log1p,
  },
  {
    key: 'fan_in',
    label: 'depended on',
    unit: 'importers',
    blurb: 'How many sibling modules import this one. High means load-bearing.',
    get: (m) => m.fan_in,
    format: (v) => `${v} importer${v === 1 ? '' : 's'}`,
    transform: plain,
  },
  {
    key: 'fan_out',
    label: 'depends on',
    unit: 'imports',
    blurb: 'How many sibling modules this one imports. High means entangled.',
    get: (m) => m.fan_out,
    format: (v) => `${v} import${v === 1 ? '' : 's'}`,
    transform: plain,
  },
  {
    key: 'api_count',
    label: 'surface',
    unit: 'public symbols',
    blurb: 'Public top-level classes and functions. The part other modules may touch.',
    get: (m) => m.api_count,
    format: (v) => `${v} public symbol${v === 1 ? '' : 's'}`,
    transform: plain,
  },
  {
    key: 'test_cases',
    label: 'tests',
    unit: 'cases',
    blurb: 'Test functions in files that import this module. Zero is drawn as a gap, not a zero.',
    get: (m) => (m.test_cases > 0 ? m.test_cases : null),
    format: (v) => `${v} test case${v === 1 ? '' : 's'}`,
    transform: log1p,
  },
  {
    key: 'ratio',
    label: 'test weight',
    unit: 'test sloc / sloc',
    blurb: 'Test lines per line of module. Not applicable where no test imports the module.',
    get: (m) => (m.test_sloc > 0 ? m.test_sloc / Math.max(1, m.sloc) : null),
    format: (v) => `${v.toFixed(2)}x its own size in tests`,
    transform: plain,
  },
  {
    key: 'commits',
    label: 'churn',
    unit: 'commits',
    blurb: 'Commits that touched the file, followed through renames.',
    get: (m) => m.commits,
    format: (v) => `${v} commit${v === 1 ? '' : 's'}`,
    transform: plain,
  },
];

export function metricByKey(key: string): MetricDef {
  return METRICS.find((m) => m.key === key) ?? METRICS[0];
}

export interface Range {
  lo: number;
  hi: number;
}

/** The transformed range of a metric across the modules actually present. */
export function rangeOf(def: MetricDef, modules: Module[]): Range {
  const values = modules
    .map((m) => def.get(m))
    .filter((v): v is number => v != null && isFinite(v))
    .map(def.transform);
  if (!values.length) return { lo: 0, hi: 1 };
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  return hi === lo ? { lo, hi: lo + 1 } : { lo, hi };
}

// ── the recorded session ───────────────────────────────────────────────────
// scripts/trace_session.py writes this by running one real conversation under
// a profile hook. It is optional: the atlas describes the harness at rest
// without it, and gains the region that shows the harness in motion with it.

export interface TraceStep {
  n: number;
  kind: 'tool' | 'denied' | 'say';
  name?: string;
  component: string;
  args?: Record<string, string>;
  detail?: string;
  ok?: boolean;
  t: number;
  ms?: number;
}

export interface TraceComponent {
  name: string;
  layer: string;
  calls: number;
  /** Time in this component's own frames, with time in its callees removed. */
  ms: number;
  /** Time between entering and leaving it, callees included. */
  held_ms: number;
  first_step: number;
}

export interface TraceEdge {
  from: string;
  to: string;
  calls: number;
}

export interface TraceSpan {
  id: number;
  parent: number;
  component: string;
  layer: string;
  func: string;
  t0: number;
  ms: number;
  step: number;
}

export interface TraceVerdict {
  exit_code: number;
  summary: string;
  failed: string[];
  passed: boolean;
}

export interface Trace {
  generated: string;
  task: string;
  subject: string;
  route: { name: string; model: string; base_url: string; stream: boolean };
  entry: string;
  wall_ms: number;
  ok: boolean;
  error: string;
  stats: {
    steps: number;
    tool_calls: number;
    input_tokens: number;
    output_tokens: number;
    errors: number;
  };
  before: TraceVerdict;
  after: TraceVerdict;
  diffstat: string[];
  steps_taken: TraceStep[];
  components: TraceComponent[];
  edges: TraceEdge[];
  spans: TraceSpan[];
  span_total: number;
}

/** null when no session has been recorded yet; the atlas works without one. */
export async function loadTrace(): Promise<Trace | null> {
  try {
    const res = await fetch(`${import.meta.env.BASE_URL}trace.json`);
    if (!res.ok) return null;
    return (await res.json()) as Trace;
  } catch {
    return null;
  }
}
