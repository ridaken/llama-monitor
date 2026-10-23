import { expect, test } from '@playwright/test'
import { launcherState, mockApi } from './fixtures'

test('a crashed server shows evidence and recovery without restarting anything', async ({
  page,
}) => {
  let probes = 0
  const calls = await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      status: {
        state: 'exited',
        exit_code: -11,
        started_at: 123,
        config_name: 'Qwen test',
      },
    }),
    'GET /api/launcher/doctor': () => {
      probes += 1
      return {
        healthy: false,
        summary: 'Diagnostic checks failed.',
        checks: [
          {
            name: 'nvidia',
            state: 'fail',
            message: 'Process exited with code 1.',
            output: 'No adapters were found',
          },
        ],
        recovery: [
          'Stop Docker Desktop, run wsl --shutdown, then start Docker Desktop.',
          'This interrupts all Docker containers and active WSL sessions.',
        ],
      }
    },
  })
  await page.goto('/')
  await expect(page.locator('#lx-status')).toContainText('Exited (code -11)')
  await expect(page.locator('#lx-diagnostics')).toContainText(
    'No adapters were found',
  )
  await expect(page.locator('#lx-diagnostics')).toContainText(
    'all Docker containers',
  )
  if (process.env.CAPTURE_UI === '1') {
    await page.screenshot({
      path: 'test-results/startup-diagnostics.png',
      fullPage: true,
    })
    await page.setViewportSize({ width: 390, height: 844 })
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(391)
    await page.screenshot({
      path: 'test-results/startup-diagnostics-mobile.png',
      fullPage: true,
    })
  }
  // State polling must not spawn another diagnostic on every refresh.
  await page.waitForTimeout(2500)
  expect(probes).toBe(1)
  expect(calls.filter((call) => call.startsWith('POST '))).toEqual([])
  await page.locator('#lx-doctor').click()
  await expect.poll(() => probes).toBe(2)
})

test('manual diagnostics report skipped NVIDIA checks without GPU recovery advice', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/doctor': () => ({
      healthy: true,
      summary:
        'Diagnostic checks passed. This does not verify model compatibility or inference.',
      checks: [
        {
          name: 'nvidia',
          state: 'skipped',
          message: 'NVIDIA access was not verified.',
          output: '',
        },
      ],
      recovery: [],
    }),
  })
  await page.goto('/')
  await page.locator('#lx-doctor').click()
  await expect(page.locator('#lx-diagnostics')).toContainText('skipped')
  await expect(page.locator('#lx-diagnostics')).not.toContainText(
    'Recovery guidance',
  )
})

test('console replaces startup fallback when the managed log appears at a larger offset', async ({
  page,
}) => {
  let reads = 0
  await mockApi(page, {
    'GET /api/launcher/console': () => {
      reads += 1
      return reads === 1
        ? {
            available: true,
            content: 'early output',
            offset: 12,
            path: '/data/llama-server.log.startup',
            source: 'startup',
          }
        : {
            available: true,
            content: 'structured server log is now available\n',
            offset: 38,
            path: '/data/llama-server.log',
            source: 'managed',
          }
    },
  })
  await page.goto('/')
  await page.locator('#lx-console').click()
  await expect(page.locator('#console-out')).toContainText('early output')
  await expect(page.locator('#console-out')).toContainText(
    'structured server log',
  )
  await expect(page.locator('#console-out')).not.toContainText('early output')
})
