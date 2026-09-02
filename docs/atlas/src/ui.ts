import { METRICS } from './data';
import { layerColor, rampCSS } from './color';
import type { Store } from './store';
import { el, type FlyOpts, type Rect, type World } from './world';

export interface Tour {
  key: string;
  label: string;
  rect: Rect;
}

export interface Chrome {
  sync(): void;
}

/** Header: the title, the metric switch, the search box, and the theme toggle. */
export function buildTopbar(host: HTMLElement, store: Store, onFind: (name: string) => void): Chrome {
  const bar = el('div', 'topbar overlay no-pan');

  const brand = el('div', 'brand');
  brand.appendChild(el('span', 'brand-name', '', 'Harness Atlas'));
  brand.appendChild(
    el('span', 'brand-sub mono', '', `${store.atlas.repo} · ${store.atlas.head.sha ?? ''}`),
  );
  bar.appendChild(brand);

  const metrics = el('div', 'metric-row');
  metrics.appendChild(el('span', 'metric-cap mono', '', 'colour by'));
  const buttons: { node: HTMLElement; key: string }[] = [];
  for (const def of METRICS) {
    const node = el('span', 'chip mono', '', def.label);
    node.title = def.blurb;
    node.addEventListener('click', () => store.setMetric(def.key));
    metrics.appendChild(node);
    buttons.push({ node, key: def.key });
  }
  const scale = el('span', 'scale-bar mini', `background:${rampCSS()}`);
  scale.title = 'Warm is less, cool is more. Hatching means the metric does not apply.';
  metrics.appendChild(scale);
  bar.appendChild(metrics);

  const search = el<HTMLInputElement>('input', 'search mono');
  search.type = 'search';
  search.placeholder = 'find a module…';
  search.spellcheck = false;
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    if (!q) return;
    const hit =
      store.atlas.modules.find((m) => m.name === q) ??
      store.atlas.modules.find((m) => m.name.startsWith(q)) ??
      store.atlas.modules.find((m) => m.name.includes(q) || m.doc.toLowerCase().includes(q));
    if (hit) {
      store.select({ type: 'module', name: hit.name });
      onFind(hit.name);
    }
  });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      search.value = '';
      search.blur();
    }
    e.stopPropagation();
  });
  bar.appendChild(search);

  const theme = el('span', 'zbtn mono', '', '◐');
  theme.title = 'Light or dark';
  theme.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('atlas.theme', next);
  });
  bar.appendChild(theme);

  host.appendChild(bar);

  const sync = () => {
    for (const b of buttons) b.node.classList.toggle('on', b.key === store.metricKey);
  };
  sync();
  return { sync };
}

/** Footer: the region jumps, the zoom buttons, and the active metric caption. */
export function buildBottombar(
  host: HTMLElement,
  store: Store,
  world: World,
  tours: Tour[],
  view: () => FlyOpts,
): Chrome {
  const bar = el('div', 'bottombar overlay no-pan');

  const jumps = el('div', 'jump-row');
  const buttons: { node: HTMLElement; rect: Rect }[] = [];
  for (const tour of tours) {
    const node = el('span', 'chip mono mini', '', tour.label);
    node.addEventListener('click', () =>
      world.flyTo(pad(tour.rect), { ...view(), maxZoom: 0.92 }),
    );
    jumps.appendChild(node);
    buttons.push({ node, rect: tour.rect });
  }
  bar.appendChild(jumps);

  const caption = el('span', 'metric-caption');
  bar.appendChild(caption);

  const zooms = el('div', 'zoom-row');
  const fit = el('span', 'zbtn mono', '', '⤢');
  fit.title = 'Fit the whole canvas';
  fit.addEventListener('click', () =>
    world.flyTo({ x: 0, y: 0, w: world.size.w, h: world.size.h }, view()),
  );
  const minus = el('span', 'zbtn mono', '', '−');
  minus.addEventListener('click', () => world.nudgeZoom(1 / 1.35));
  const plus = el('span', 'zbtn mono', '', '+');
  plus.addEventListener('click', () => world.nudgeZoom(1.35));
  zooms.append(fit, minus, plus);
  bar.appendChild(zooms);

  host.appendChild(bar);

  const sync = () => {
    const def = store.metric;
    caption.textContent = `${def.label} — ${def.blurb}`;
    // Light the region the viewport is mostly sitting in.
    const v = world.viewport();
    let bestIdx = -1;
    let bestArea = 0;
    buttons.forEach((b, i) => {
      const area = overlap(v, b.rect);
      if (area > bestArea) {
        bestArea = area;
        bestIdx = i;
      }
    });
    buttons.forEach((b, i) => b.node.classList.toggle('on', i === bestIdx));
  };
  sync();
  return { sync };
}

