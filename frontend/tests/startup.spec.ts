import { expect, test } from '@playwright/test'
import { mockApi, startupState } from './fixtures'

test('boot setup remembers models without dashboard password input', async ({
  page,
}) => {
  let requested: any
  let state = { ...startupState }
  await mockApi(page, {
    'GET /api/startup/state': () => state,
    'POST /api/startup/install': (request) => {
      requested = request.postDataJSON()
      state = { ...state, pending: true }
      return state
    },
  })
  await page.goto('/#manage')
  await expect(page.locator('#startup-models')).toBeChecked()
  await page.locator('#startup-install').click()
  await expect(page.locator('#startup-status')).toContainText(
    'Waiting for Windows setup',
  )
  expect(requested).toEqual({ mode: 'boot', autostart_models: true })
  await expect(page.locator('#startup-panel input[type=password]')).toHaveCount(
    0,
  )
  await expect(page.locator('#startup-install')).toBeDisabled()
})

test('monitor-only sign-in startup can be selected', async ({ page }) => {
  let requested: any
  await mockApi(page, {
    'POST /api/startup/install': (request) => {
      requested = request.postDataJSON()
      return { ...startupState, pending: true }
    },
  })
  await page.goto('/#manage')
  await page.locator('#startup-mode').click()
  await page
    .getByRole('option', { name: 'When I sign in', exact: true })
    .click()
  await page.locator('#startup-models').click()
  await page.locator('#startup-install').click()
  await expect(page.locator('#startup-status')).toContainText(
    'Waiting for Windows setup',
  )
  expect(requested).toEqual({ mode: 'logon', autostart_models: false })
})

test('installed task and remembered models support removal', async ({
  page,
}) => {
  const state = {
    ...startupState,
    installed: true,
    autostart_models: true,
    task_name: 'llama-monitor-test',
    models: [{ id: 'primary', name: 'Original model', port: 8001 }],
  }
  const calls = await mockApi(page, {
    'GET /api/startup/state': () => state,
    'POST /api/startup/remove': () => ({ ...state, pending: true }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#startup-status')).toContainText(
    'starts before sign-in',
  )
  await expect(page.locator('#startup-saved-models')).toContainText(
    'Original model (8001)',
  )
  await page.locator('#startup-remove').click()
  expect(calls).toContain('POST /api/startup/remove')
})

test('cancelled Windows setup reports an error', async ({ page }) => {
  await mockApi(page, {
    'POST /api/startup/install': () => ({
      status: 400,
      body: { error: 'Windows setup cancelled' },
    }),
  })
  await page.goto('/#manage')
  await page.locator('#startup-install').click()
  await expect(page.locator('#startup-error')).toHaveText(
    'Windows setup cancelled',
  )
  await expect(page.locator('#startup-install')).toBeEnabled()
})
