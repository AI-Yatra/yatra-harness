import {
  METRICS,
  metricByKey,
  rangeOf,
  type Atlas,
  type Command,
  type EventType,
  type MetricDef,
  type Module,
  type Range,
  type Tool,
} from './data';
import type { Rect } from './world';

export type Selection =
  | { type: 'none' }
  | { type: 'module'; name: string }
  | { type: 'tool'; name: string }
  | { type: 'command'; name: string }
  | { type: 'event'; name: string }
  | { type: 'layer'; key: string };

/** Shared state every section reads and no section owns. */
export class Store {
  atlas: Atlas;
  metricKey: string;
  sel: Selection = { type: 'none' };
  /** Module name currently hovered anywhere, for cross-section highlighting. */
  hover: string | null = null;

  readonly byName: Map<string, Module>;
  readonly toolByName: Map<string, Tool>;
  readonly commandByName: Map<string, Command>;
  readonly eventByName: Map<string, EventType>;
  /** Where each module's cell sits in world coordinates, filled in by the wall. */
  cellRect = new Map<string, Rect>();

  private listeners: (() => void)[] = [];

  constructor(atlas: Atlas) {
    this.atlas = atlas;
    this.metricKey = localStorage.getItem('atlas.metric') ?? METRICS[0].key;
    if (!METRICS.some((m) => m.key === this.metricKey)) this.metricKey = METRICS[0].key;
    this.byName = new Map(atlas.modules.map((m) => [m.name, m]));
    this.toolByName = new Map(atlas.tools.map((t) => [t.name, t]));
    this.commandByName = new Map(atlas.commands.map((c) => [c.name, c]));
    this.eventByName = new Map(atlas.events.map((e) => [e.type, e]));
  }

  get metric(): MetricDef {
    return metricByKey(this.metricKey);
  }

  get range(): Range {
    return rangeOf(this.metric, this.atlas.modules);
  }

  setMetric(key: string): void {
    if (key === this.metricKey) return;
    this.metricKey = key;
    localStorage.setItem('atlas.metric', key);
    this.emit();
  }

  select(sel: Selection): void {
    if (sel.type === this.sel.type && keyOf(sel) === keyOf(this.sel)) {
      this.sel = { type: 'none' };
    } else {
      this.sel = sel;
    }
    this.emit();
  }

  clear(): void {
    if (this.sel.type === 'none') return;
    this.sel = { type: 'none' };
    this.emit();
  }

  setHover(name: string | null): void {
    if (this.hover === name) return;
    this.hover = name;
    this.emit();
  }

  /** The module currently selected, if the selection is a module at all. */
  get selectedModule(): Module | null {
    return this.sel.type === 'module' ? (this.byName.get(this.sel.name) ?? null) : null;
  }

  /**
   * Modules touching the current selection or hover, for dimming everything
   * else. Null means nothing is emphasised and all modules draw at full ink.
   */
  get emphasis(): Set<string> | null {
    const focus = this.hover ?? (this.sel.type === 'module' ? this.sel.name : null);
    if (focus) {
      const m = this.byName.get(focus);
      if (!m) return null;
      return new Set([m.name, ...m.imports, ...m.imported_by]);
    }
    const sel = this.sel;
    if (sel.type === 'layer') {
      const layer = this.atlas.layers.find((l) => l.key === sel.key);
      return layer ? new Set(layer.modules) : null;
    }
    if (sel.type === 'event') {
      const ev = this.eventByName.get(sel.name);
      return ev ? new Set(ev.writers) : null;
    }
    return null;
  }

  onChange(fn: () => void): void {
    this.listeners.push(fn);
  }

  emit(): void {
    for (const fn of this.listeners) fn();
  }
}

function keyOf(sel: Selection): string {
  return sel.type === 'layer' ? sel.key : sel.type === 'none' ? '' : sel.name;
}
