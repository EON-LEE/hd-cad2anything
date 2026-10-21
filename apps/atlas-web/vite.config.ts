/// <reference types="vitest/config" />
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const root = fileURLToPath(new URL('.', import.meta.url))
// Backend (FastAPI atlas-api) used by the dev/preview proxy. Override with ATLAS_API_URL if needed.
const apiTarget = process.env.ATLAS_API_URL ?? 'http://localhost:8000'
const proxy = { '/api': { target: apiTarget, changeOrigin: true } }

export default defineConfig(({ mode }) => ({
  root,
  base: '/',
  plugins: [react()],
  // `vite --mode mock` forces the built-in fixtures without needing an (ignored) .env.mock file.
  define: mode === 'mock' ? { 'import.meta.env.VITE_MOCK': JSON.stringify('1') } : {},
  server: { proxy },
  preview: { proxy },
  build: {
    outDir: fileURLToPath(new URL('./dist', import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 2500,
  },
  test: {
    // Pure logic tests run in Node; component tests opt into jsdom with `// @vitest-environment jsdom`.
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
}))
