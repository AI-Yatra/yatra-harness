import { el, type Rect } from './world';

export interface Section {
  root: HTMLDivElement;
  rect: Rect;
  /** Recolour or re-emphasise in place when the store changes. */
  sync?: () => void;
}

export function section(
  tag: string,
  title: string,
  sub: string,
  x: number,
  y: number,
  width: number,
): { root: HTMLDivElement; body: HTMLDivElement } {
  const root = el('div', 'section', `left:${x}px;top:${y}px;width:${width}px`);
  root.dataset.region = tag;
  const head = el('div', 'section-head');
  head.appendChild(el('span', 'section-tag mono', '', tag));
  head.appendChild(el('h2', 'section-title', '', title));
  root.appendChild(head);
  if (sub) root.appendChild(el('p', 'section-sub', '', sub));
  const body = el('div', 'section-body');
  root.appendChild(body);
  return { root, body };
}

export function card(style = ''): HTMLDivElement {
  return el('div', 'card', style);
}

export function eyebrow(text: string, accent = false): HTMLDivElement {
  return el('div', `eyebrow mono${accent ? ' accent' : ''}`, '', text);
}

export function note(text: string): HTMLParagraphElement {
  return el<HTMLParagraphElement>('p', 'note', '', text);
}

export function smallNote(text: string): HTMLParagraphElement {
  return el<HTMLParagraphElement>('p', 'small-note', '', text);
}

/** A number with its label under it, for the stat rows. */
export function stat(value: string, label: string, hint = ''): HTMLDivElement {
  const box = el('div', 'stat');
  box.appendChild(el('div', 'stat-value mono', '', value));
  box.appendChild(el('div', 'stat-label', '', label));
  if (hint) box.title = hint;
  return box;
}

export function chip(text: string, cls = ''): HTMLSpanElement {
  return el<HTMLSpanElement>('span', `chip mono no-pan ${cls}`.trim(), '', text);
}

export function tag(text: string, swatch: { bg: string; fg: string; bd: string }): HTMLSpanElement {
  return el<HTMLSpanElement>(
    'span',
    'tag mono',
    `background:${swatch.bg};color:${swatch.fg};border-color:${swatch.bd}`,
    text,
  );
}

/** A definition row: label on the left, value on the right. */
export function row(label: string, value: string): HTMLDivElement {
  const line = el('div', 'kv');
  line.appendChild(el('span', 'kv-k', '', label));
  line.appendChild(el('span', 'kv-v mono', '', value));
  return line;
}

export function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
