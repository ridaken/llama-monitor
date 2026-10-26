import { expect, test } from '@playwright/test'
import { historyPage, mockApi } from './fixtures'

const proxied = {
  id: 'proxy:h1',
  record_id: 'h1',
  source: 'proxy',
  observed_at: historyPage.items[0].observed_at,
  model: 'Qwen test',
  endpoint: '/v1/chat/completions',
  state: 'complete',
  status_code: 200,
  total_seconds: 1.2,
  prompt_tokens: 80,
  generated_tokens: 12,
}

for (const width of [1440, 390]) {
  test(`combined history keeps both observations accessible at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    const calls = await mockApi(page, {
      'GET /api/activity': () => ({
        ...historyPage,
        items: [proxied, ...historyPage.items],
      }),
      'GET /api/requests/h1': () => ({
        ...proxied,
        id: 'h1',
        target: 'http://localhost:8001',
        request_text: 'Saved request',
        response_text: 'Saved answer',
        usage: null,
      }),
    })
    await page.goto('/#history')
    await expect(page.getByRole('navigation').getByRole('link')).toHaveCount(3)
    const rows =
      width === 390
        ? page.locator('#history-mobile article')
        : page.locator('#history-body tr')
    await expect(rows).toHaveCount(2)
    await expect(rows.first()).toContainText('Proxied')
    await expect(rows.last()).toContainText('Monitored')
    expect(calls.some((call) => call.startsWith('GET /api/requests/h1'))).toBe(
      false,
    )
    await page.screenshot({
      path: `test-results/unified-history-${width}.png`,
      fullPage: true,
    })
    await rows.first().getByRole('button').click()
    await expect(page.getByRole('dialog')).toContainText('Saved answer')
    await page.keyboard.press('Escape')
    await rows.last().getByRole('button').click()
    await expect(page.getByRole('dialog')).toContainText('Secret test prompt')
    await expect(page.getByRole('dialog')).toContainText('Generation duration')
  })
}

test('source navigation preserves filters, supports Back, and refresh resets paging', async ({
  page,
}) => {
  const calls = await mockApi(page, {
    'GET /api/activity': (request) => {
      const params = new URL(request.url()).searchParams
      return {
        ...historyPage,
        next_cursor: params.has('cursor') ? null : 'next-page',
      }
    },
  })
  await page.goto('/#history')
  await page.locator('#history-from').fill('2026-01-01')
  await page.locator('#history-source').click()
  await page.getByRole('option', { name: 'Proxied', exact: true }).click()
  await expect(page).toHaveURL(/#history\?source=proxy$/)
  await page.locator('#history-endpoint').fill('/completion')
  await page.getByRole('link', { name: 'Manage', exact: true }).click()
  await page.getByRole('link', { name: 'History', exact: true }).click()
  await expect(page.locator('#history-source')).toHaveText('Proxied')
  await expect(page.locator('#history-from')).toHaveValue('2026-01-01')
  await expect(page.locator('#history-endpoint')).toHaveValue('/completion')
  await page.locator('#history-next').click()
  await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Refresh', exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  await page.locator('#history-source').click()
  await page.getByRole('option', { name: 'Monitored', exact: true }).click()
  await expect(page.locator('#history-endpoint')).toHaveValue('')
  await expect(page.locator('#history-endpoint')).toBeDisabled()
  await page.goBack()
  await expect(page.locator('#history-source')).toHaveText('Proxied')
  await expect
    .poll(() => calls.some((call) => call.includes('source=monitored')))
    .toBe(true)
})

test('legacy Requests link redirects without an extra Back step', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/#manage')
  await page.evaluate(() => {
    location.hash = 'requests'
  })
  await expect(page).toHaveURL(/#history\?source=proxy$/)
  await expect(page.locator('#history-source')).toHaveText('Proxied')
  await page.goBack()
  await expect(page).toHaveURL(/#manage$/)
})

test('coverage and capture warnings coexist and missing details report an error', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/activity': () => ({
      ...historyPage,
      items: [proxied],
      log_status: { ...historyPage.log_status, gap: true },
      capture_error: 'Storage unavailable',
    }),
    'GET /api/requests/h1': () => ({
      status: 404,
      body: { error: 'Request unavailable' },
    }),
  })
  await page.goto('/#history')
  await expect(page.locator('#history-status')).toContainText('Coverage gap')
  await expect(page.getByRole('alert')).toContainText('Storage unavailable')
  await page.locator('#history-body').getByRole('button').click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(
    'Request unavailable',
  )
})

test('clearing monitored records preserves proxy records despite active filters', async ({
  page,
}) => {
  let cleared = false
  const calls = await mockApi(page, {
    'GET /api/activity': () => ({
      ...historyPage,
      items: cleared ? [proxied] : [proxied, ...historyPage.items],
    }),
    'DELETE /api/history': () => {
      cleared = true
      return { deleted: 1 }
    },
  })
  await page.goto('/#history?source=proxy')
  await page.locator('#history-clear').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('regardless of the current filters')
  expect(calls).not.toContain('DELETE /api/history')
  await dialog
    .getByRole('button', { name: 'Clear monitored history', exact: true })
    .click()
  await expect(page.locator('#history-body tr')).toHaveCount(1)
  await expect(page.locator('#history-body')).toContainText('Proxied')
  expect(calls).not.toContain('DELETE /api/requests')
})
