import { rampCSS } from '../color';
import type { Store } from '../store';
import { card, eyebrow, section, stat, type Section } from '../widgets';
import { el } from '../world';

const WIDTH = 620;

/** The header: the rule, the counts, the key. No paragraphs. */
export function buildIntro(store: Store, x: number, y: number): Section {
  const a = store.atlas;
  const t = a.totals;
  const { root, body } = section('start', 'Harness Atlas', '', x, y, WIDTH);

  const rule = card('padding:22px 24px');
  rule.appendChild(eyebrow('the invariant', true));
  rule.appendChild(
    el(
      'blockquote',
      'pull',
      '',
      'The model proposes. The harness decides, executes, records, recovers, and proves.',
    ),
  );
  body.appendChild(rule);

  const stats = card('padding:20px 22px');
  stats.appendChild(eyebrow(`measured at ${a.head.sha ?? 'head'} · ${a.head.date ?? ''}`));
  const grid = el('div', 'stat-grid');
  const gaps = a.primitives.filter((p) => !p.repl.modules.length).length;
  const cells: [string, string, string][] = [
    [String(t.modules), 'modules', 'Python modules in harness/'],
    [t.sloc.toLocaleString(), 'lines', 'neither blank nor comment'],
    [String(t.edges), 'import edges', 'inside the package'],
    [String(a.primitives.length), 'primitives', 'rows of the coverage matrix'],
    [String(t.tools), 'tools', 'literal ToolSpec constructions'],
    [String(a.gates.length), 'refusal gates', 'ways a call can be stopped'],
    [String(a.events.length), 'event types', 'string literals reaching the ledger'],
    [String(a.statuses.length), 'run states', `${gaps} primitives absent from the REPL`],
  ];
  for (const [v, l, hint] of cells) grid.appendChild(stat(v, l, hint));
  stats.appendChild(grid);
  body.appendChild(stats);

  const key = card('padding:18px 22px');
  key.appendChild(eyebrow('key'));
  const legend = el('div', 'key-list');
  const entries: [string, string][] = [
    ['measured', 'counted from the AST, the ledger literals, or git'],
    ['a reading', 'written in scripts/taxonomy.py; argue with it there'],
    ['hatched', 'the metric does not apply — never a silent zero'],
    ['dashed', 'batch loop only'],
  ];
  for (const [term, meaning] of entries) {
    const line = el('div', 'key-row');
    line.appendChild(el('span', `key-mark key-${term.replace(/\s/g, '')}`, '', ''));
    line.appendChild(el('span', 'key-term mono', '', term));
    line.appendChild(el('span', 'key-meaning', '', meaning));
    legend.appendChild(line);
  }
  key.appendChild(legend);

  const scale = el('div', 'scale-row');
  scale.appendChild(el('span', 'scale-cap mono', '', 'less'));
  scale.appendChild(el('span', 'scale-bar', `background:${rampCSS()}`));
  scale.appendChild(el('span', 'scale-cap mono', '', 'more'));
  key.appendChild(scale);
  key.appendChild(
    el('div', 'code mono', '', 'python3 docs/atlas/scripts/scan_harness.py'),
  );
  body.appendChild(key);

  return { root, rect: { x, y, w: WIDTH, h: 700 } };
}
