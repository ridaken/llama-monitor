import { expect, test } from '@playwright/test'
import { historyPage, mockApi } from './fixtures'

const proxy = {
  enabled: false,
  log_prompts: true,
  log_responses: true,
  listening: false,
  host: '127.0.0.1',
  port: 8501,
  target: 'http://127.0.0.1:8001',
  listener_error: null as string | null,
  capture_error: null as string | null,
}
const record = {
  id: 'proxy-one',
  observed_at: 1700000000,
  endpoint: '/v1/chat/completions',
  target: 'http://127.0.0.1:8001',
  model: 'Test model',
  state: 'cancelled',
  status_code: 200,
  duration_seconds: 1.2,
  first_token_seconds: 0.1,
  prompt_logging: true,
  response_logging: true,
  response_truncated: true,
  request_text:
    '{"messages":[{"content":"<script>window.injected=true</script>"}]}',
  response_text:
    '{"choices":[{"content":"Answer","reasoning_content":"Reasoning","tool_calls":[]}]}',
  usage: null,
}

test('Manage proxy toggles persist and expose listener errors', async ({
  page,
}) => {
  let state = { ...proxy }
  await mockApi(page, {
    'GET /api/proxy/state': () => state,
    'POST /api/proxy/settings': (request) => {
      state = { ...state, ...request.postDataJSON() }
      state.listening = state.enabled
      return state
    },
  })
  await page.goto('/#manage')
  await page.getByLabel('Enable inference proxy', { exact: true }).click()
  await expect(
    page.getByLabel('Enable inference proxy', { exact: true }),
  ).toBeChecked()
  await page.getByLabel('Save model responses', { exact: true }).click()
  await expect(
    page.getByLabel('Save model responses', { exact: true }),
  ).not.toBeChecked()
  await page.reload()
  await expect(
    page.getByLabel('Save model responses', { exact: true }),
  ).not.toBeChecked()
  await expect(page.locator('#proxy-panel')).toContainText(':8501/v1')
  state = {
    ...state,
    listening: false,
    listener_error: 'Address already in use.',
  }
  await page.reload()
  await expect(page.locator('#proxy-panel').getByRole('alert')).toContainText(
    'Address already in use',
  )
})

test('Proxied history filters, pagination, safe details and confirmed clearing', async ({
  page,
}) => {
  let cleared = false
  const calls = await mockApi(page, {
    'GET /api/activity': (request) => {
      const cursor = new URL(request.url()).searchParams.get('cursor')
      return {
        items:
          cleared || cursor
            ? []
            : [
                {
                  ...record,
                  id: 'proxy:proxy-one',
                  source: 'proxy',
                  record_id: 'proxy-one',
                },
              ],
        models: ['Test model'],
        log_status: historyPage.log_status,
        next_cursor: cleared || cursor ? null : 'next-one',
      }
    },
    'GET /api/requests/proxy-one': () => record,
    'DELETE /api/requests': () => {
      cleared = true
      return { deleted: 1 }
    },
  })
  await page.goto('/#requests')
  await expect(
    page.getByRole('link', { name: 'History', exact: true }),
  ).toHaveAttribute('aria-current', 'page')
  await page.locator('#history-model').click()
  await page.getByRole('option', { name: 'Test model', exact: true }).click()
  await expect
    .poll(() => calls.some((call) => call.includes('model=Test+model')))
    .toBe(true)
  await page.getByRole('button', { name: 'View details', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('Saved output may be partial')
  await expect(dialog).toContainText('Truncated at the 1 MiB')
  await expect(dialog).toContainText('Reasoning')
  await expect(dialog).toContainText('<script>window.injected=true</script>')
  expect(await page.evaluate(() => (window as any).injected)).toBeUndefined()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect
    .poll(() => calls.some((call) => call.includes('cursor=next-one')))
    .toBe(true)
  await page.getByRole('button', { name: 'Previous', exact: true }).click()
  await page
    .getByRole('button', { name: 'Clear proxy history', exact: true })
    .click()
  expect(calls).not.toContain('DELETE /api/requests')
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Clear proxy history', exact: true })
    .click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect.poll(() => calls.includes('DELETE /api/requests')).toBe(true)
  await expect(
    page
      .locator('#history-body')
      .getByText('No history records', { exact: false }),
  ).toBeVisible()
})

test('Proxied history remains usable on a narrow viewport', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockApi(page, {
    'GET /api/activity': () => ({
      ...historyPage,
      items: [
        {
          ...record,
          source: 'proxy',
          record_id: 'proxy-one',
          id: 'proxy:proxy-one',
        },
      ],
    }),
  })
  await page.goto('/#requests')
  await expect(page.getByLabel('Endpoint', { exact: true })).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true)
})
