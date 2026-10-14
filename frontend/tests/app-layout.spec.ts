import { expect, test } from '@playwright/test'
import {
  gamingState,
  historyPage,
  launcherState,
  mockApi,
  startupState,
} from './fixtures'

test('layout is shared across views, preserves drafts and remembers the existing preference', async ({
  page,
}) => {
  await mockApi(page)
  await page.addInitScript(() => {
    if (!localStorage.getItem('llama-monitor:monitor-layout'))
      localStorage.setItem('llama-monitor:monitor-layout', 'comfortable')
  })
  await page.goto('/#manage')
  const control = page.getByRole('group', { name: 'App layout' })
  await expect(
    control.getByRole('button', { name: 'Comfortable' }),
  ).toHaveAttribute('aria-pressed', 'true')
  await page.locator('#gaming-user').fill('Draft username')
  await control.getByRole('button', { name: 'Fit', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.app-fit')).toBeVisible()
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await page.locator('#lx-name').fill('Draft configuration')
  await page.locator('#history-open').click()
  await page.locator('#history-from').fill('2026-01-01')
  await control.getByRole('button', { name: 'Comfortable' }).click()
  await expect(page.locator('#history-from')).toHaveValue('2026-01-01')
  await page.locator('#manage-open').click()
  await expect(page.locator('#gaming-user')).toHaveValue('Draft username')
  await expect(
    control.getByRole('button', { name: 'Comfortable' }),
  ).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await expect(page.locator('#lx-name')).toHaveValue('Draft configuration')
  await control.getByRole('button', { name: 'Fit', exact: true }).click()
  for (const view of ['manage', 'history', 'monitor']) {
    await page.goto(`/#${view}`)
    await expect(
      control.getByRole('button', { name: 'Fit', exact: true }),
    ).toHaveAttribute('aria-pressed', 'true')
  }
  await page.reload()
  await expect(page.locator('.app-fit')).toBeVisible()
})

test('Fit keeps all desktop landing views within the viewport', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/gaming/state': () => ({
      ...gamingState,
      credentials_saved: true,
      certificate_sha256: 'a'.repeat(64),
      auxiliary: { executable: 'llama-server', port: 8081 },
    }),
    'GET /api/startup/state': () => ({
      ...startupState,
      installed: true,
      task_name: 'llama-monitor',
      models: [
        { name: 'Primary model', port: 8001 },
        { name: 'Embedding model', port: 8081 },
      ],
    }),
  })
  for (const [width, height] of [
    [1440, 900],
    [1920, 1080],
    [2560, 1440],
    [3840, 2160],
  ]) {
    await page.setViewportSize({ width, height })
    for (const view of ['monitor', 'manage', 'history']) {
      await page.goto(`/#${view}`)
      await expect(
        page.locator(
          view === 'monitor'
            ? '#s-model'
            : view === 'manage'
              ? '#startup-saved-models'
              : '#history-body',
        ),
      ).toContainText(
        view === 'monitor'
          ? 'Qwen test'
          : view === 'manage'
            ? 'Embedding model'
            : 'Qwen test',
      )
      await expect
        .poll(() => page.evaluate(() => document.documentElement.scrollHeight))
        .toBeLessThanOrEqual(height)
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width + 1)
      if (view === 'manage') {
        const geometry = await page.evaluate(() => {
          const gaming = document
            .querySelector('#gaming-panel')!
            .getBoundingClientRect()
          const binary = document
            .querySelector('#binary-panel')!
            .getBoundingClientRect()
          const startup = document
            .querySelector('#startup-panel')!
            .getBoundingClientRect()
          return {
            alongside: gaming.right <= binary.left,
            below: binary.bottom <= startup.top,
          }
        })
        expect(geometry).toEqual({ alongside: true, below: true })
      }
      if (process.env.CAPTURE_UI === '1')
        await page.screenshot({
          path: `test-results/app-${view}-fit-${width}.png`,
        })
    }
  }
})

