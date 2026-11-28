import {defineConfig} from 'vite';
import preact from '@preact/preset-vite';

// ui/dist/ is committed and served as-is by the plugin, so the build must be
// byte-stable: fixed file names, no source maps, no license comments (their
// URLs would trip the static guard against external addresses).
export default defineConfig({
  base: './',
  plugins: [preact()],
  esbuild: {legalComments: 'none'},
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    modulePreload: false,
    reportCompressedSize: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
