import './style.css';
import './diagrams.css';
import { loadAtlas } from './data';
import { buildPanel } from './panel';
import { buildGates } from './sections/gates';
import { buildGraph } from './sections/graph';
import { buildIntro } from './sections/intro';
import { buildLoops } from './sections/loops';
import { buildMatrix } from './sections/matrix';
import { buildState } from './sections/state';
import { buildTurn } from './sections/turn';
import { buildMass } from './sections/mass';
import { buildSurface } from './sections/surface';
import { buildWall } from './sections/wall';
import { Store } from './store';
import { buildBottombar, buildLegend, buildMinimap, buildTopbar, type Tour } from './ui';
import { el, World, type FlyOpts } from './world';

const app = document.getElementById('app')!;

document.documentElement.dataset.theme = localStorage.getItem('atlas.theme') ?? 'light';

// A wash of colour under each region, so the regions read as places rather
// than as cards floating on one flat sheet.
const WASHES = [
  'rgba(206,228,224,0.5)',
  'rgba(255,214,178,0.42)',
  'rgba(219,208,236,0.4)',
  'rgba(224,216,243,0.36)',
  'rgba(246,226,201,0.48)',
  'rgba(206,228,224,0.42)',
  'rgba(255,214,178,0.36)',
];

async function boot(): Promise<void> {
  const loading = el(
    'div',
    'boot mono',
    '',
    'reading the harness…',
  );
  app.appendChild(loading);

  let atlas;
  try {
    atlas = await loadAtlas();
  } catch (err) {
    loading.textContent = String(err instanceof Error ? err.message : err);
    loading.classList.add('boot-error');
    return;
  }
  app.replaceChildren();

  const store = new Store(atlas);
  // A placeholder size; the real one is known once the regions are measured.
  const world = new World(app, { w: 1000, h: 1000 });

  // ── regions ──
  // Sections are built at the origin, measured, and only then placed: their
  // real height depends on the data, so a hand-written layout would drift the
  // moment the harness grows a module.
  const intro = buildIntro(store, 0, 0);
  const matrix = buildMatrix(store, 0, 0);
  const turn = buildTurn(store, 0, 0);
  const gates = buildGates(store, 0, 0);
  const state = buildState(store, 0, 0);
  const loops = buildLoops(store, 0, 0);
  const surface = buildSurface(store, 0, 0);
  const wall = buildWall(store, 0, 0);
  const graph = buildGraph(store, 0, 0);
  const mass = buildMass(store, 0, 0);

  const sections = [intro, matrix, turn, gates, state, loops, surface, wall, graph, mass];
  for (const s of sections) world.world.appendChild(s.root);

  // Columns pair a claim with the measurement that backs it: the argument over
  // the module wall, the boundary over the graph that shows it holds, the run
  // over where the code actually is.
  // Column one states the claim, the rest are the evidence for it.
  const columns = [
    [intro, gates, loops],
    [matrix, wall],
    [turn, state],
    [surface, graph, mass],
  ];
  const MARGIN = 120;
  const GAP = 110;
  let left = MARGIN;
  let bottom = 0;
  for (const column of columns) {
    let top = MARGIN;
    let widest = 0;
    for (const s of column) {
      s.rect.w = s.root.offsetWidth;
      s.rect.h = s.root.offsetHeight;
      s.rect.x = left;
      s.rect.y = top;
      s.root.style.left = `${left}px`;
      s.root.style.top = `${top}px`;
      top += s.rect.h + GAP * 1.3;
      widest = Math.max(widest, s.rect.w);
    }
    left += widest + GAP;
    bottom = Math.max(bottom, top);
  }

  // The wall hands out its cell positions in world coordinates, so they have to
  // be shifted by wherever the wall actually landed.
  for (const [name, r] of store.cellRect) {
    store.cellRect.set(name, { ...r, x: r.x + wall.rect.x, y: r.y + wall.rect.y });
  }

  sections.forEach((s, i) => {
    const pad = 130;
    world.world.insertBefore(
      el(
        'div',
        'wash',
        `left:${s.rect.x - pad}px;top:${s.rect.y - pad}px;` +
          `width:${s.rect.w + pad * 2}px;height:${s.rect.h + pad * 2}px;` +
          `background:radial-gradient(ellipse at 50% 50%,${WASHES[i % WASHES.length]},transparent 68%)`,
      ),
      world.world.firstChild,
    );
  });

  world.size = { w: left - GAP + MARGIN, h: bottom - GAP * 1.3 + MARGIN };
  world.world.style.width = `${world.size.w}px`;
  world.world.style.height = `${world.size.h}px`;

  const tours: Tour[] = [
    { key: 'start', label: 'start', rect: intro.rect },
    { key: 'map', label: 'map', rect: matrix.rect },
    { key: 'turn', label: 'turn', rect: turn.rect },
    { key: 'gates', label: 'gates', rect: gates.rect },
    { key: 'state', label: 'state', rect: state.rect },
    { key: 'loops', label: 'loops', rect: loops.rect },
    { key: 'tools', label: 'tools', rect: surface.rect },
    { key: 'wall', label: 'wall', rect: wall.rect },
    { key: 'graph', label: 'graph', rect: graph.rect },
    { key: 'mass', label: 'mass', rect: mass.rect },
  ];

  // ── chrome ──
  const narrow = () => window.innerWidth < 820;
  const panelOpen = () => store.sel.type !== 'none';
  // The header, footer and inspector all sit over the canvas, so a fly-to has
  // to aim at the part of the viewport that is actually still visible.
  const view = (): FlyOpts => ({
    padRight: panelOpen() && !narrow() ? Math.min(430, window.innerWidth * 0.36) + 26 : 0,
    padTop: narrow() ? 110 : 80,
    padBottom: panelOpen() && narrow() ? window.innerHeight * 0.62 + 20 : 70,
  });

  const flyToModule = (name: string) => {
    const r = store.cellRect.get(name);
    if (r) {
      world.flyTo({ x: r.x - 300, y: r.y - 220, w: r.w + 600, h: r.h + 440 }, { ...view(), maxZoom: 1.5 });
    }
  };

  const panel = buildPanel(store, flyToModule);
  app.appendChild(panel.root);

  const topbar = buildTopbar(app, store, flyToModule);
  const bottombar = buildBottombar(app, store, world, tours, view);
  const minimap = buildMinimap(app, world, tours, view);
  const legend = buildLegend(app, store);

  const chrome = [topbar, bottombar, minimap, legend];
  const syncAll = () => {
    for (const s of sections) s.sync?.();
    panel.sync();
    for (const c of chrome) c.sync();
  };
  store.onChange(syncAll);
  world.onChange = () => {
    bottombar.sync();
    minimap.sync();
  };

  // Clicking the bare canvas closes the inspector.
  world.board.addEventListener('click', (e) => {
    if (world.moved) return;
    const t = e.target as HTMLElement;
    if (t === world.board || t === world.world || t.classList.contains('wash')) store.clear();
  });

  window.addEventListener('keydown', (e) => {
    if ((e.target as HTMLElement).tagName === 'INPUT') return;
    if (e.key === 'Escape') store.clear();
    else if (e.key === '+' || e.key === '=') world.nudgeZoom(1.35);
    else if (e.key === '-' || e.key === '_') world.nudgeZoom(1 / 1.35);
    else if (e.key === '0') {
      world.flyTo({ x: 0, y: 0, w: world.size.w, h: world.size.h }, view());
    } else if (e.key >= '1' && e.key <= String(tours.length)) {
      const tour = tours[Number(e.key) - 1];
      world.flyTo(
        { x: tour.rect.x - 60, y: tour.rect.y - 60, w: tour.rect.w + 120, h: tour.rect.h + 120 },
        { ...view(), maxZoom: 0.92 },
      );
    }
  });

  // Open on the argument, not on the whole board, so the first thing readable
  // is the sentence the rest of the canvas is evidence for.
  world.flyTo(
    { x: intro.rect.x - 70, y: intro.rect.y - 90, w: intro.rect.w + 140, h: intro.rect.h + 140 },
    { ...view(), instant: true, maxZoom: 0.86 },
  );
  syncAll();
}

void boot();
