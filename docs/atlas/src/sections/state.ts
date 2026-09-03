import { STATUS_TONE } from '../color';
import type { Store } from '../store';
import { card, eyebrow, section, smallNote, type Section } from '../widgets';
import { el, svgEl } from '../world';

const COL_W = 260;
const NODE_W = 172;
const NODE_H = 44;
const ROW_GAP = 56;  // room to label a move within a column
const PAD = 150;  // room for the side-routed edges and their labels

/**
 * The run state machine. Nodes are the statuses in the contract, edges are
 * labelled with what causes the move. The five terminal states sit in the
 * last column, so a run's possible endings are countable at a glance.
 */
export function buildState(store: Store, x: number, y: number): Section {
  const columns = store.atlas.state_columns;
  const transitions = store.atlas.transitions;
  const terminal = new Set(
    store.atlas.events.filter((e) => e.terminal).map((e) => e.type.replace(/^RUN_/, '')),
  );

  const tallest = Math.max(...columns.map((c) => c.length));
  const width = PAD * 2 + columns.length * COL_W;
  const height = PAD * 2 + tallest * (NODE_H + ROW_GAP);

  const { root, body } = section(
    'state',
    'How a run ends',
    'Nine statuses, five of them terminal. Only the verifier may produce COMPLETED.',
    x,
    y,
    Math.max(width + 48, 640),
  );

  const frame = card('padding:20px 22px 22px');
  frame.appendChild(eyebrow(`${store.atlas.statuses.length} statuses · ${transitions.length} transitions`));

  const plot = el('div', 'fsm', `width:${width}px;height:${height}px`);
  const svg = svgEl('svg', { width, height, class: 'fsm-svg' });
  plot.appendChild(svg);

  const at = new Map<string, { cx: number; cy: number; left: number; top: number }>();
  columns.forEach((column, ci) => {
    const columnHeight = column.length * (NODE_H + ROW_GAP) - ROW_GAP;
    const startY = PAD + (height - PAD * 2 - columnHeight) / 2;
    column.forEach((status, ri) => {
      const left = PAD + ci * COL_W + (COL_W - NODE_W) / 2;
      const top = startY + ri * (NODE_H + ROW_GAP);
      at.set(status, { cx: left + NODE_W / 2, cy: top + NODE_H / 2, left, top });
    });
  });

  const defs = svgEl('defs');
  const marker = svgEl('marker', {
    id: 'fsm-head', viewBox: '0 0 8 8', refX: 7, refY: 4,
    markerWidth: 5.5, markerHeight: 5.5, orient: 'auto-start-reverse',
  });
  marker.appendChild(svgEl('path', { d: 'M0,0 L8,4 L0,8 z', class: 'fsm-head' }));
  defs.appendChild(marker);
  svg.appendChild(defs);

  // Labels sit in the empty channel between two columns, never over a node.
  // Several edges share a channel, so each one is nudged onto its own line.
  const used = new Map<string, number>();

  for (const move of transitions) {
    const from = at.get(move.from);
    const to = at.get(move.to);
    if (!from || !to) continue;
    const sameColumn = Math.abs(to.cx - from.cx) < 1;
    const backwards = to.cx < from.cx;

    let path: SVGPathElement;
    let labelX: number;
    let labelY: number;

    if (sameColumn) {
      // A move within a column runs straight down the gap between its two
      // nodes, and is labelled there. Routing it around the outside put the
      // label on top of whatever sat in the neighbouring column.
      const downwards = to.cy > from.cy;
      const y1 = downwards ? from.top + NODE_H : from.top;
      const y2 = downwards ? to.top : to.top + NODE_H;
      // Two edges share this gap in opposite directions, so they are offset
      // to either side of the column's centre line.
      const lane = from.left + NODE_W * (downwards ? 0.34 : 0.66);
      path = svgEl('path', {
        d: `M${lane},${y1} L${lane},${y2}`,
        class: 'fsm-edge fsm-side',
        'marker-end': 'url(#fsm-head)',
      });
      labelX = from.left + (downwards ? -78 : NODE_W - 46);
      labelY = (y1 + y2) / 2 - 8;
    } else {
      const x1 = backwards ? from.left : from.left + NODE_W;
      const x2 = backwards ? to.left + NODE_W : to.left;
      const channel = (x1 + x2) / 2;
      const drop = backwards ? 52 : 0;
      path = svgEl('path', {
        d: `M${x1},${from.cy} Q${channel},${(from.cy + to.cy) / 2 + drop} ${x2},${to.cy}`,
        class: `fsm-edge${backwards ? ' fsm-retry' : ''}`,
        'marker-end': 'url(#fsm-head)',
      });
      // Anchored just before the target rather than at the channel midpoint.
      // Five terminal states share one channel, so midpoints all land on top
      // of each other; each target's own approach is uncontested.
      const taken = used.get(move.to) ?? 0;
      used.set(move.to, taken + 1);
      labelX = backwards ? x2 + 6 : x2 - 152;
      labelY = to.cy - 27 + taken * 15;
    }
    svg.appendChild(path);
    plot.appendChild(
      el(
        'div',
        `fsm-label${backwards || sameColumn ? ' back' : ''}`,
        `left:${labelX}px;top:${labelY}px;width:${sameColumn ? 124 : 146}px;` +
          `text-align:${sameColumn || backwards ? 'center' : 'right'}`,
        move.on,
      ),
    );
  }

  const nodes: { node: HTMLDivElement; status: string }[] = [];
  for (const status of store.atlas.statuses) {
    const spot = at.get(status);
    if (!spot) continue;
    const tone = STATUS_TONE[status] ?? 'live';
    const isEnd = terminal.has(status);
    const node = el(
      'div',
      `fsm-node tone-${tone}${isEnd ? ' fsm-terminal no-pan' : ''}`,
      `left:${spot.left}px;top:${spot.top}px;width:${NODE_W}px;height:${NODE_H}px`,
    );
    node.appendChild(el('span', 'fsm-name mono', '', status));
    if (isEnd) {
      node.appendChild(el('span', 'fsm-ev mono', '', `RUN_${status}`));
      node.addEventListener('click', () =>
        store.select({ type: 'event', name: `RUN_${status}` }),
      );
    }
    plot.appendChild(node);
    nodes.push({ node, status });
  }

  frame.appendChild(plot);
  frame.appendChild(
    smallNote(
      'The edge back from VERIFYING to RUNNING is the retry loop: a failed verification is an ' +
        'observation, not an ending. A finish claim that does not verify never becomes COMPLETED.',
    ),
  );
  body.appendChild(frame);

  const sync = () => {
    const chosen =
      store.sel.type === 'event' ? store.sel.name.replace(/^RUN_/, '') : null;
    for (const { node, status } of nodes) node.classList.toggle('on', status === chosen);
  };
  sync();

  return { root, rect: { x, y, w: Math.max(width + 48, 640), h: height + 200 }, sync };
}
