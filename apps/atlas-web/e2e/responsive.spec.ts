import { expect, test } from '@playwright/test'
import { clickQuickPrompt, expectNoHorizontalOverflow, gotoApp } from './helpers'

// Spec 3: no horizontal page overflow at the required widths (R1 stacked layout, R2 revision select).
test.describe('responsive layout', () => {
  test('no horizontal overflow at the project width and at 768px (spec 3)', async ({ page }) => {
    await gotoApp(page)
    await expectNoHorizontalOverflow(page)
    // 768px (tablet) is one of the widths the review called out; verify it from every project too.
    await page.setViewportSize({ width: 768, height: 1024 })
    await expectNoHorizontalOverflow(page)
    // Opening the revision <select> options must not widen the page either (R2).
    await page.setViewportSize({ width: 360, height: 740 })
    await expectNoHorizontalOverflow(page)
  })

  // Spec 4: the floating journey card must not blanket the small canvas (collapsed by default on a phone).
  test('journey card covers < 50% of the canvas at 390x844 (spec 4)', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'mobile', 'phone layout (390x844) only')
    await gotoApp(page)
    await clickQuickPrompt(page, 'VIP 고객 한 명의 쇼핑 동선을 보여줘')

    const card = page.locator('.journey-card')
    await expect(card).toBeVisible({ timeout: 20_000 })
    // Collapsed by default on a phone: the 자세히 expander is shown instead of the full timeline.
    await expect(card.getByRole('button', { name: '자세히' })).toBeVisible()

    const cardBox = await card.boundingBox()
    const canvasBox = await page.locator('#floor-canvas .canvas-stage').boundingBox()
    expect(cardBox).not.toBeNull()
    expect(canvasBox).not.toBeNull()
    const ratio = (cardBox!.width * cardBox!.height) / (canvasBox!.width * canvasBox!.height)
    expect(ratio, `journey card covered ${(ratio * 100).toFixed(1)}% of the canvas`).toBeLessThan(0.5)

    // Expanding shows the full card; collapsing returns to the one-line summary.
    await card.getByRole('button', { name: '자세히' }).click()
    await expect(card.getByRole('list', { name: '동선 요약' })).toBeVisible()
    await expect(card.getByRole('button', { name: '접기' })).toBeVisible()
  })
})
