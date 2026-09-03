import { layerColor } from '../color';
import type { Store } from '../store';
import { card, row, section, smallNote, type Section } from '../widgets';
import { el } from '../world';

const WIDTH = 760;

/**
 * The two loops side by side over the layer they share. The point of the
 * drawing is the seam: the same providers, contracts and policy underneath,
 * two different loops on top, and no code path between them.
 */
export function buildLoops(store: Store, x: number, y: number): Section {
  const loops = store.atlas.loops;
  const { root, body } = section(
    'loops',
    'Two Loops',
    'They share the transport and the rules. They do not share the loop, because only one has a verdict to reach.',
    x,
    y,
    WIDTH,
  );

  const frame = card('padding:20px 22px 22px');

  const columns = el('div', 'loops');
  const nodes: { node: HTMLElement; module: string }[] = [];

  for (const loop of loops) {
    const column = el('div', 'loop-col');
    const head = el('div', 'loop-head');
    head.appendChild(el('span', 'loop-cmd mono', '', loop.name));
    head.appendChild(el('span', 'loop-shape', '', loop.shape));
    column.appendChild(head);

    const facts = el('div', 'kv-list');
    facts.appendChild(row('entry', loop.entry));
    facts.appendChild(row('loop', loop.root));
    facts.appendChild(row('workspace', loop.workspace));
    facts.appendChild(row('ends in', loop.ends));
    column.appendChild(facts);

    // What this loop has that the other does not.
    const mine = new Set<string>();
    const theirs = new Set<string>();
    for (const primitive of store.atlas.primitives) {
      const here = loop.key === 'batch' ? primitive.batch : primitive.repl;
      const other = loop.key === 'batch' ? primitive.repl : primitive.batch;
      if (here.modules.length && !other.modules.length) mine.add(primitive.name);
      if (!here.modules.length && other.modules.length) theirs.add(primitive.name);
    }
    if (mine.size) {
      column.appendChild(el('div', 'loop-label mono', '', 'only here'));
      const list = el('div', 'ev-row wrap');
      for (const name of mine) list.appendChild(el('span', 'ev loop-only', '', name));
      column.appendChild(list);
    }
    if (theirs.size) {
      column.appendChild(el('div', 'loop-label mono', '', 'absent here'));
      const list = el('div', 'ev-row wrap');
      for (const name of theirs) list.appendChild(el('span', 'ev loop-gap', '', name));
      column.appendChild(list);
    }
    columns.appendChild(column);
  }

  frame.appendChild(columns);

  frame.appendChild(el('div', 'loop-seam mono', '', 'both are built on'));
  const shared = el('div', 'shared-row');
  for (const name of store.atlas.shared) {
    const module = store.byName.get(name);
    const swatch = layerColor(module?.layer ?? 'support');
    const chip = el(
      'span',
      'shared-chip mono no-pan',
      `background:${swatch.bg};color:${swatch.fg};border-color:${swatch.bd}`,
      name,
    );
    chip.title = module?.doc ?? '';
    chip.addEventListener('click', () => store.select({ type: 'module', name }));
    shared.appendChild(chip);
    nodes.push({ node: chip, module: name });
  }
  frame.appendChild(shared);
  frame.appendChild(
    smallNote(
      'Nothing in harness/repl is imported by harness/runtime, or the other way round. The shared row ' +
        'is the whole seam, which is why a fix to the command deny-list binds both loops at once.',
    ),
  );
  body.appendChild(frame);

  const sync = () => {
    const emphasis = store.emphasis;
    for (const { node, module } of nodes) {
      node.classList.toggle('on', store.sel.type === 'module' && store.sel.name === module);
      node.classList.toggle('dim', !!emphasis && !emphasis.has(module));
    }
  };
  sync();

  return { root, rect: { x, y, w: WIDTH, h: 620 }, sync };
}