/** A minimap of the whole canvas with the viewport drawn on it. */
export function buildMinimap(
  host: HTMLElement,
  world: World,
  tours: Tour[],
  view: () => FlyOpts,
): Chrome {
  const MAP_W = 186;
  const scale = MAP_W / world.size.w;
  const MAP_H = Math.round(world.size.h * scale);

  const map = el('div', 'minimap overlay no-pan', `width:${MAP_W}px;height:${MAP_H}px`);
  for (const tour of tours) {
    const box = el(
      'div',
      'minimap-region',
      `left:${tour.rect.x * scale}px;top:${tour.rect.y * scale}px;` +
        `width:${Math.max(3, tour.rect.w * scale)}px;height:${Math.max(3, tour.rect.h * scale)}px`,
    );
    box.title = tour.label;
    box.addEventListener('click', () =>
      world.flyTo(pad(tour.rect), { ...view(), maxZoom: 0.92 }),
    );
    map.appendChild(box);
  }
  const viewBox = el('div', 'minimap-view');
  map.appendChild(viewBox);

  map.addEventListener('click', (e) => {
    if ((e.target as HTMLElement).classList.contains('minimap-region')) return;
    const r = map.getBoundingClientRect();
    const wx = (e.clientX - r.left) / scale;
    const wy = (e.clientY - r.top) / scale;
    const v = world.viewport();
    world.flyTo({ x: wx - v.w / 2, y: wy - v.h / 2, w: v.w, h: v.h }, { ...view(), maxZoom: world.zoom });
  });

  host.appendChild(map);

  const sync = () => {
    const v = world.viewport();
    viewBox.style.left = `${v.x * scale}px`;
    viewBox.style.top = `${v.y * scale}px`;
    viewBox.style.width = `${Math.max(4, v.w * scale)}px`;
    viewBox.style.height = `${Math.max(4, v.h * scale)}px`;
  };
  sync();
  return { sync };
}

/** A fixed key for the layer colours, so a band's colour means something. */
export function buildLegend(host: HTMLElement, store: Store): Chrome {
  const box = el('div', 'legend overlay no-pan');
  box.appendChild(el('div', 'legend-cap mono', '', 'layers'));
  const items: { node: HTMLElement; key: string }[] = [];
  for (const layer of store.atlas.layers) {
    const swatch = layerColor(layer.key);
    const line = el('div', 'legend-item');
    line.appendChild(el('span', 'legend-dot', `background:${swatch.solid}`));
    line.appendChild(el('span', 'legend-name', '', layer.title));
    line.appendChild(el('span', 'legend-n mono', '', String(layer.modules.length)));
    line.title = layer.blurb;
    line.addEventListener('click', () => store.select({ type: 'layer', key: layer.key }));
    box.appendChild(line);
    items.push({ node: line, key: layer.key });
  }
  host.appendChild(box);

  const sync = () => {
    for (const item of items) {
      item.node.classList.toggle('on', store.sel.type === 'layer' && store.sel.key === item.key);
    }
  };
  sync();
  return { sync };
}

function pad(r: Rect): Rect {
  return { x: r.x - 60, y: r.y - 60, w: r.w + 120, h: r.h + 120 };
}

function overlap(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}
