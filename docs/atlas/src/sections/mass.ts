import { HATCH, colorOfModule, inkOn, layerColor } from '../color';
import type { Module } from '../data';
import type { Store } from '../store';
import { card, eyebrow, note, section, smallNote, type Section } from '../widgets';
import { el } from '../world';

const W = 880;
const H = 620;

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * Where the code actually is. Area is line count, so the treemap answers a
 * question the wall's equal cells cannot: which layer is most of the package.
 */
export function buildMass(store: Store, x: number, y: number): Section {
  const a = store.atlas;
  const { root, body } = section(
    'mass',
    'Where the lines are',
    'Area is line count. Layers first, then the modules inside them.',
    x,
    y,
    W + 48,
  );

  const frame = card('padding:20px 22px 24px');
  frame.appendChild(eyebrow(`${a.totals.sloc.toLocaleString()} lines, laid out by area`));

  const plot = el('div', 'mass', `width:${W}px;height:${H}px`);

  const layers = a.layers
    .map((l) => ({
      layer: l,
      modules: l.modules
        .map((n) => store.byName.get(n))
        .filter((m): m is Module => !!m)
        .sort((p, q) => q.sloc - p.sloc),
    }))
    .filter((l) => l.modules.length > 0)
    .map((l) => ({ ...l, sloc: l.modules.reduce((s, m) => s + m.sloc, 0) }))
    .sort((p, q) => q.sloc - p.sloc);

  const cells: { node: HTMLDivElement; module: Module; value: HTMLElement }[] = [];
  const outerBoxes = squarify(
    layers.map((l) => l.sloc),
    { x: 0, y: 0, w: W, h: H },
  );

  layers.forEach((group, li) => {
    const box = outerBoxes[li];
    const swatch = layerColor(group.layer.key);
    const shell = el(
      'div',
      'mass-group no-pan',
      `left:${box.x}px;top:${box.y}px;width:${box.w}px;height:${box.h}px;border-color:${swatch.bd}`,
    );
    const label = el('div', 'mass-group-label mono', `color:${swatch.fg}`);
    label.appendChild(el('span', '', '', group.layer.title.toLowerCase()));
    label.appendChild(el('span', 'mass-group-n', '', `${group.sloc.toLocaleString()}`));
    label.title = group.layer.blurb;
    label.addEventListener('click', () => store.select({ type: 'layer', key: group.layer.key }));
    shell.appendChild(label);

    const inner: Box = { x: 2, y: 20, w: box.w - 4, h: box.h - 22 };
    const boxes = squarify(
      group.modules.map((m) => m.sloc),
      inner,
    );
    group.modules.forEach((m, i) => {
      const b = boxes[i];
      const node = el(
        'div',
        'mass-cell',
        `left:${b.x}px;top:${b.y}px;width:${Math.max(0, b.w - 2)}px;height:${Math.max(0, b.h - 2)}px`,
      );
      const name = el('span', 'mass-name mono', '', m.name);
      const value = el('span', 'mass-value mono', '', String(m.sloc));
      node.appendChild(name);
      node.appendChild(value);
      // Text is hidden rather than clipped where the box is too small for it.
      node.dataset.room = b.w > 74 && b.h > 30 ? 'yes' : b.w > 42 && b.h > 18 ? 'name' : 'no';
      node.addEventListener('click', () => store.select({ type: 'module', name: m.name }));
      node.addEventListener('mouseenter', () => store.setHover(m.name));
      node.addEventListener('mouseleave', () => store.setHover(null));
      shell.appendChild(node);
      cells.push({ node, module: m, value });
    });

    plot.appendChild(shell);
  });

  frame.appendChild(plot);
  frame.appendChild(
    smallNote(
      'Colour follows the metric in the header, so switching to fan-in shows a large module that almost ' +
        'nothing depends on, and a small one that everything does.',
    ),
  );
  body.appendChild(frame);

  const biggest = [...a.modules].sort((p, q) => q.sloc - p.sloc).slice(0, 3);
  const reading = card('padding:20px 22px');
  reading.appendChild(eyebrow('the reading'));
  reading.appendChild(
    note(
      `${biggest.map((m) => `${m.name} (${m.sloc})`).join(', ')} are the three largest modules and ` +
        `together they are ${Math.round((biggest.reduce((s, m) => s + m.sloc, 0) / a.totals.sloc) * 100)}% ` +
        'of the package. The tool registry, the operator CLI and the loop itself: the three places where ' +
        'the abstract design has to meet something concrete.',
    ),
  );
  body.appendChild(reading);

  const sync = () => {
    const def = store.metric;
    const range = store.range;
    const emphasis = store.emphasis;
    for (const { node, module, value } of cells) {
      const raw = def.get(module);
      const fill = colorOfModule(module, def, range);
      node.style.background = fill;
      node.style.backgroundImage = fill === HATCH ? HATCH : 'none';
      node.style.color = inkOn(raw, def, range);
      value.textContent = raw == null ? 'n/a' : def.key === 'ratio' ? raw.toFixed(1) : String(Math.round(raw));
      node.title = `${module.name} - ${module.sloc} sloc, ${raw == null ? `no ${def.unit}` : def.format(raw)}`;
      node.classList.toggle('on', store.sel.type === 'module' && store.sel.name === module.name);
      node.classList.toggle('dim', !!emphasis && !emphasis.has(module.name));
    }
  };
  sync();

  return { root, rect: { x, y, w: W + 48, h: H + 330 }, sync };
}

