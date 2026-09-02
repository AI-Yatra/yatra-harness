// The canvas engine: pan, zoom to the cursor, pinch, and an eased fly-to.

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface FlyOpts {
  /** Chrome that covers the viewport, so a target is not flown in behind it. */
  padRight?: number;
  padTop?: number;
  padBottom?: number;
  maxZoom?: number;
  instant?: boolean;
}

const MIN_ZOOM = 0.06;
const MAX_ZOOM = 2.4;

export function el<T extends HTMLElement = HTMLDivElement>(
  tag: string,
  cls = '',
  style = '',
  text = '',
): T {
  const node = document.createElement(tag) as T;
  if (cls) node.className = cls;
  if (style) node.style.cssText = style;
  if (text) node.textContent = text;
  return node;
}

export function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number> = {},
): SVGElementTagNameMap[K] {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

export class World {
  board: HTMLDivElement;
  world: HTMLDivElement;
  pan = { x: 0, y: 0 };
  zoom = 0.42;
  size: { w: number; h: number };
  /** True while a drag has actually moved, so the click it ends in is swallowed. */
  moved = false;
  onChange: (() => void) | null = null;

  private drag: { mx: number; my: number; px: number; py: number } | null = null;
  private anim: { x: number; y: number; z: number } | null = null;
  private raf = 0;

  constructor(parent: HTMLElement, size: { w: number; h: number }) {
    this.size = size;
    this.board = el('div', 'board');
    this.world = el('div', 'world', `width:${size.w}px;height:${size.h}px`);
    this.board.appendChild(this.world);
    this.board.style.touchAction = 'none';
    parent.appendChild(this.board);

    this.board.addEventListener(
      'click',
      (e) => {
        if (this.moved) {
          e.stopPropagation();
          e.preventDefault();
        }
      },
      true,
    );

    this.bindMouse();
    this.bindTouch();
    window.addEventListener('resize', () => this.apply());
  }

  private bindMouse(): void {
    this.board.addEventListener('mousedown', (e) => {
      if ((e.target as HTMLElement).closest('.no-pan')) return;
      this.drag = { mx: e.clientX, my: e.clientY, px: this.pan.x, py: this.pan.y };
      this.moved = false;
      this.anim = null;
      this.board.classList.add('dragging');
    });
    window.addEventListener('mousemove', (e) => {
      if (!this.drag) return;
      const dx = e.clientX - this.drag.mx;
      const dy = e.clientY - this.drag.my;
      if (Math.abs(dx) + Math.abs(dy) > 4) this.moved = true;
      this.pan.x = this.drag.px + dx;
      this.pan.y = this.drag.py + dy;
      this.apply();
    });
    window.addEventListener('mouseup', () => {
      if (this.drag) {
        this.drag = null;
        this.board.classList.remove('dragging');
      }
      setTimeout(() => {
        this.moved = false;
      }, 0);
    });
    this.board.addEventListener(
      'wheel',
      (e) => {
        if ((e.target as HTMLElement).closest('.scrolls')) return;
        e.preventDefault();
        this.anim = null;
        const r = this.board.getBoundingClientRect();
        const mx = e.clientX - r.left;
        const my = e.clientY - r.top;
        const factor = Math.exp(-e.deltaY * 0.0016);
        this.zoomAt(mx, my, this.zoom * factor);
      },
      { passive: false },
    );
  }

  private bindTouch(): void {
    type TS = {
      mode: 'pan' | 'zoom';
      sx: number;
      sy: number;
      px: number;
      py: number;
      dist: number;
      midX: number;
      midY: number;
      zoom0: number;
    };
    let ts: TS | null = null;
    const spread = (e: TouchEvent) =>
      Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY,
      );
    const middle = (e: TouchEvent) => {
      const r = this.board.getBoundingClientRect();
      return {
        x: (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left,
        y: (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top,
      };
    };
    const asPan = (e: TouchEvent): TS => ({
      mode: 'pan',
      sx: e.touches[0].clientX,
      sy: e.touches[0].clientY,
      px: this.pan.x,
      py: this.pan.y,
      dist: 0,
      midX: 0,
      midY: 0,
      zoom0: this.zoom,
    });
    const asZoom = (e: TouchEvent): TS => {
      const m = middle(e);
      return {
        mode: 'zoom',
        sx: 0,
        sy: 0,
        px: this.pan.x,
        py: this.pan.y,
        dist: spread(e),
        midX: m.x,
        midY: m.y,
        zoom0: this.zoom,
      };
    };

    this.board.addEventListener(
      'touchstart',
      (e) => {
        this.anim = null;
        if (e.touches.length === 1) {
          ts = asPan(e);
          this.moved = false;
        } else if (e.touches.length >= 2) {
          ts = asZoom(e);
          this.moved = true;
        }
      },
      { passive: true },
    );
    this.board.addEventListener(
      'touchmove',
      (e) => {
        if (!ts) return;
        if ((e.target as HTMLElement).closest('.scrolls')) return;
        e.preventDefault();
        if (ts.mode === 'pan' && e.touches.length === 1) {
          const dx = e.touches[0].clientX - ts.sx;
          const dy = e.touches[0].clientY - ts.sy;
          if (Math.abs(dx) + Math.abs(dy) > 8) this.moved = true;
          this.pan.x = ts.px + dx;
          this.pan.y = ts.py + dy;
          this.apply();
        } else if (e.touches.length >= 2) {
          if (ts.mode !== 'zoom') ts = asZoom(e);
          const m = middle(e);
          const z = clamp(ts.zoom0 * (spread(e) / (ts.dist || 1)));
          this.pan.x = m.x - (ts.midX - ts.px) * (z / ts.zoom0);
          this.pan.y = m.y - (ts.midY - ts.py) * (z / ts.zoom0);
          this.zoom = z;
          this.moved = true;
          this.apply();
        }
      },
      { passive: false },
    );
    this.board.addEventListener('touchend', (e) => {
      if (e.touches.length === 0) {
        ts = null;
        setTimeout(() => {
          this.moved = false;
        }, 0);
      } else if (e.touches.length === 1) {
        ts = asPan(e);
      }
    });
  }

  zoomAt(mx: number, my: number, next: number): void {
    const z = clamp(next);
    this.pan.x = mx - (mx - this.pan.x) * (z / this.zoom);
    this.pan.y = my - (my - this.pan.y) * (z / this.zoom);
    this.zoom = z;
    this.apply();
  }

  /** Zoom about the middle of the viewport, for the +/- buttons and keys. */
  nudgeZoom(factor: number): void {
    const r = this.board.getBoundingClientRect();
    this.anim = null;
    this.zoomAt(r.width / 2, r.height / 2, this.zoom * factor);
  }

  viewport(): Rect {
    const r = this.board.getBoundingClientRect();
    return {
      x: -this.pan.x / this.zoom,
      y: -this.pan.y / this.zoom,
      w: r.width / this.zoom,
      h: r.height / this.zoom,
    };
  }

  flyTo(rect: Rect, opts: FlyOpts = {}): void {
    const r = this.board.getBoundingClientRect();
    const padRight = opts.padRight ?? 0;
    const padTop = opts.padTop ?? 0;
    const padBottom = opts.padBottom ?? 0;
    const availW = Math.max(120, r.width - padRight);
    const availH = Math.max(120, r.height - padTop - padBottom);
    const z = clamp(
      Math.min(availW / Math.max(1, rect.w), availH / Math.max(1, rect.h)),
      MIN_ZOOM,
      opts.maxZoom ?? MAX_ZOOM,
    );
    const target = {
      x: -(rect.x + rect.w / 2) * z + availW / 2,
      y: -(rect.y + rect.h / 2) * z + padTop + availH / 2,
      z,
    };
    if (opts.instant) {
      this.pan.x = target.x;
      this.pan.y = target.y;
      this.zoom = target.z;
      this.apply();
      return;
    }
    this.anim = target;
    this.tick();
  }

  private tick(): void {
    if (this.raf) return;
    const step = () => {
      this.raf = 0;
      if (!this.anim) return;
      const { x, y, z } = this.anim;
      const dx = x - this.pan.x;
      const dy = y - this.pan.y;
      const dz = z - this.zoom;
      if (Math.abs(dx) < 0.6 && Math.abs(dy) < 0.6 && Math.abs(dz) < 0.0015) {
        this.pan.x = x;
        this.pan.y = y;
        this.zoom = z;
        this.anim = null;
        this.apply();
        return;
      }
      this.pan.x += dx * 0.18;
      this.pan.y += dy * 0.18;
      this.zoom += dz * 0.18;
      this.apply();
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  apply(): void {
    this.world.style.transform = `translate3d(${this.pan.x}px,${this.pan.y}px,0) scale(${this.zoom})`;
    // Detail below a legible size is hidden rather than drawn as fuzz.
    this.board.dataset.detail = this.zoom < 0.22 ? 'far' : this.zoom < 0.5 ? 'mid' : 'near';
    this.onChange?.();
  }
}

function clamp(z: number, lo = MIN_ZOOM, hi = MAX_ZOOM): number {
  return Math.max(lo, Math.min(hi, z));
}
