import { expect, test } from '@playwright/test'
import { mockApi } from './fixtures'

test('console selects prior runs and resets after retained output is evicted', async ({
  page,
}) => {
  const old = 'a'.repeat(32)
  let reads = 0
  await mockApi(page, {
    'GET /api/launcher/log-runs': () => ({
      items: [
        {
          run_id: old,
          config_name: 'Previous model',
          started_at: 1000,
          ended_at: 1100,
          state: 'exited',
          stop_reason: 'unexpected_exit',
          exit_code: 7,
          gap: true,
          logging_error: null,
          startup_truncated: false,
        },
      ],
      next_cursor: null,
    }),
    'GET /api/launcher/console': (request) => {
      const url = new URL(request.url())
      const historical = url.searchParams.get('run_id') === old
      const first = !url.searchParams.get('cursor')
      if (historical) reads += 1
      return {
        available: true,
        content: historical
          ? first
            ? 'prior loader error\n'
            : reads === 2
              ? 'retained tail\n'
              : ''
          : 'current output\n',
        offset: reads + 100,
        size: 200,
        source: 'filtered',
        path: '/data/logs',
        cursor: 'next',
        reset: historical && reads === 2,
        gap: historical,
      }
    },
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  await expect(page.locator('#console-out')).toContainText('current output')
  await page.getByLabel('Model run').selectOption(old)
  await expect(page.locator('#console-out')).toContainText('prior loader error')
  await expect(page.getByRole('status')).toContainText('no longer retained')
  await expect(page.locator('#console-out')).toContainText('retained tail')
  await expect(page.locator('#console-out')).not.toContainText(
    'prior loader error',
  )
  await expect(page.getByLabel('Model run')).toContainText('exit 7')
})

test('console bounds accumulated text and displays capture errors', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/console': () => ({
      available: true,
      content: 'x'.repeat(600 * 1024) + '\nfinal warning\n',
      offset: 700000,
      source: 'filtered',
      path: '/data/logs',
      cursor: 'next',
      error: 'Disk is full',
    }),
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  await expect(page.locator('#console-out')).toContainText('final warning')
  await expect(page.locator('#console-out')).toContainText(
    'Earlier output omitted',
  )
  expect(
    (await page.locator('#console-out').textContent())!.length,
  ).toBeLessThan(525000)
  await expect(page.getByRole('status')).toContainText('Disk is full')
})
