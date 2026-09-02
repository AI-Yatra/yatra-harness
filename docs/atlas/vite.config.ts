import { defineConfig } from 'vite';

// Relative base so a built atlas opens from any path, including a docs
// subdirectory on GitHub Pages or a plain file:// copy.
export default defineConfig({
  base: './',
  build: { outDir: 'dist', assetsDir: 'assets', target: 'es2022' },
});
