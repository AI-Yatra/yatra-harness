import { METRICS, type Module } from './data';
import { colorOf, layerColor, riskColor, tOf } from './color';
import type { Store } from './store';
import { count, eyebrow, row, smallNote, tag } from './widgets';
import { el } from './world';

export interface Panel {
  root: HTMLDivElement;
  sync(): void;
}

/**
 * The inspector. Whatever is selected, this is where its real numbers go, in
 * plain language rather than as a chart.
 */
export function buildPanel(store: Store, onGoto: (name: string) => void): Panel {
  const root = el('div', 'panel');
  const inner = el('div', 'panel-inner scrolls');
  const close = el<HTMLButtonElement>('button', 'panel-close mono no-pan', '', '×');
  close.title = 'Close (Esc)';
  close.addEventListener('click', () => store.clear());
  root.appendChild(close);
  root.appendChild(inner);

  const sync = () => {
    const sel = store.sel;
    root.classList.toggle('open', sel.type !== 'none');
    if (sel.type === 'none') return;
    inner.replaceChildren();
    inner.scrollTop = 0;
    if (sel.type === 'module') renderModule(inner, store, sel.name, onGoto);
    else if (sel.type === 'tool') renderTool(inner, store, sel.name);
    else if (sel.type === 'command') renderCommand(inner, store, sel.name);
    else if (sel.type === 'event') renderEvent(inner, store, sel.name, onGoto);
    else if (sel.type === 'layer') renderLayer(inner, store, sel.key, onGoto);
  };

  sync();
  return { root, sync };
}

function heading(host: HTMLElement, kicker: string, title: string, sub = ''): void {
  host.appendChild(eyebrow(kicker, true));
  host.appendChild(el('h3', 'panel-title mono', '', title));
  if (sub) host.appendChild(el('p', 'panel-sub', '', sub));
}

function linkList(
  host: HTMLElement,
  label: string,
  names: string[],
  store: Store,
  onGoto: (name: string) => void,
): void {
  if (!names.length) return;
  host.appendChild(el('div', 'panel-label mono', '', `${label} · ${names.length}`));
  const wrap = el('div', 'ev-row wrap');
  for (const name of names) {
    const m = store.byName.get(name);
    const swatch = layerColor(m?.layer ?? 'support');
    const pill = el(
      'span',
      'ev mono no-pan',
      m ? `background:${swatch.bg};color:${swatch.fg};border-color:${swatch.bd}` : '',
      name,
    );
    if (m) {
      pill.title = m.doc;
      pill.addEventListener('click', () => {
        store.select({ type: 'module', name });
        onGoto(name);
      });
    }
    wrap.appendChild(pill);
  }
  host.appendChild(wrap);
}

function renderModule(
  host: HTMLElement,
  store: Store,
  name: string,
  onGoto: (name: string) => void,
): void {
  const m = store.byName.get(name);
  if (!m) return;
  const layer = store.atlas.layers.find((l) => l.key === m.layer);
  heading(host, layer?.title ?? m.layer, m.name, m.path);

  if (m.in_boundary) {
    const stage = store.atlas.boundary.find((s) => s.module === m.name);
    host.appendChild(
      el('div', 'panel-flag', '', `On the authority boundary: ${stage?.stage ?? ''}. ${stage?.note ?? ''}`),
    );
  }

  if (m.doc_full) host.appendChild(el('p', 'panel-doc', '', m.doc_full));

  // Every metric for this one module, on the same scale the canvas uses.
  host.appendChild(el('div', 'panel-label mono', '', 'measured'));
  const bars = el('div', 'bars');
  for (const def of METRICS) {
    const value = def.get(m);
    const range = { lo: 0, hi: 1 };
    const all = store.atlas.modules;
    const values = all.map((x) => def.get(x)).filter((v): v is number => v != null);
    range.lo = Math.min(...values.map(def.transform));
    range.hi = Math.max(...values.map(def.transform));
    if (range.hi === range.lo) range.hi = range.lo + 1;
    const t = tOf(value, def, range);

    const line = el('div', 'bar-row');
    line.appendChild(el('span', 'bar-label', '', def.label));
    const track = el('div', 'bar-track');
    if (t == null) {
      track.classList.add('bar-na');
    } else {
      track.appendChild(
        el('div', 'bar-fill', `width:${Math.max(2, t * 100)}%;background:${colorOf(value, def, range)}`),
      );
    }
    line.appendChild(track);
    line.appendChild(
      el('span', 'bar-value mono', '', value == null ? 'n/a' : def.format(value).split(' ')[0]),
    );
    line.title = value == null ? `${def.label}: not applicable. ${def.blurb}` : `${def.format(value)}. ${def.blurb}`;
    bars.appendChild(line);
  }
  host.appendChild(bars);

  const kv = el('div', 'kv-list');
  kv.appendChild(row('lines in file', String(m.lines)));
  kv.appendChild(row('code lines', String(m.sloc)));
  kv.appendChild(row('public classes', String(m.classes)));
  kv.appendChild(row('public functions', String(m.functions)));
  if (m.last_touched) kv.appendChild(row('last touched', m.last_touched));
  host.appendChild(kv);

  linkList(host, 'imports', m.imports, store, onGoto);
  linkList(host, 'imported by', m.imported_by, store, onGoto);

  if (m.external.length) {
    host.appendChild(el('div', 'panel-label mono', '', `outside the package · ${m.external.length}`));
    host.appendChild(el('div', 'panel-flat mono', '', m.external.join(' · ')));
  }

  if (m.api.length) {
    host.appendChild(el('div', 'panel-label mono', '', `public surface · ${m.api.length}`));
    const list = el('div', 'api-list');
    for (const entry of m.api) {
      const item = el('div', 'api');
      const line = el('div', 'api-line');
      line.appendChild(el('span', `api-kind mono kind-${entry.kind}`, '', entry.kind === 'class' ? 'class' : 'def'));
      line.appendChild(el('span', 'api-name mono', '', entry.name));
      line.appendChild(el('span', 'api-line-n mono', '', `:${entry.line}`));
      item.appendChild(line);
      if (entry.doc) item.appendChild(el('div', 'api-doc', '', entry.doc));
      if (entry.methods.length) {
        item.appendChild(
          el('div', 'api-methods mono', '', entry.methods.map((x) => `${x}()`).join(' · ')),
        );
      }
      list.appendChild(item);
    }
    host.appendChild(list);
  }

  host.appendChild(el('div', 'panel-label mono', '', 'tested by'));
  if (m.tests.length) {
    host.appendChild(
      el('div', 'panel-flat mono', '', m.tests.map((f) => f.replace(/\.py$/, '')).join(' · ')),
    );
    host.appendChild(
      smallNote(
        `${count(m.test_cases, 'case')} across ${count(m.tests.length, 'file')}, ` +
          `${m.test_sloc.toLocaleString()} lines, ` +
          `${(m.test_sloc / Math.max(1, m.sloc)).toFixed(2)}x the size of the module.`,
      ),
    );
  } else {
    host.appendChild(
      smallNote('No test file imports this module directly. It may still be exercised through another.'),
    );
  }
}

