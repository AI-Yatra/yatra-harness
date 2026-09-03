import type { Store } from '../store';
import { card, eyebrow, section, smallNote, type Section } from '../widgets';
import { el } from '../world';

const WIDTH = 720;

/**
 * The authority chain as a filter, not a paragraph. A proposal enters at the
 * top; each gate either passes it down or drops it out to the right with a
 * named verdict. The right-hand column is every way a call can die.
 */
export function buildGates(store: Store, x: number, y: number): Section {
  const gates = store.atlas.gates;
  const { root, body } = section(
    'gates',
    'Refusal Gates',
    'A proposal falls through these in order. Anything that drops out never reaches the filesystem.',
    x,
    y,
    WIDTH,
  );

  const frame = card('padding:20px 22px 22px');
  frame.appendChild(eyebrow('proposal enters here'));

  const stack = el('div', 'gates');
  const nodes: { node: HTMLDivElement; module: string }[] = [];

  gates.forEach((gate, index) => {
    const row = el('div', `gate-row${gate.final ? '' : ' gate-asks'}`);

    const box = el('div', 'gate-box no-pan');
    box.appendChild(el('span', 'gate-n mono', '', String(index + 1)));
    box.appendChild(el('span', 'gate-name', '', gate.gate));
    const mod = el('span', 'gate-mod mono', '', gate.module);
    box.appendChild(mod);
    box.addEventListener('click', () => store.select({ type: 'module', name: gate.module }));
    row.appendChild(box);

    row.appendChild(el('div', 'gate-rule mono', '', gate.rule));
    row.appendChild(el('div', 'gate-out', '', '→'));
    row.appendChild(
      el(
        'div',
        `gate-verdict${gate.final ? ' final' : ' asks'}`,
        '',
        gate.verdict,
      ),
    );
    const scope = el('div', 'gate-scope mono', '', gate.loop);
    row.appendChild(scope);

    stack.appendChild(row);
    nodes.push({ node: box, module: gate.module });
    if (index < gates.length - 1) stack.appendChild(el('div', 'gate-link'));
  });

  frame.appendChild(stack);
  frame.appendChild(el('div', 'gate-exit', '', 'passes all → the side effect runs'));
  frame.appendChild(
    smallNote(
      'Solid verdicts are final: no mode and no operator may authorize them, because a human clicking ' +
        'yes on a prompt is the mistake the deny-list exists to prevent. Only "asks a human" is a ' +
        'question rather than an answer.',
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

  return { root, rect: { x, y, w: WIDTH, h: 200 + gates.length * 54 }, sync };
}
