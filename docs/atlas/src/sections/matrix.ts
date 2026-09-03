import { layerColor } from '../color';
import type { Cell, Primitive } from '../data';
import type { Store } from '../store';
import { card, eyebrow, section, smallNote, type Section } from '../widgets';
import { el } from '../world';

const ROW_H = 40;
const NAME_W = 210;
const ASK_W = 300;
const CELL_W = 250;
const HEAD_H = 44;

/**
 * Fifteen harness-engineering primitives against the two loops. Cell colour
 * is depth, cell text is the modules. A blank cell is a real statement: that
 * loop does not do this.
 */
export function buildMatrix(store: Store, x: number, y: number): Section {
  const rows = store.atlas.primitives;
  const width = NAME_W + ASK_W + CELL_W * 2;
  const height = HEAD_H + rows.length * ROW_H;

  const { root, body } = section(
    'map',
    'Coverage Map',
    'Rows are the primitives the field has converged on. Columns are this repository’s two loops.',
    x,
    y,
    width + 48,
  );

  const frame = card('padding:20px 22px 22px');
  frame.appendChild(eyebrow('coverage · cell shows modules, size, tests'));

  const grid = el('div', 'mx', `width:${width}px;height:${height}px`);
  const deepest = Math.max(...rows.flatMap((r) => [r.batch.sloc, r.repl.sloc]), 1);

  const head = [
    { label: '', x: 0, w: NAME_W },
    { label: 'the question it answers', x: NAME_W, w: ASK_W },
    { label: 'harness run — batch', x: NAME_W + ASK_W, w: CELL_W },
    { label: 'ay — conversational', x: NAME_W + ASK_W + CELL_W, w: CELL_W },
  ];
  for (const column of head) {
    grid.appendChild(
      el('div', 'mx-head mono', `left:${column.x}px;top:0;width:${column.w}px`, column.label),
    );
  }

  const cells: { node: HTMLDivElement; modules: string[] }[] = [];

  rows.forEach((row, index) => {
    const top = HEAD_H + index * ROW_H;
    grid.appendChild(
      el('div', 'mx-name', `left:0;top:${top}px;width:${NAME_W}px;height:${ROW_H}px`, row.name),
    );
    grid.appendChild(
      el(
        'div',
        'mx-ask',
        `left:${NAME_W}px;top:${top}px;width:${ASK_W}px;height:${ROW_H}px`,
        row.asks,
      ),
    );
    (['batch', 'repl'] as const).forEach((loop, column) => {
      const cell = row[loop];
      const left = NAME_W + ASK_W + column * CELL_W;
      const node = renderCell(store, row, cell, deepest);
      node.style.cssText += `left:${left}px;top:${top}px;width:${CELL_W}px;height:${ROW_H}px`;
      grid.appendChild(node);
      cells.push({ node, modules: cell.modules });
    });
  });

  frame.appendChild(grid);
  frame.appendChild(
    smallNote(
      'Primitive names follow the design-primitive catalogues; which module implements which is a ' +
        'reading of this repository, written down in scripts/taxonomy.py. Everything numeric in a ' +
        'cell is measured.',
    ),
  );
  body.appendChild(frame);

  const sync = () => {
    const emphasis = store.emphasis;
    for (const { node, modules } of cells) {
      const lit = store.sel.type === 'module' && modules.includes(store.sel.name);
      node.classList.toggle('on', lit);
      node.classList.toggle(
        'dim',
        !!emphasis && modules.length > 0 && !modules.some((m) => emphasis.has(m)),
      );
    }
  };
  sync();

  return { root, rect: { x, y, w: width + 48, h: height + 190 }, sync };
}

function renderCell(store: Store, row: Primitive, cell: Cell, deepest: number): HTMLDivElement {
  if (!cell.modules.length) {
    const gap = el('div', 'mx-cell mx-gap');
    gap.appendChild(el('span', 'mx-gap-mark mono', '', 'not present'));
    gap.title = `${row.name}: this loop does not implement it`;
    return gap;
  }
  // Depth is drawn on a square-root scale: a 1700-line row would otherwise
  // make every 100-line row look identically empty.
  const depth = Math.sqrt(cell.sloc / deepest);
  const swatch = layerColor(store.byName.get(cell.modules[0])?.layer ?? 'support');
  const node = el(
    'div',
    'mx-cell no-pan',
    `--depth:${(depth * 100).toFixed(1)}%;--tint:${swatch.solid}`,
  );
  const names = el('div', 'mx-mods mono');
  for (const name of cell.modules) {
    const chip = el('span', 'mx-mod', '', name.replace('repl.', ''));
    chip.addEventListener('click', () => store.select({ type: 'module', name }));
    names.appendChild(chip);
  }
  node.appendChild(names);
  node.appendChild(
    el(
      'div',
      'mx-num mono',
      '',
      `${cell.sloc.toLocaleString()} sloc · ${cell.tests} tests`,
    ),
  );
  if (cell.missing.length) {
    node.appendChild(el('div', 'mx-missing mono', '', `missing: ${cell.missing.join(', ')}`));
  }
  node.title = `${row.name}: ${cell.modules.join(', ')}`;
  return node;
}
