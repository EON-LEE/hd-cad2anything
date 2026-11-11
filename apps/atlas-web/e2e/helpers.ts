import { expect, type Page } from '@playwright/test'

/** Navigate to the mock app and wait for the shell to render. */
export async function gotoApp(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'CAD2Anything Atlas' })).toBeVisible()
}

/** On the stacked phone layout the sidebar (chat + composer) is behind the 대화·패널 view; no-op on desktop. */
export async function showConversation(page: Page) {
  const toggle = page.getByRole('button', { name: '대화·패널' })
  if (await toggle.isVisible().catch(() => false)) {
    await toggle.click()
  }
}

/** Click a quick-prompt chip from the empty-chat suggestions (switches to the conversation view first). */
export async function clickQuickPrompt(page: Page, name: string) {
  await showConversation(page)
  await page.getByRole('group', { name: '추천 질문' }).getByRole('button', { name }).first().click()
}

/** The chat composer textbox (same accessible name used across the app). */
export function composer(page: Page) {
  return page.getByRole('textbox', { name: '에이전트에게 질문' })
}

/** Send a message through the composer. */
export async function ask(page: Page, text: string) {
  await showConversation(page)
  const input = composer(page)
  await input.click()
  await input.fill(text)
  await input.press('Enter')
}

/** Assert the page itself does not scroll horizontally (allowing 1px rounding). */
export async function expectNoHorizontalOverflow(page: Page) {
  const m = await page.evaluate(() => ({
    docScroll: document.documentElement.scrollWidth,
    docClient: document.documentElement.clientWidth,
    bodyScroll: document.body.scrollWidth,
    inner: window.innerWidth,
  }))
  expect(m.docScroll, `documentElement scrollWidth ${m.docScroll} > clientWidth ${m.docClient}`).toBeLessThanOrEqual(m.docClient + 1)
  expect(m.bodyScroll, `body scrollWidth ${m.bodyScroll} > innerWidth ${m.inner}`).toBeLessThanOrEqual(m.inner + 1)
}
