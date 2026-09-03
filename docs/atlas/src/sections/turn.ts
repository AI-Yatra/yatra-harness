import type { Store } from '../store';
import { card, eyebrow, section, smallNote, type Section } from '../widgets';
import { el, svgEl } from '../world';

const LANE_W = 128;
const HEAD_H = 52;
const STEP_H = 46;
const PAD = 16;

const SIDE_TINT: Record<string, string> = {
  human: 'var(--live)',
  harness: 'var(--good)',
  model: 'var(--accent)',
  world: 'var(--warn)',
};

/**
 * One turn as a sequence over actor lanes. Each arrow carries the module that
 * performs it and the ledger event it writes; a dashed arrow is a step only
 * the batch loop takes, which is how the REPL's missing verifier and ledger
 * become visible rather than asserted.
 */
export function buildTurn(store: Store, x: number, y: number): Section {
  const lanes = store.atlas.lanes;
  const steps = store.atlas.steps;
  const width = PAD * 2 + lanes.length * LANE_W;
  const height = HEAD_H + steps.length * STEP_H + 40;

  const { root, body } = section(
    'turn',
    'One Turn',
    'Who does what, in order, and what each step writes down.',
    x,
    y,
    Math.max(width + 48, 700),
  );

  const frame = card('padding:20px 22px 22px');
  frame.appendChild(eyebrow('solid = both loops · dashed = batch only'));

  const plot = el('div', 'seq', `width:${width}px;height:${height}px`);
  const svg = svgEl('svg', { width, height, class: 'seq-svg' });
  plot.appendChild(svg);

  const centre = new Map<string, number>();
  lanes.forEach((lane, index) => {
    const cx = PAD + index * LANE_W + LANE_W / 2;
    centre.set(lane.key, cx);
    const head = el(
      'div',
      `seq-lane seq-${lane.side}`,
      `left:${PAD + index * LANE_W}px;top:0;width:${LANE_W}px;--tint:${SIDE_TINT[lane.side]}`,
    );
    head.appendChild(el('span', 'seq-lane-name mono', '', lane.name));
    head.appendChild(el('span', 'seq-lane-side', '', lane.side));
    plot.appendChild(head);
    // The lifeline.
    svg.appendChild(
      svgEl('line', {
        x1: cx, y1: HEAD_H - 6, x2: cx, y2: height - 24, class: 'seq-life',
      }),
    );
  });

  const arrows: { path: SVGPathElement; module: string }[] = [];

  steps.forEach((step, index) => {
    const top = HEAD_H + index * STEP_H + 18;
    const from = centre.get(step.at);
    const to = centre.get(step.to);
    if (from == null || to == null) return;
    const batchOnly = !step.loops.includes('repl');
    const back = to < from;

    const line = svgEl('path', {
      d: `M${from},${top} L${to},${top}`,
      class: `seq-arrow${batchOnly ? ' seq-batch' : ''}${back ? ' seq-back' : ''}`,
      'marker-end': 'url(#seq-head)',
    });
    svg.appendChild(line);
    arrows.push({ path: line, module: step.module });

    const midpoint = (from + to) / 2;
    const label = el(
      'div',
      `seq-label${batchOnly ? ' batch-only' : ''}`,
      `left:${midpoint - 120}px;top:${top - 30}px;width:240px`,
    );
    label.appendChild(el('span', 'seq-n mono', '', String(step.n)));
    label.appendChild(el('span', 'seq-text', '', step.label));
    plot.appendChild(label);

    const meta = el('div', 'seq-meta mono', `left:${PAD}px;top:${top - 9}px`);
    const modChip = el('span', 'seq-mod no-pan', '', step.module);
    modChip.addEventListener('click', () => store.select({ type: 'module', name: step.module }));
    meta.appendChild(modChip);
    if (step.emits) {
      const ev = el('span', 'seq-emit no-pan', '', step.emits);
      ev.addEventListener('click', () => store.select({ type: 'event', name: step.emits }));
      meta.appendChild(ev);
    }
    plot.appendChild(meta);
  });

  const defs = svgEl('defs');
  const marker = svgEl('marker', {
    id: 'seq-head', viewBox: '0 0 8 8', refX: 7, refY: 4,
    markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse',
  });
  marker.appendChild(svgEl('path', { d: 'M0,0 L8,4 L0,8 z', class: 'seq-head' }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  frame.appendChild(plot);
  frame.appendChild(
    smallNote(
      'Steps 3 to 10 repeat until the model stops asking for tools. The batch loop then hands to the ' +
        'verifier; the REPL simply answers, which is why steps 2, 10, 11 and 12 are dashed.',
    ),
  );
  body.appendChild(frame);

  const sync = () => {
    const focus = store.hover ?? (store.sel.type === 'module' ? store.sel.name : null);
    svg.classList.toggle('focused', !!focus);
    for (const arrow of arrows) {
      arrow.path.classList.toggle('lit', arrow.module === focus);
    }
  };
  sync();

  return { root, rect: { x, y, w: Math.max(width + 48, 700), h: height + 200 }, sync };
}
