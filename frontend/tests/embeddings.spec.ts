import { expect, test } from '@playwright/test'
import { config, launcherState, mockApi } from './fixtures'

test('embedding configuration reuses the mounted model browser and saves role settings', async ({
  page,
}) => {
  await mockApi(page)
  const embedding = {
    ...config,
    name: 'Embedding test',
    model_path: '/models/embed.gguf',
    port: 8081,
  }
  let current = {
    ...launcherState,
    settings: {
      ...launcherState.settings,
      default_config: embedding.name,
      default_port: 8081,
      models_dir: '/models',
      autostart_after_primary: false,
      cpu_fallback: false,
    },
    configs: [embedding],
    runtime: {
      containerized: true,
      models_dir: '/models',
      managed_port: 8001,
      embedding_port: 8081,
    },
    status: {
      state: 'stopped',
      adopted: false,
      config_name: null as string | null,
      config: embedding,
      reason: 'Stopped',
      mode: 'configured',
    },
  }
  await page.route('**/api/**role=embeddings*', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/settings')) {
      current = {
        ...current,
        settings: { ...current.settings, ...route.request().postDataJSON() },
      }
    }
    if (path.endsWith('/launch')) {
      current = {
        ...current,
        status: {
          ...current.status,
          state: 'waiting',
          reason: 'Waiting for primary readiness.',
        },
      }
    }
    if (path.endsWith('/stop')) {
      current = {
        ...current,
        status: {
          ...current.status,
          state: 'stopped',
          reason: 'Embedding launch cancelled.',
        },
      }
    }
    await route.fulfill({ json: current })
  })
  await page.goto('/')
  await page.locator('#embedding-configure').click()
  await expect(page.locator('#ex-model')).toHaveValue('/models/embed.gguf')
  await expect(page.getByRole('dialog')).toContainText('internal port 8081')
  await page.locator('#embedding-autostart').check()
  await page.locator('#embedding-cpu-fallback').check()
  await expect(page.locator('#embedding-cpu-fallback')).toBeChecked()
  await expect(page.locator('#embedding-autostart')).toBeChecked()
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({ path: 'test-results/embedding-configuration.png' })
  await page.locator('#ex-model-browse').click()
  await expect(page.locator('#ex-browse-path')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.keyboard.press('Escape')
  await page.locator('#embedding-start').click()
  await expect(page.locator('#embedding-status')).toHaveText(
    'Waiting for primary',
  )
  await page.locator('#embedding-stop').click()
  await expect(page.locator('#embedding-status')).toHaveText('Stopped')
  await expect(page.locator('#lx-config')).toContainText(config.name)
})

for (const width of [390, 1440]) {
  test(`embedding CPU and failure status fit at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockApi(page)
    let status = {
      state: 'ready',
      config: { ...config, port: 8081, name: 'Embedding test' },
      reason: 'Running on CPU.',
      mode: 'cpu',
    }
    await page.route('**/api/launcher/state?role=embeddings', (route) =>
      route.fulfill({ json: { ...launcherState, status } }),
    )
    await page.goto('/')
    await expect(page.locator('#embedding-status')).toHaveText('Running on CPU')
    if (process.env.CAPTURE_UI === '1')
      await page.screenshot({
        path: `test-results/embedding-${width}.png`,
        fullPage: true,
      })
    status = {
      ...status,
      state: 'failed',
      reason: 'CPU fallback failed: insufficient RAM.',
    }
    await expect(page.locator('#embedding-status')).toHaveText('Failed')
    await expect(page.locator('#embedding-panel')).toContainText(
      'insufficient RAM',
    )
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy()
  })
}
