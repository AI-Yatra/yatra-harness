import { HATCH, colorOfModule, inkOn, layerColor } from '../color';
import type { Module } from '../data';
import type { Store } from '../store';
import { card, eyebrow, note, section, smallNote, type Section } from '../widgets';
import { el, type Rect } from '../world';

const CELL = 118;
const GAP = 9;
const COL_PAD = 16;
const HEAD = 46;

/**
 * Every module at once. One column per layer, ordered top of the stack down,
 * so the shape of the package reads before any single module does.
 */
export function buildWall(store: Store, x: number, y: number): Section {
  const a = store.atlas;
  const columns = a.layers.filter((l) => l.modules.length > 0);
  const tallest = Math.max(...columns.map((l) => l.modules.length));
  const colW = CELL + COL_PAD * 2;
  const width = columns.length * colW + (columns.length - 1) * GAP;
  const gridH = HEAD + tallest * (CELL + GAP);

  const { root, body } = section(
    'wall',
    'Module Wall',
    'One column per layer, top of the stack on the left. Colour is the metric in the header.',
    x,
    y,
    Math.max(width + 4, 640),
  );

  const frame = card('padding:20px 22px 24px');
  frame.appendChild(eyebrow('40 modules, one grid'));

  const grid = el('div', 'wall', `width:${width}px;height:${gridH}px`);
  const cells: { node: HTMLDivElement; module: Module; value: HTMLDivElement }[] = [];

  columns.forEach((layer, ci) => {
    const cx = ci * (colW + GAP);
    const swatch = layerColor(layer.key);

    const head = el(
      'div',
      'wall-col-head no-pan',
      `left:${cx}px;top:0;width:${colW}px;color:${swatch.fg};border-bottom-color:${swatch.bd}`,
    );
    head.appendChild(el('span', 'wall-col-title', '', layer.title));
    head.appendChild(el('span', 'wall-col-n mono', '', String(layer.modules.length)));
    head.title = layer.blurb;
    head.addEventListener('click', () => store.select({ type: 'layer', key: layer.key }));
    grid.appendChild(head);

    layer.modules.forEach((name, ri) => {
      const m = store.byName.get(name);
      if (!m) return;
      const cy = HEAD + ri * (CELL + GAP);
      const node = el(
        'div',
        'wall-cell no-pan',
        `left:${cx + COL_PAD}px;top:${cy}px;width:${CELL}px;height:${CELL}px`,
      );
      node.appendChild(el('div', 'wall-name mono', '', name));
      const value = el('div', 'wall-value mono');
      node.appendChild(value);
      if (m.in_boundary) node.appendChild(el('div', 'wall-mark', '', ''));

      node.addEventListener('click', () => store.select({ type: 'module', name }));
      node.addEventListener('mouseenter', () => store.setHover(name));
      node.addEventListener('mouseleave', () => store.setHover(null));

      grid.appendChild(node);
      cells.push({ node, module: m, value });
      store.cellRect.set(name, {
        x: x + 22 + cx + COL_PAD,
        y: y + 118 + cy,
        w: CELL,
        h: CELL,
      } satisfies Rect);
    });
  });

  frame.appendChild(grid);
  frame.appendChild(
    smallNote(
      'A cell with a corner notch sits on the authority boundary. Hatching means the metric does not ' +
        'apply to that module rather than that its value is zero.',
    ),
  );
  body.appendChild(frame);

  const reading = card('padding:20px 22px');
  reading.appendChild(eyebrow('what the grid says'));
  reading.appendChild(
    note(
      'The shared floor on the right is small and heavily depended on. The authority gate is narrow and ' +
        'thick. Execution is where the size is. That distribution is the whole design argument: the code ' +
        'that decides what may happen is small enough to read, and the code that does the work sits ' +
        'underneath it with no way around.',
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
      const ink = inkOn(raw, def, range);
      node.style.color = ink;
      value.textContent = raw == null ? 'n/a' : shortValue(raw, def.key);
      value.classList.toggle('na', raw == null);
      node.title = `${module.name} - ${raw == null ? `no ${def.unit}` : def.format(raw)}`;
      const on = store.sel.type === 'module' && store.sel.name === module.name;
      node.classList.toggle('on', on);
      node.classList.toggle('dim', !!emphasis && !emphasis.has(module.name));
    }
  };
  sync();

  return {
    root,
    rect: { x, y, w: Math.max(width + 48, 640), h: gridH + 300 },
    sync,
  };
}

function shortValue(v: number, key: string): string {
  if (key === 'ratio') return `${v.toFixed(1)}x`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(Math.round(v));
}