function renderTool(host: HTMLElement, store: Store, name: string): void {
  const tool = store.toolByName.get(name);
  if (!tool) return;
  const swatch = riskColor(tool.risk);
  heading(host, `tool · ${tool.risk}`, tool.name, `harness/tools.py:${tool.line}`);
  const badge = el('div', 'pill-row');
  badge.appendChild(tag(`risk: ${tool.risk}`, swatch));
  host.appendChild(badge);
  host.appendChild(el('p', 'panel-doc', '', tool.description));

  host.appendChild(el('div', 'panel-label mono', '', 'arguments'));
  if (tool.arguments.length) {
    const kv = el('div', 'kv-list');
    for (const arg of tool.arguments) {
      kv.appendChild(row(arg, tool.required.includes(arg) ? 'required' : 'optional'));
    }
    host.appendChild(kv);
  } else {
    host.appendChild(smallNote('None. The schema takes an empty object.'));
  }

  host.appendChild(el('div', 'panel-label mono', '', 'what it has to pass'));
  host.appendChild(
    el(
      'p',
      'panel-doc',
      '',
      'The registry validates these arguments against the tool schema, then the policy gate reads the ' +
        `${tool.risk} risk class against the configured allowlist and approval rules. Only then does the ` +
        'handler run, and its result is written to the ledger either way.',
    ),
  );
}

function renderCommand(host: HTMLElement, store: Store, name: string): void {
  const cmd = store.commandByName.get(name);
  if (!cmd) return;
  const full = cmd.group ? `auth ${cmd.name}` : cmd.name;
  heading(host, 'operator verb', full, `harness/cli.py:${cmd.line}`);
  host.appendChild(el('p', 'panel-doc', '', cmd.help));
  host.appendChild(el('div', 'code mono', '', `python -m harness ${full}`));
}

function renderEvent(
  host: HTMLElement,
  store: Store,
  name: string,
  onGoto: (name: string) => void,
): void {
  const ev = store.eventByName.get(name);
  if (!ev) return;
  heading(host, ev.terminal ? 'terminal event' : 'ledger event', ev.type);
  host.appendChild(
    el(
      'p',
      'panel-doc',
      '',
      ev.terminal
        ? 'A run writes exactly one terminal event and then stops. Reading the ledger backwards to the ' +
          'first of these is how a finished run is classified without loading its state.'
        : 'Written during the run. Every event carries a monotonic sequence number, a run id, and a ' +
          'redacted payload, and a gap in the sequence is an error on read.',
    ),
  );
  linkList(host, 'written by', ev.writers, store, onGoto);
}

function renderLayer(
  host: HTMLElement,
  store: Store,
  key: string,
  onGoto: (name: string) => void,
): void {
  const layer = store.atlas.layers.find((l) => l.key === key);
  if (!layer) return;
  const modules = layer.modules
    .map((n) => store.byName.get(n))
    .filter((m): m is Module => !!m);
  const sloc = modules.reduce((s, m) => s + m.sloc, 0);
  heading(host, 'layer', layer.title, layer.blurb);
  host.appendChild(
    el('div', 'panel-flag', '', 'Which layer a module belongs to is a reading of the design, written down in the scanner rather than measured.'),
  );
  const kv = el('div', 'kv-list');
  kv.appendChild(row('modules', String(modules.length)));
  kv.appendChild(row('code lines', sloc.toLocaleString()));
  kv.appendChild(row('share of package', `${Math.round((sloc / store.atlas.totals.sloc) * 100)}%`));
  kv.appendChild(row('test cases', String(modules.reduce((s, m) => s + m.test_cases, 0))));
  host.appendChild(kv);
  linkList(
    host,
    'modules',
    modules.sort((p, q) => q.sloc - p.sloc).map((m) => m.name),
    store,
    onGoto,
  );
}
