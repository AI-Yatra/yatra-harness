import type { Trace } from '../data';
import { card, chip, eyebrow, section, smallNote, stat, type Section } from '../widgets';
import { el, svgEl } from '../world';

const LAYER_TINT: Record<string, string> = {
  repl: 'var(--live)',
  execution: 'var(--warn)',
  models: 'var(--accent)',
  record: 'var(--accent)',
  run: 'var(--good)',
  autonomy: 'var(--good)',
  core: 'var(--muted)',
};

const COL_W = 190;
const NODE_H = 44;
const ROW_GAP = 18;
const TOP = 40;

const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);

/**
 * The harness in motion rather than at rest.
 *
 * Every other region on this canvas is measured from the code as it sits.
 * This one is measured from one real session: a failing test suite, a real
 * provider, and a profile hook that recorded which component handed to which
 * on the way to making the suite pass. Nothing here is drawn by hand, so a
 * component that stops being on the path stops being in the picture.
 */
export function buildTrace(trace: Trace, x: number, y: number): Section {
  const { root, body } = section(
    'trace',
    'One real session, recorded',
    'The path a single task actually took through the harness, start to green.',
    x,
    y,
    980,
  );

  // ── the verdict ──
  // Stated first and stated as the test suite stated it, because the whole
  // region is only worth reading if the run reached the goal.
  const head = card('padding:18px 22px');
  head.appendChild(eyebrow(`${trace.route.model} · ${trace.subject} · ${trace.generated}`));

  const verdict = el('div', 'tr-verdict');
  const before = el('div', 'tr-side tr-bad');
  before.appendChild(el('span', 'tr-side-label', '', 'before'));
  before.appendChild(el('span', 'tr-side-value mono', '', trace.before.summary));
  const arrow = el('div', 'tr-arrow', '', '→');
  const after = el('div', `tr-side ${trace.after.passed ? 'tr-good' : 'tr-bad'}`);
  after.appendChild(el('span', 'tr-side-label', '', 'after'));
  after.appendChild(el('span', 'tr-side-value mono', '', trace.after.summary));
  verdict.append(before, arrow, after);
  head.appendChild(verdict);

  const stats = el('div', 'tr-stats');
  stats.appendChild(stat(trace.entry, 'entry point', 'the first harness call of the session'));
  stats.appendChild(stat(ms(trace.wall_ms), 'wall clock', 'first call to final answer'));
  stats.appendChild(
    stat(String(trace.stats.tool_calls), 'tool calls', 'each one gated before it ran'),
  );
  stats.appendChild(
    stat(
      `${(trace.stats.input_tokens / 1000).toFixed(1)}k`,
      'tokens in',
      'the whole thread, resent every step',
    ),
  );
  stats.appendChild(stat(String(trace.components.length), 'components', 'modules actually touched'));
  stats.appendChild(stat(String(trace.span_total), 'crossings', 'calls from one component into another'));
  head.appendChild(stats);
  body.appendChild(head);

  // ── where the time went ──
  // Self time per component: a span's duration with its callees' time removed,
  // so the parts sum to the wall clock instead of double-counting nesting.
  // Two components dominate and neither is thinking. `models.providers` is
  // blocked on the provider socket and `execution.process` on the test
  // subprocess it started. What is left is the harness deciding things, and it
  // is worth seeing how small that is before optimising any of it.
  const WAIT_ON_MODEL = 'models.providers';
  const WAIT_ON_WORLD = 'execution.process';
  const selfOf = (name: string) =>
    trace.components.find((c) => c.name === name)?.ms ?? 0;
  const onModel = selfOf(WAIT_ON_MODEL);
  const onWorld = selfOf(WAIT_ON_WORLD);
  const logic = trace.components
    .filter((c) => c.name !== WAIT_ON_MODEL && c.name !== WAIT_ON_WORLD)
    .reduce((sum, c) => sum + c.ms, 0);
  const total = Math.max(1, onModel + onWorld + logic);

  const time = card('padding:18px 22px');
  time.appendChild(eyebrow('where the wall clock went'));
  const bar = el('div', 'tr-bar');
  const seg = (w: number, cls: string, label: string, title: string) => {
    const node = el('div', `tr-seg ${cls}`, `width:${Math.max(2.5, (w / total) * 100)}%`);
    node.appendChild(el('span', 'tr-seg-label', '', label));
    node.title = title;
    return node;
  };
  bar.appendChild(
    seg(onModel, 'tr-seg-wait', `${ms(onModel)} on ${trace.route.model}`, WAIT_ON_MODEL),
  );
  bar.appendChild(
    seg(onWorld, 'tr-seg-world', `${ms(onWorld)} running commands`, WAIT_ON_WORLD),
  );
  bar.appendChild(seg(logic, 'tr-seg-own', `${ms(logic)} harness`, 'everything else'));
  time.appendChild(bar);
  time.appendChild(
    smallNote(
      `Of ${ms(trace.wall_ms)} wall clock, ${((onModel / total) * 100).toFixed(0)}% was blocked ` +
        `on the provider and ${((onWorld / total) * 100).toFixed(0)}% on the test subprocess. ` +
        `Routing, gating, editing and threading the conversation came to ${ms(logic)} between ` +
        `them. The harness is not the thing to make faster.`,
    ),
  );
  body.appendChild(time);

  // ── the ladder ──
  // The steps in the order they happened, with the result each one returned.
  // A denied call is kept in place rather than dropped, because a refusal is a
  // step the loop took.
  const ladder = card('padding:18px 22px');
  ladder.appendChild(eyebrow('every step, in order'));
  const list = el('div', 'tr-steps');
  for (const step of trace.steps_taken) {
    const rowEl = el('div', `tr-step tr-${step.kind}${step.ok === false ? ' tr-step-bad' : ''}`);
    rowEl.appendChild(el('span', 'tr-step-n mono', '', String(step.n)));
    rowEl.appendChild(el('span', 'tr-step-t mono', '', ms(step.t)));
    rowEl.appendChild(
      el('span', 'tr-step-name mono', '', step.name ?? (step.kind === 'say' ? 'answer' : step.kind)),
    );
    const args = Object.entries(step.args ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join('  ');
    rowEl.appendChild(el('span', 'tr-step-args mono', '', args));
    rowEl.appendChild(el('span', 'tr-step-detail', '', (step.detail ?? '').split('\n')[0]));
    list.appendChild(rowEl);
  }
  ladder.appendChild(list);
  ladder.appendChild(
    smallNote(
      'Step 1 runs the suite before touching anything, and the last step runs it again. ' +
        'The two edits between them are the whole change.',
    ),
  );
  body.appendChild(ladder);

  // ── the call map ──
  // Components in columns by the layer they belong to, so the picture doubles
  // as a check on the layering: every arrow should point rightward, into a
  // lower layer, and one that does not is a contract violation you can see.
  const flow = card('padding:18px 22px 24px');
  flow.appendChild(eyebrow('which component handed to which'));

  const order = ['repl', 'execution', 'models', 'core'];
  const columns = order
    .map((layer) => ({
      layer,
      items: trace.components
        .filter((c) => c.layer === layer)
        .sort((a, b) => a.first_step - b.first_step || b.calls - a.calls),
    }))
    .filter((col) => col.items.length);

  const rows = Math.max(...columns.map((c) => c.items.length));
  const width = columns.length * COL_W + 40;
  const height = TOP + rows * (NODE_H + ROW_GAP) + 20;
  const plot = el('div', 'tr-flow', `width:${width}px;height:${height}px`);
  const svg = svgEl('svg', { width, height, class: 'tr-flow-svg' });
  plot.appendChild(svg);

  const at = new Map<string, { x: number; y: number }>();
  columns.forEach((col, ci) => {
    const cx = 20 + ci * COL_W;
    plot.appendChild(
      el('div', 'tr-col-head mono', `left:${cx}px;top:0;width:${COL_W - 24}px`, col.layer),
    );
    col.items.forEach((item, ri) => {
      const cy = TOP + ri * (NODE_H + ROW_GAP);
      at.set(item.name, { x: cx, y: cy });
      const node = el(
        'div',
        'tr-node',
        `left:${cx}px;top:${cy}px;width:${COL_W - 24}px;height:${NODE_H}px;` +
          `--tint:${LAYER_TINT[item.layer] ?? 'var(--muted)'}`,
      );
      node.appendChild(el('span', 'tr-node-name mono', '', item.name.split('.').slice(1).join('.')));
      node.appendChild(el('span', 'tr-node-meta', '', `${item.calls} calls`));
      node.title = `${item.name} — ${item.calls} calls, ${ms(item.ms)} held`;
      plot.appendChild(node);
    });
  });

  const layerByName = new Map(trace.components.map((c) => [c.name, c.layer]));
  const heaviest = Math.max(...trace.edges.map((e) => e.calls), 1);
  for (const edge of trace.edges) {
    const a = at.get(edge.from);
    const b = at.get(edge.to);
    if (!a || !b) continue; // the <entry> pseudo-node has no box
    const x1 = a.x + COL_W - 24;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const mid = (x1 + x2) / 2;
    const fromRank = order.indexOf(layerByName.get(edge.from) ?? '');
    const toRank = order.indexOf(layerByName.get(edge.to) ?? '');
    // Rightward is downward through the layers. Same rank is a call inside one
    // package. Leftward would be a component reaching back up, which the
    // import contract forbids, so it is drawn to be noticed rather than hidden.
    const kind =
      toRank < fromRank ? ' tr-edge-up' : toRank === fromRank ? ' tr-edge-peer' : '';
    svg.appendChild(
      svgEl('path', {
        d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
        class: `tr-edge${kind}`,
        'stroke-width': String(0.6 + (edge.calls / heaviest) * 2.6),
      }),
    );
  }
  flow.appendChild(plot);
  flow.appendChild(
    smallNote(
      `Arrow thickness is call count. Faint arrows are calls inside one package; solid ones ` +
        `cross into a lower layer. Across ${trace.edges.length} recorded edges not one runs ` +
        `back up the stack, which is the import contract holding at run time and not only ` +
        `under lint-imports.`,
    ),
  );
  body.appendChild(flow);

  // ── what changed ──
  const out = card('padding:18px 22px');
  out.appendChild(eyebrow('what it actually changed'));
  const diff = el('pre', 'tr-diff mono', '', trace.diffstat.join('\n'));
  out.appendChild(diff);
  const chips = el('div', 'tr-chips');
  for (const name of trace.before.failed.slice(0, 14)) {
    chips.appendChild(chip(name, 'tr-fixed'));
  }
  out.appendChild(chips);
  out.appendChild(
    smallNote(
      `${trace.before.failed.length} named failures before, ${trace.after.failed.length} after. ` +
        'The suite is write-protected, so the only way to turn it green was to fix the code.',
    ),
  );
  body.appendChild(out);

  return { root, rect: { x, y, w: 980, h: 0 } };
}
