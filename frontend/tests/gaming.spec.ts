import { expect, test } from '@playwright/test'
import { config, gamingState, launcherState, mockApi, stats } from './fixtures'

for (const [phase, label, countdown] of [
  ['gaming', 'AI paused for Moonlight', null],
  ['countdown', 'AI resumes in 42s', 42],
  ['retry_countdown', 'AI retry in 60s', 60],
  ['restoring', 'Restoring AI models', null],
] as const) {
  test(`${phase} is visible and prevents AI launches`, async ({ page }) => {
    const state = {
      ...gamingState,
      enabled: true,
      phase,
      blocked: true,
      countdown,
      servers: [
        {
          id: 'primary',
          name: config.name,
          port: 8001,
          status: 'stopped',
          attempts: 0,
          error: null,
        },
      ],
    }
    await mockApi(page, {
      'GET /api/stats': () => ({ ...stats, online: false, gaming: state }),
      'GET /api/gaming/state': () => state,
      'GET /api/launcher/state': () => ({
        ...launcherState,
        gaming: state,
        status: { state: 'stopped', config_name: config.name, config },
      }),
    })
    await page.goto('/#manage')
    await expect(page.locator('#status')).toHaveText(label)
    await expect(page.locator('#gaming-status')).toContainText(label)
    await expect(page.locator('#lx-launch')).toBeDisabled()
    await expect(page.locator('#lx-restart')).toBeDisabled()
    await expect(page.locator('#lx-stop')).toBeEnabled()
  })
}

test('pending unsaved configuration is restored in the form after reload', async ({
  page,
}) => {
  const pending = { ...config, port: 9002, log_prompts: true }
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      status: { state: 'stopped', config: pending },
      gaming: {
        ...gamingState,
        enabled: true,
        phase: 'gaming',
        blocked: true,
        servers: [{ id: 'primary', status: 'stopped' }],
      },
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-port')).toHaveValue('9002')
  await expect(page.locator('#lx-log-prompts')).toBeChecked()
  await expect(page.locator('#lx-dirty-mark')).toBeVisible()
})

test('failed restoration reports error and manual retry', async ({ page }) => {
  let state = {
    ...gamingState,
    enabled: true,
    phase: 'failed',
    blocked: true,
    connected_clients: 0,
    servers: [
      {
        id: 'primary',
        name: 'Original model',
        port: 8001,
        status: 'failed',
        attempts: 2,
        error: 'Insufficient memory',
      },
    ],
  }
  const calls = await mockApi(page, {
    'GET /api/gaming/state': () => state,
    'GET /api/stats': () => ({ ...stats, online: false, gaming: state }),
    'POST /api/gaming/retry': () => {
      state = { ...state, phase: 'restoring' }
      return state
    },
  })
  await page.goto('/#manage')
  await expect(page.locator('#gaming-servers')).toContainText(
    'Insufficient memory',
  )
  await expect(page.locator('#gaming-servers')).toContainText('attempt 2/2')
  await page.locator('#gaming-retry').click()
  await expect(page.locator('#gaming-status')).toContainText(
    'Restoring AI models',
  )
  expect(calls).toContain('POST /api/gaming/retry')
})

test('unknown connection state holds retry and explains why', async ({
  page,
}) => {
  const state = {
    ...gamingState,
    enabled: true,
    phase: 'failed',
    blocked: true,
    connected_clients: null,
    integration_error: 'Apollo is unreachable',
  }
  await mockApi(page, {
    'GET /api/gaming/state': () => state,
    'GET /api/stats': () => ({ ...stats, gaming: state }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#status')).toContainText('restoration on hold')
  await expect(page.locator('#gaming-panel')).toContainText(
    'Apollo is unreachable',
  )
  await expect(page.locator('#gaming-retry')).toBeDisabled()
})

test('connection password clears after save and installation commands are visible', async ({
  page,
}) => {
  let saved = false
  await mockApi(page, {
    'POST /api/gaming/settings': (request) => {
      saved = request.postDataJSON().password === 'test-private-password'
      return { ...gamingState, credentials_saved: true }
    },
  })
  await page.goto('/#manage')
  await page.locator('#gaming-user').fill('user')
  await page.locator('#gaming-password').fill('test-private-password')
  await page.locator('#gaming-save').click()
  await expect(page.locator('#gaming-password')).toHaveValue('')
  expect(saved).toBe(true)
  await page.getByText('Apollo installation commands', { exact: true }).click()
  await expect(page.locator('#gaming-panel pre')).toContainText(
    'prepare-command',
  )
  await expect(page.locator('#gaming-panel pre')).toContainText('--rollback')
})

test('gaming panel fits narrow screens', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockApi(page)
  await page.goto('/#manage')
  await expect(page.locator('#gaming-panel')).toBeVisible()
  await page.getByText('Apollo installation commands', { exact: true }).click()
  const width = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    viewport: innerWidth,
  }))
  expect(width.scroll).toBeLessThanOrEqual(width.viewport + 1)
})

test('unsupported Apollo explains the build requirement without enabling integration', async ({
  page,
}) => {
  const calls = await mockApi(page, {
    'GET /api/gaming/state': () => ({
      ...gamingState,
      credentials_saved: true,
    }),
    'POST /api/gaming/test': () => ({
      status: 400,
      body: {
        error:
          'Install the Apollo build with independent authentication sessions. No login was attempted.',
      },
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#gaming-panel a')).toHaveAttribute(
    'href',
    'https://github.com/ridaken/Apollo',
  )
  await page.locator('#gaming-test').click()
  await expect(page.locator('#gaming-message')).toContainText(
    'No login was attempted',
  )
  expect(calls).not.toContain('POST /api/gaming/settings')
})
