import { expect, test } from '@playwright/test'
import { config, launcherState, mockApi, stats } from './fixtures'

test('Monitor owns the daily controls and keeps drafts across navigation', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/')
  await expect(page.locator('#lx-launch')).toBeVisible()
  await expect(page.locator('#lx-config')).toContainText(config.name)
  const geometry = await page.evaluate(() => ({
    hero: document.querySelector('.monitor-hero')!.getBoundingClientRect()
      .bottom,
    configuration: document
      .querySelector('#configuration-panel')!
      .getBoundingClientRect().top,
  }))
  expect(geometry.configuration).toBeGreaterThan(geometry.hero)
  await page.locator('#lx-name').fill('Draft kept across views')
  await page.locator('#manage-open').click()
  await expect(page.locator('#lx-bin-browse')).toBeVisible()
  await expect(page.locator('#gaming-panel')).toBeVisible()
  await expect(page.locator('#startup-panel')).toBeVisible()
  await expect(page.locator('#lx-launch')).toBeHidden()
  await expect(page.locator('#lx-config')).toBeHidden()
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await expect(page.locator('#lx-name')).toHaveValue('Draft kept across views')
  await expect(page.locator('#lx-config')).toHaveCount(1)
  await expect(page.locator('#lx-launch')).toHaveCount(1)
})

test('Flags ignores the old preference and expansion grows the page naturally', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const flags = Array.from({ length: 18 }, (_, i) => ({
    flag: `--custom-${i}`,
    value: 'value',
  }))
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      configs: [{ ...config, flags }],
    }),
  })
  await page.addInitScript(() =>
    localStorage.setItem('lx-flags-collapsed', '0'),
  )
  await page.goto('/')
  await expect(page.locator('#lx-flags-label')).toHaveAttribute(
    'aria-expanded',
    'false',
  )
  await expect(page.locator('#lx-flags-summary')).toContainText('+16')
  const collapsedHeight = await page
    .locator('#configuration-panel')
    .evaluate((el) => el.getBoundingClientRect().height)
  const telemetryHeight = await page
    .locator('.monitor-telemetry')
    .evaluate((el) => el.getBoundingClientRect().height)
  await page.locator('#lx-flags-label').click()
  await expect(page.locator('.lx-flag-row')).toHaveCount(18)
  expect(
    await page
      .locator('.monitor-telemetry')
      .evaluate((el) => el.getBoundingClientRect().height),
  ).toBe(telemetryHeight)
  const expanded = await page
    .locator('#configuration-panel')
    .evaluate((el) => ({
      height: el.getBoundingClientRect().height,
      overflow: getComputedStyle(el).overflowY,
      pageHeight: document.documentElement.scrollHeight,
      viewportHeight: innerHeight,
    }))
  expect(expanded.height).toBeGreaterThan(collapsedHeight)
  expect(expanded.overflow).toBe('visible')
  expect(expanded.pageHeight).toBeGreaterThan(expanded.viewportHeight)
  await page.locator('#lx-save').scrollIntoViewIfNeeded()
  await expect(page.locator('#lx-save')).toBeInViewport()
  await page.evaluate(() => window.scrollTo(0, 0))
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/monitor-expanded.png',
      fullPage: true,
    })
  await page.reload()
  await expect(page.locator('#lx-flags-label')).toHaveAttribute(
    'aria-expanded',
    'false',
  )
})

test('invalid binary links to executable setup while launch remains disabled', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      binary_valid: false,
    }),
  })
  await page.goto('/')
  await expect(page.locator('#lx-launch')).toBeDisabled()
  await page
    .locator('.launch-controls')
    .getByRole('link', { name: 'Manage', exact: true })
    .click()
  await expect(page).toHaveURL(/#manage$/)
  await expect(page.locator('#lx-bin-msg')).toBeVisible()
  await expect(page.locator('#lx-bin-browse')).toBeVisible()
})

test('long names and flag previews remain accessible without horizontal overflow', async ({
  page,
}) => {
  const name = 'A-very-long-model-and-configuration-name-'.repeat(4)
  const flags = Array.from({ length: 12 }, (_, i) => ({
    flag: `--long-option-${i}`,
    value: 'long-value-'.repeat(15),
  }))
  const saved = { ...config, name, flags }
  await mockApi(page, {
    'GET /api/stats': () => ({ ...stats, model: { ...stats.model, name } }),
    'GET /api/launcher/state': () => ({
      ...launcherState,
      configs: [saved],
      settings: { ...launcherState.settings, default_config: name },
    }),
  })
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 390, height: 844 },
    { width: 320, height: 700 },
  ]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await expect(page.locator('#s-model')).toHaveText(name)
    await expect(page.locator('#lx-config')).toContainText(name)
    await expect(page.locator('#lx-flags-summary')).toContainText('+10')
    if (process.env.CAPTURE_UI === '1')
      await page.screenshot({
        path: `test-results/monitor-long-${viewport.width}.png`,
        fullPage: true,
      })

    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true)
    await page.locator('#lx-flags-label').click()
    await expect(page.locator('.lx-flag-row')).toHaveCount(12)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true)
  }
})
