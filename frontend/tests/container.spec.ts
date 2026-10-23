import { expect, test } from '@playwright/test'
import {
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
  await expect(page.locator('#gaming-panel')).toContainText(
    'native Windows installation',
  )
  await expect(page.locator('#gaming-save')).toHaveCount(0)
  await expect(page.locator('#startup-panel')).toContainText(
    'native Windows installation',
  )
  await expect(page.locator('#startup-install')).toBeDisabled()
})
