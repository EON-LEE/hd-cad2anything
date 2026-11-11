import { expect, test } from '@playwright/test'
import { ask, gotoApp } from './helpers'

/**
 * These two specs exercise the chat/panel data flow that WS6a owns (chat scroll restore and keeping a
 * deferred layout result in the store). They are written against the agreed test ids and behaviour; if
 * WS6a's fixes are not merged yet they will fail, which is reported honestly rather than worked around.
 */
test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'runs once on desktop')
})

test.describe('chat data flow (depends on WS6a)', () => {
  // Spec 1: after switching to another side tab and back, the latest chat message is still reachable.
  test('latest chat message stays visible after switching side tabs and back (spec 1)', async ({ page }) => {
    await gotoApp(page)
    await ask(page, 'S/S 개편 도면에서 뭐가 바뀌었는지 알려줘')
    await expect(page.locator('.answer').first()).toBeVisible({ timeout: 30_000 })

    await page.getByRole('tab', { name: '관계 그래프' }).click()
    await page.getByRole('tab', { name: '에이전트' }).click()

    const log = page.locator('[data-testid="chat-log"]')
    await expect(log).toBeVisible()
    const atBottom = await log.evaluate((el) => Math.abs(el.scrollHeight - el.scrollTop - el.clientHeight) <= 4)
    const jumpVisible = await page.locator('[data-testid="jump-latest"]').isVisible().catch(() => false)
    expect(atBottom || jumpVisible, 'latest message is visible (scrolled to bottom) or a jump-to-latest button is offered').toBeTruthy()
  })

  // Spec 2: a deferred layout result survives a later, unrelated answer and can still be opened.
  test('a deferred layout result survives another answer (spec 2)', async ({ page }) => {
    await gotoApp(page)
    await ask(page, '4층 재배치안 3개 만들어 비교해줘')
    // The result is deferred while the conversation stays visible (a notice / tab mark appears).
    await expect(page.locator('.result-notice, .side-tabs .result-mark').first()).toBeVisible({ timeout: 40_000 })
    await expect(page.getByText('답변을 마쳤어요.')).toBeVisible({ timeout: 40_000 })

    await ask(page, 'VIP 고객 한 명의 쇼핑 동선을 보여줘')
    await expect(page.locator('.journey-card')).toBeVisible({ timeout: 30_000 })

    await page.getByRole('tab', { name: /레이아웃 대안|배치 대안/ }).click()
    await expect(page.getByRole('heading', { name: /ALT-/ }).first()).toBeVisible({ timeout: 15_000 })
  })
})
