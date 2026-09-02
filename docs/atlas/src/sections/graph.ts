import { layerColor } from '../color';
import type { Module } from '../data';
import type { Store } from '../store';
import { card, eyebrow, note, section, smallNote, stat, type Section } from '../widgets';
import { el, svgEl } from '../world';

const NODE_W = 108;
const NODE_H = 34;
const NODE_GAP = 12;
const BAND_GAP = 96;
const PAD = 30;

interface Placed {
  m: Module;
  x: number;
  y: number;
  band: number;
}

/**
 * The real import graph. Layers are bands in stack order, so an edge that runs
 * downward is a module leaning on the layer beneath it and an edge that runs
 * upward is the design being leaned on backwards. Both are drawn.
 */
export function buildGraph(store: Store, x: number, y: number): Section {
  const a = store.atlas;
  const bands = a.layers.filter((l) => l.modules.length > 0);

  // Widest band decides the drawing width; every band is centred in it.
  const perRow = Math.max(...bands.map((l) => l.modules.length));
  const innerW = perRow * NODE_W + (perRow - 1) * NODE_GAP;
  const innerH = bands.length * NODE_H + (bands.length - 1) * BAND_GAP;
  const W = innerW + PAD * 2;
  const H = innerH + PAD * 2;

  const placed = new Map<string, Placed>();
  bands.forEach((layer, bi) => {
    // Most depended-on first, so the load-bearing modules line up centre-left.
    const ordered = [...layer.modules]
      .map((n) => store.byName.get(n))
      .filter((m): m is Module => !!m)
      .sort((p, q) => q.fan_in - p.fan_in || p.name.localeCompare(q.name));
    const rowW = ordered.length * NODE_W + (ordered.length - 1) * NODE_GAP;
    const startX = PAD + (innerW - rowW) / 2;
    const rowY = PAD + bi * (NODE_H + BAND_GAP);
    ordered.forEach((m, i) => {
      placed.set(m.name, { m, x: startX + i * (NODE_W + NODE_GAP), y: rowY, band: bi });
    });
  });

  const { root, body } = section(
    'graph',
    'What imports what',
    `All ${a.totals.edges} import edges inside the package, drawn where they actually run.`,
    x,
    y,
    W + 48,
  );

  const frame = card('padding:20px 22px 24px');
  frame.appendChild(eyebrow('layers as bands, in stack order'));

  const plot = el('div', 'graph', `width:${W}px;height:${H}px`);
  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: 'graph-svg' });
  plot.appendChild(svg);

  let down = 0;
  let up = 0;
  let sideways = 0;
  const edges: { path: SVGPathElement; from: string; to: string }[] = [];

  for (const from of a.modules) {
    const src = placed.get(from.name);
    if (!src) continue;
    for (const toName of from.imports) {
      const dst = placed.get(toName);
      if (!dst) continue;
      const dir = dst.band > src.band ? 'down' : dst.band < src.band ? 'up' : 'same';
      if (dir === 'down') down++;
      else if (dir === 'up') up++;
      else sideways++;

      const x1 = src.x + NODE_W / 2;
      const x2 = dst.x + NODE_W / 2;
      let d: string;
      if (dir === 'same') {
        // A sibling edge arcs over the band rather than running through it.
        const y1 = src.y;
        const lift = Math.min(40, 14 + Math.abs(x2 - x1) * 0.18);
        d = `M${x1},${y1} C${x1},${y1 - lift} ${x2},${y1 - lift} ${x2},${y1}`;
      } else {
        const y1 = dir === 'down' ? src.y + NODE_H : src.y;
        const y2 = dir === 'down' ? dst.y : dst.y + NODE_H;
        const mid = (y1 + y2) / 2;
        d = `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;
      }
      const path = svgEl('path', { d, class: `edge edge-${dir}` });
      svg.appendChild(path);
      edges.push({ path, from: from.name, to: toName });
    }
  }

  // Band labels behind the nodes.
  bands.forEach((layer, bi) => {
    const swatch = layerColor(layer.key);
    const rowY = PAD + bi * (NODE_H + BAND_GAP);
    const label = el(
      'div',
      'graph-band no-pan',
      `left:0;top:${rowY - 20}px;width:${W}px;color:${swatch.fg}`,
    );
    label.appendChild(el('span', 'graph-band-name mono', '', layer.title.toLowerCase()));
    label.title = layer.blurb;
    label.addEventListener('click', () => store.select({ type: 'layer', key: layer.key }));
    plot.appendChild(label);
  });

  const nodes: { node: HTMLDivElement; m: Module }[] = [];
  for (const p of placed.values()) {
    const swatch = layerColor(p.m.layer);
    const node = el(
      'div',
      'graph-node no-pan',
      `left:${p.x}px;top:${p.y}px;width:${NODE_W}px;height:${NODE_H}px;` +
        `background:${swatch.bg};color:${swatch.fg};border-color:${swatch.bd}`,
    );
    node.appendChild(el('span', 'graph-node-name mono', '', p.m.name));
    node.appendChild(el('span', 'graph-node-n mono', '', `${p.m.fan_in}`));
    node.title = `${p.m.name} - imported by ${p.m.fan_in}, imports ${p.m.fan_out}`;
    node.addEventListener('click', () => store.select({ type: 'module', name: p.m.name }));
    node.addEventListener('mouseenter', () => store.setHover(p.m.name));
    node.addEventListener('mouseleave', () => store.setHover(null));
    plot.appendChild(node);
    nodes.push({ node, m: p.m });
  }

  frame.appendChild(plot);
  frame.appendChild(
    smallNote(
      'The number on a node is how many siblings import it. Hover or select a module to keep only its ' +
        'own edges lit.',
    ),
  );
  body.appendChild(frame);

  const reading = card('padding:20px 22px');
  reading.appendChild(eyebrow('the shape of the dependencies'));
  const grid = el('div', 'stat-grid');
  grid.appendChild(stat(String(down), 'downward', 'A module leaning on the layer beneath it'));
  grid.appendChild(stat(String(up), 'upward', 'A module reaching back up the stack'));
  grid.appendChild(stat(String(sideways), 'within a layer', 'Siblings in the same band'));
  const hubs = [...a.modules].sort((p, q) => q.fan_in - p.fan_in).slice(0, 3);
  grid.appendChild(stat(hubs[0] ? String(hubs[0].fan_in) : '0', 'widest fan-in', hubs[0]?.name ?? ''));
  reading.appendChild(grid);
  reading.appendChild(
    note(
      `The most depended-on modules are ${hubs.map((h) => `${h.name} (${h.fan_in})`).join(', ')}. ` +
        'Those are the ones that cannot change quietly.',
    ),
  );
  body.appendChild(reading);

  const sync = () => {
    const focus = store.hover ?? (store.sel.type === 'module' ? store.sel.name : null);
    const emphasis = store.emphasis;
    svg.classList.toggle('focused', !!focus);
    for (const e of edges) {
      const lit = !!focus && (e.from === focus || e.to === focus);
      e.path.classList.toggle('lit', lit);
    }
    for (const { node, m } of nodes) {
      node.classList.toggle('on', store.sel.type === 'module' && store.sel.name === m.name);
      node.classList.toggle('dim', !!emphasis && !emphasis.has(m.name));
    }
  };
  sync();

  return { root, rect: { x, y, w: W + 48, h: H + 340 }, sync };
}
