import { riskColor } from '../color';
import type { Store } from '../store';
import { card, eyebrow, section, smallNote, tag, type Section } from '../widgets';
import { el } from '../world';

const WIDTH = 760;

//: Which tools the conversational loop registers. Read from the REPL toolset's
//: own names via the scanner would be better; until the scanner walks it, this
//: is the list `harness/repl/tools.py` declares.
const REPL_TOOLS = new Set([
  'read_file', 'list_dir', 'glob', 'grep', 'write_file', 'edit_file', 'run_command',
]);

/**
 * The tool surface as a grid: what the model may ask for, at what risk, on
 * which loop. The point is the asymmetry -- the two loops do not offer the
 * same capabilities, and editing is shaped differently on each.
 */
export function buildSurface(store: Store, x: number, y: number): Section {
  const a = store.atlas;
  const { root, body } = section(
    'tools',
    'Tool Surface',
    'Grouped by the risk class the policy gate reads. Arguments are the schema, not a description of it.',
    x,
    y,
    WIDTH,
  );

  const frame = card('padding:20px 22px 22px');
  frame.appendChild(eyebrow('tool · risk · arguments · which loop'));

  const table = el('div', 'tools-table');
  const header = el('div', 'tool-row tool-head mono');
  for (const [label, cls] of [
    ['tool', 'tr-name'], ['args', 'tr-args'], ['risk', 'tr-risk'],
    ['batch', 'tr-loop'], ['repl', 'tr-loop'],
  ] as const) {
    header.appendChild(el('span', cls, '', label));
  }
  table.appendChild(header);

  const nodes: { node: HTMLElement; name: string }[] = [];
  for (const risk of a.risks) {
    const group = a.tools.filter((t) => t.risk === risk);
    if (!group.length) continue;
    for (const tool of group) {
      const swatch = riskColor(risk);
      const line = el('div', 'tool-row no-pan');
      line.style.setProperty('--tint', swatch.solid);
      line.appendChild(el('span', 'tr-name mono', '', tool.name));
      const args = tool.arguments.length
        ? tool.arguments.map((n) => (tool.required.includes(n) ? n : `${n}?`)).join(' ')
        : '—';
      line.appendChild(el('span', 'tr-args mono', '', args));
      const riskCell = el('span', 'tr-risk');
      riskCell.appendChild(tag(risk, swatch));
      line.appendChild(riskCell);
      line.appendChild(el('span', 'tr-loop mono', '', '●'));
      line.appendChild(
        el('span', `tr-loop mono${REPL_TOOLS.has(tool.name) ? '' : ' tr-absent'}`, '',
           REPL_TOOLS.has(tool.name) ? '●' : '·'),
      );
      line.title = tool.description;
      line.addEventListener('click', () => store.select({ type: 'tool', name: tool.name }));
      table.appendChild(line);
      nodes.push({ node: line, name: tool.name });
    }
  }

  // Tools the REPL adds that the batch path has no equivalent for.
  const extra = [...REPL_TOOLS].filter((n) => !a.tools.some((t) => t.name === n));
  for (const name of extra) {
    const line = el('div', 'tool-row tool-extra');
    line.appendChild(el('span', 'tr-name mono', '', name));
    line.appendChild(el('span', 'tr-args mono', '', 'see harness/repl/tools.py'));
    const riskCell = el('span', 'tr-risk');
    riskCell.appendChild(tag(name.startsWith('run') ? 'execute' : name.includes('write') || name.includes('edit') ? 'write' : 'read',
                            riskColor(name.includes('write') || name.includes('edit') ? 'write' : name.startsWith('run') ? 'execute' : 'read')));
    line.appendChild(riskCell);
    line.appendChild(el('span', 'tr-loop mono tr-absent', '', '·'));
    line.appendChild(el('span', 'tr-loop mono', '', '●'));
    table.appendChild(line);
  }

  frame.appendChild(table);
  frame.appendChild(
    smallNote(
      'The batch loop patches with apply_patch, one unified diff at a time. The REPL replaces that with ' +
        'edit_file, which takes the exact text to replace and refuses when it is absent or ambiguous — ' +
        'the single change that most affects whether a smaller model can edit code at all.',
    ),
  );
  body.appendChild(frame);

  // Operator surface, as a dense grid rather than a list of sentences.
  const cli = card('padding:20px 22px');
  cli.appendChild(eyebrow(`operator verbs · ${a.commands.length}`));
  const verbs = el('div', 'verb-grid');
  for (const command of a.commands) {
    const chip = el('span', 'verb mono no-pan', '', command.group ? `auth ${command.name}` : command.name);
    chip.title = command.help;
    chip.addEventListener('click', () => store.select({ type: 'command', name: command.name }));
    verbs.appendChild(chip);
  }
  cli.appendChild(verbs);
  body.appendChild(cli);

  const sync = () => {
    const chosen = store.sel.type === 'tool' ? store.sel.name : null;
    for (const { node, name } of nodes) node.classList.toggle('on', name === chosen);
  };
  sync();

  return { root, rect: { x, y, w: WIDTH, h: 1000 }, sync };
}
