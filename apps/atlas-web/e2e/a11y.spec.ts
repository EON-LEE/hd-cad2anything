import { expect, test } from '@playwright/test'
import { composer, gotoApp } from './helpers'

// Accessibility specs run once, on the desktop project (viewport-independent behaviour).
test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'runs once on desktop')
})

test.describe('accessibility', () => {
  // Spec 5: the map is pointer-only, so a keyboard user selects a unit through the combobox.
  test('keyboard-only unit selection opens the details card (R3)', async ({ page }) => {
    await gotoApp(page)
    const combo = page.getByRole('combobox', { name: '매장 찾기' })
    await expect(combo).toBeVisible()
    await combo.focus()
    await combo.fill('3F-C01')
    await expect(page.getByRole('option', { name: /3F-C01/ }).first()).toBeVisible()
    await page.keyboard.press('Enter')
    // The same selection a map click makes: the unit details card opens.
    await expect(page.getByRole('complementary', { name: /3F-C01.*매장 상세/ })).toBeVisible()
  })

  // Spec 6: role/name checks for the floor tab/tabpanel pairing and the composer.
  test('floor tabs control the canvas tabpanel and the composer is a labelled textbox (R4)', async ({ page }) => {
    await gotoApp(page)
    const tablist = page.getByRole('tablist', { name: '층' })
    await expect(tablist).toBeVisible()
    const tabs = tablist.getByRole('tab')
    await expect(tabs.first()).toBeVisible()
    for (const tab of await tabs.all()) {
      await expect(tab).toHaveAttribute('aria-controls', 'floor-canvas')
    }
    const panel = page.locator('#floor-canvas')
    await expect(panel).toHaveAttribute('role', 'tabpanel')
    const selected = tablist.getByRole('tab', { selected: true })
    const selectedId = await selected.getAttribute('id')
    expect(selectedId).toBeTruthy()
    await expect(panel).toHaveAttribute('aria-labelledby', selectedId!)

    // The side panel is a proper tablist, and the composer is a labelled textbox with a send button.
    await expect(page.getByRole('tablist', { name: '패널' })).toBeVisible()
    await expect(composer(page)).toBeVisible()
    await expect(page.getByRole('button', { name: '보내기' })).toBeVisible()
  })

  // Spec 6 (R4): the floor tabs are a roving-tabindex tablist whose arrow keys wrap at both ends.
  test('floor tab arrow keys wrap at the ends (R4)', async ({ page }) => {
    await gotoApp(page)
    const tabs = await page.getByRole('tablist', { name: '층' }).getByRole('tab').all()
    expect(tabs.length).toBeGreaterThanOrEqual(2)
    await tabs[0].focus()
    await page.keyboard.press('ArrowLeft')
    await expect(tabs[tabs.length - 1]).toBeFocused()
    await page.keyboard.press('ArrowRight')
    await expect(tabs[0]).toBeFocused()
  })
})