for (const mode of ['Fit', 'Comfortable']) {
  for (const width of [1440, 390]) {
    test(`History scrolls 50 records internally in ${mode} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 })
      await mockApi(page, {
        'GET /api/history': () => ({
          ...historyPage,
          items: Array.from({ length: 50 }, (_, i) => ({
            ...historyPage.items[0],
            id: `h${i + 1}`,
            model: `Record ${i + 1}`,
          })),
          next_cursor: 'next-page',
        }),
        'GET /api/history/h50/prompt': () => ({
          prompt_text: 'Final record prompt',
        }),
      })
      await page.goto('/#history')
      await page.getByRole('button', { name: mode, exact: true }).click()
      const records = page.getByRole('region', { name: 'History records' })
      const rows =
        width === 390
          ? page.locator('#history-mobile article')
          : page.locator('#history-body tr')
      await expect(rows).toHaveCount(50)
      await expect
        .poll(() => records.evaluate((el) => el.scrollHeight > el.clientHeight))
        .toBe(true)
      await records.focus()
      await page.keyboard.press('Control+End')
      await expect
        .poll(() => records.evaluate((el) => el.scrollTop))
        .toBeGreaterThan(0)
      if (width === 1440) {
        const top = await records.evaluate(
          (el) => el.getBoundingClientRect().top,
        )
        expect(
          await page
            .locator('.history-table th')
            .first()
            .evaluate((el) => el.getBoundingClientRect().top),
        ).toBeCloseTo(top + 1, 0)
        await expect(page.locator('#history-next')).toBeInViewport()
        await expect(page.locator('#history-from')).toBeInViewport()
      }
      await rows.last().getByRole('button', { name: 'View prompt' }).click()
      await expect(page.locator('#history-prompt-text')).toHaveText(
        'Final record prompt',
      )
      await page.getByRole('button', { name: 'Close', exact: true }).click()
      await page.locator('#history-next').scrollIntoViewIfNeeded()
      await expect(page.locator('#history-next')).toBeInViewport()
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width + 1)
      if (process.env.CAPTURE_UI === '1')
        await page.screenshot({
          path: `test-results/history-records-${mode}-${width}.png`,
          fullPage: true,
        })
    })
  }
}

test('small windows and expanded settings use natural page scrolling without clipping', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      binary_valid: false,
      settings: {
        ...launcherState.settings,
        llama_server_path: `C:/${'long-directory/'.repeat(20)}llama-server.exe`,
      },
    }),
    'GET /api/gaming/state': () => ({
      ...gamingState,
      integration_error: 'Connection error with a long description. '.repeat(
        12,
      ),
    }),
    'GET /api/startup/state': () => ({
      ...startupState,
      last_error: 'Startup error with additional details. '.repeat(12),
    }),
  })
  for (const [width, height] of [
    [1440, 600],
    [820, 1180],
    [390, 844],
    [320, 700],
  ]) {
    await page.setViewportSize({ width, height })
    for (const mode of ['Fit', 'Comfortable']) {
      await page.goto('/#manage')
      await page.getByRole('button', { name: mode, exact: true }).click()
      await page
        .getByText('Apollo installation commands', { exact: true })
        .click()
      await page.locator('#startup-error').scrollIntoViewIfNeeded()
      await expect(page.locator('#startup-error')).toBeInViewport()
      expect(
        await page.evaluate(() => document.documentElement.scrollHeight),
      ).toBeGreaterThan(height)
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width + 1)
      await page.goto('/#history')
      await expect(page.locator('#history-body')).toContainText('Qwen test')
      await page.locator('#history-next').scrollIntoViewIfNeeded()
      await expect(page.locator('#history-next')).toBeInViewport()
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width + 1)
      if (process.env.CAPTURE_UI === '1')
        await page.screenshot({
          path: `test-results/history-small-${mode}-${width}.png`,
          fullPage: true,
        })
    }
  }
})