/**
 * Squarified treemap. Values are laid into the box in descending order,
 * keeping each row's worst aspect ratio as close to square as it can.
 */
function squarify(values: number[], box: Box): Box[] {
  const total = values.reduce((s, v) => s + v, 0) || 1;
  const area = box.w * box.h;
  const scaled = values.map((v) => (v / total) * area);
  const out: Box[] = new Array(values.length);
  let rest: Box = { ...box };
  let i = 0;

  while (i < scaled.length) {
    const horizontal = rest.w >= rest.h;
    const side = horizontal ? rest.h : rest.w;
    let row: number[] = [];
    let rowIdx: number[] = [];
    let best = Infinity;

    // Grow the row while the worst aspect ratio in it keeps improving.
    for (let j = i; j < scaled.length; j++) {
      const next = [...row, scaled[j]];
      const ratio = worst(next, side);
      if (row.length && ratio > best) break;
      row = next;
      rowIdx = [...rowIdx, j];
      best = ratio;
    }

    const rowSum = row.reduce((s, v) => s + v, 0);
    const thickness = side > 0 ? rowSum / side : 0;
    let offset = 0;
    for (let k = 0; k < row.length; k++) {
      const length = rowSum > 0 ? (row[k] / rowSum) * side : 0;
      out[rowIdx[k]] = horizontal
        ? { x: rest.x, y: rest.y + offset, w: thickness, h: length }
        : { x: rest.x + offset, y: rest.y, w: length, h: thickness };
      offset += length;
    }

    rest = horizontal
      ? { x: rest.x + thickness, y: rest.y, w: Math.max(0, rest.w - thickness), h: rest.h }
      : { x: rest.x, y: rest.y + thickness, w: rest.w, h: Math.max(0, rest.h - thickness) };
    i += row.length;
    if (!row.length) break;
  }

  return out.map((b) => b ?? { x: box.x, y: box.y, w: 0, h: 0 });
}

function worst(row: number[], side: number): number {
  const sum = row.reduce((s, v) => s + v, 0);
  if (sum <= 0 || side <= 0) return Infinity;
  const max = Math.max(...row);
  const min = Math.min(...row);
  const s2 = sum * sum;
  const side2 = side * side;
  return Math.max((side2 * max) / s2, s2 / (side2 * min));
}
