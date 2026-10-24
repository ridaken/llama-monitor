import { expect, test } from '@playwright/test'
import { config, launcherState, mockApi } from './fixtures'

test('startup preference saves immediately and persists without launching', async ({
  page,
}) => {
  let state = structuredClone(launcherState)
  const calls = await mockApi(page, {
    'GET /api/launcher/state': () => state,
    'POST /api/launcher/settings': (request) => {
      expect(request.postDataJSON()).toEqual({ autostart_default_model: true })
      state = {
        ...state,
        settings: { ...state.settings, autostart_default_model: true },
      }
      return state
    },
  })
  await page.goto('/#manage')
  const checkbox = page.getByRole('checkbox', {
    name: 'Start the default model when llama-monitor starts',
  })
  await expect(checkbox).not.toBeChecked()
  await expect(page.locator('#model-startup-default')).toContainText(
    config.name,
  )
  await checkbox.click()
  await expect(checkbox).toBeChecked()
  await page.reload()
  await expect(checkbox).toBeChecked()
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await expect(page.locator('#lx-model')).toHaveValue(config.model_path)
  expect(calls).not.toContain('POST /api/launcher/launch')
})

test('saving disables the checkbox and preserves launcher edits', async ({
  page,
}) => {
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  await mockApi(page, {
    'POST /api/launcher/settings': async () => {
      await pending
      return {
        ...launcherState,
        settings: { ...launcherState.settings, autostart_default_model: true },
      }
    },
  })
  await page.goto('/')
  await expect(page.locator('#lx-model')).toHaveValue(config.model_path)
  await page.locator('#lx-port').fill('9001')
  await page.locator('#manage-open').click()
  const checkbox = page.locator('#autostart-default-model')
  await checkbox.click()
  await expect(checkbox).toBeDisabled()
  release()
  await expect(checkbox).toBeEnabled()
  await expect(checkbox).toBeChecked()
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await expect(page.locator('#lx-port')).toHaveValue('9001')
  await expect(page.locator('#lx-dirty-mark')).toBeVisible()
})

test('save failure retains the saved preference and reports an error', async ({
  page,
}) => {
  await mockApi(page, {
    'POST /api/launcher/settings': () => ({
      status: 400,
      body: { error: 'Unable to save settings' },
    }),
  })
  await page.goto('/#manage')
  await page.locator('#autostart-default-model').click()
  await expect(page.locator('#model-startup-panel [role=alert]')).toContainText(
    'Unable to save settings',
  )
  await expect(page.locator('#autostart-default-model')).not.toBeChecked()
  await expect(page.locator('#autostart-default-model')).toBeEnabled()
})

test('missing default guidance and startup failure remain visible', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      settings: { ...launcherState.settings, default_config: null },
      autostart_error:
        'Could not start the default model: Model file not found: missing.gguf',
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#model-startup-default')).toContainText(
    'Select a saved configuration in Monitor and click Set default',
  )
  await expect(page.locator('#model-startup-panel [role=alert]')).toContainText(
    'Model file not found: missing.gguf',
  )
})

test('unchecked startup still fills the launcher with the default configuration', async ({
  page,
}) => {
  const calls = await mockApi(page)
  await page.goto('/')
  await expect(page.locator('#lx-model')).toHaveValue(config.model_path)
  await page.reload()
  await expect(page.locator('#lx-model')).toHaveValue(config.model_path)
  expect(calls).not.toContain('POST /api/launcher/launch')
})
