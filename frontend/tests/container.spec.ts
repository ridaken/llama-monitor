import { expect, test } from '@playwright/test'
import {
  config,
  gamingState,
  launcherState,
  mockApi,
  startupState,
  stats,
} from './fixtures'

test('Docker explains ports, model paths, telemetry, and native-only features', async ({
  page,
}) => {
  const runtime = {
    containerized: true,
    models_dir: '/models',
    models_host_path: '/home/tom/models',
    managed_port: 8001,
  }
  await mockApi(page, {
    'GET /api/launcher/state': () => ({ ...launcherState, runtime }),
    'GET /api/stats': () => ({ ...stats, runtime }),
    'GET /api/gaming/state': () => ({ ...gamingState, supported: false }),
    'GET /api/startup/state': () => ({ ...startupState, supported: false }),
  })
  await page.goto('/')
  await expect(page.locator('#lx-port')).toHaveAttribute('readonly', '')
  await expect(
    page.getByRole('button', {
      name: 'More about Hardware',
      exact: true,
    }),
  ).toBeVisible()
  await expect(page.locator('.monitor-cpu')).toContainText('CPU & Docker RAM')
  await page
    .getByRole('button', { name: 'More about Hardware', exact: true })
    .hover()
  await expect(page.getByRole('tooltip')).toContainText('not Windows host RAM')
  await expect(page.getByText(/Change INFERENCE_PORT/)).toBeVisible()
  await page.getByRole('link', { name: 'Manage', exact: true }).click()
  await expect(page.getByText(/Browse models under \/models/)).toBeVisible()
  await expect(
    page.getByText('Host folder for /models', { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText('/home/tom/models', { exact: true }),
  ).toBeVisible()
  await expect(page.locator('#gaming-panel')).toContainText(
    'native Windows installation',
  )
  await expect(page.locator('#gaming-save')).toHaveCount(0)
  await expect(page.locator('#startup-panel')).toContainText(
    'native Windows installation',
  )
  await expect(page.locator('#startup-install')).toBeDisabled()
})

test('model picker keeps folders intact and recovers from a pasted host path', async ({
  page,
}) => {
  const requested: string[] = []
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      settings: { ...launcherState.settings, models_dir: '/models/controller' },
      configs: [{ ...config, model_path: '' }],
      runtime: {
        containerized: true,
        models_dir: '/models',
        models_host_path: '/home/tom/models',
        managed_port: 8001,
      },
    }),
    'GET /api/browse': (request) => {
      const path = new URL(request.url()).searchParams.get('path') || ''
      requested.push(path)
      if (path.startsWith('\\\\')) {
        return {
          status: 400,
          body: {
            error:
              'This is a Windows or WSL File Explorer path. Open /models or set MODELS_DIR.',
          },
        }
      }
      return {
        path,
        parent: '/models',
        dirs: [],
        files: [`${path}/model.gguf`],
      }
    },
  })
  await page.goto('/')
  await page.locator('#lx-model-browse').click()
  const dialog = page.getByRole('dialog')
  const location = dialog.getByRole('textbox', {
    name: 'Location',
    exact: true,
  })
  await expect(location).toHaveValue('/models/controller')
  expect(requested[0]).toBe('/models/controller')
  await expect(dialog).toContainText('Browse files inside Docker')
  await expect(
    dialog.getByText('/home/tom/models', { exact: true }),
  ).toBeVisible()
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({ path: 'test-results/models-host-folder.png' })
  const hostPath = '\\\\wsl.localhost\\Ubuntu\\home\\tom'
  await location.fill(hostPath)
  await location.press('Enter')
  await expect(dialog).toContainText('File Explorer path. Open /models')
  await expect(location).toHaveValue(hostPath)
  await dialog
    .getByRole('button', { name: 'Mounted models (/models)', exact: true })
    .click()
  await expect(location).toHaveValue('/models')
  await expect(dialog).not.toContainText('Cannot open:')
  await location.fill('/models/Other models')
  await dialog.getByRole('button', { name: 'Go', exact: true }).click()
  await expect(location).toHaveValue('/models/Other models')
  await dialog.getByRole('button', { name: 'model.gguf', exact: true }).click()
  await expect(page.locator('#lx-model')).toHaveValue(
    '/models/Other models/model.gguf',
  )
})

for (const hostPath of [
  'C:\\Users\\Tom\\AI Models\\' + 'long folder name\\'.repeat(12),
  null,
]) {
  test(`model picker shows the mount source: ${hostPath ? 'Windows path' : 'unavailable'}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await mockApi(page, {
      'GET /api/launcher/state': () => ({
        ...launcherState,
        runtime: {
          containerized: true,
          models_dir: '/models',
          models_host_path: hostPath,
          managed_port: 8001,
        },
      }),
    })
    await page.goto('/')
    await page.locator('#lx-model-browse').click()
    const dialog = page.getByRole('dialog')
    await expect(
      dialog.getByText('Host folder for /models', { exact: true }),
    ).toBeVisible()
    const path = dialog.getByText(
      hostPath || 'Unavailable for this container',
      { exact: true },
    )
    await expect(path).toBeVisible()
    const box = await dialog.boundingBox()
    const pathBox = await path.boundingBox()
    expect(pathBox!.x + pathBox!.width).toBeLessThanOrEqual(box!.x + box!.width)
  })
}

test('native model picker has no Docker mount information', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/')
  await page.locator('#lx-model-browse').click()
  await expect(page.getByRole('dialog')).not.toContainText(
    'Host folder for /models',
  )
})
