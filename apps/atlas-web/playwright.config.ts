import { defineConfig } from '@playwright/test'

/**
 * Browser e2e for the Atlas web app (R5). Runs against the built-in mock data (`vite --mode mock`),
 * so no backend, Azure or Foundry is needed. Covers the chat/panel data flow (WS6a) and the
 * responsive layout / accessibility work (WS6b) that jsdom + mocked deck.gl cannot exercise.
 */
const PORT = 5199
const isCI = !!process.env.CI

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  workers: isCI ? 1 : undefined,
  reporter: isCI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: 'on-first-retry',
  },
  // Mock mode means the app starts from fixtures with no backend (see src/api/source.ts).
  webServer: {
    command: `npm run dev:mock -- --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !isCI,
    timeout: 120_000,
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 } } },
    { name: 'narrow', use: { viewport: { width: 360, height: 740 } } },
  ],
})
