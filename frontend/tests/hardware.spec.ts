import { expect, test } from '@playwright/test'
import { mockApi, stats } from './fixtures'

test('Fit Hardware hugs its cards on a tall three-GPU dashboard', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      gpu: {
        ...stats.gpu,
        devices: [0, 1, 2].map((index) => ({
          ...stats.gpu.devices[0],
          index,
        })),
      },
    }),
  })
  await page.setViewportSize({ width: 2560, height: 1440 })
  await page.goto('/')
  const layout = await page.locator('.monitor-hardware').evaluate((panel) => {
    const grid = panel.querySelector('.hardware-grid')!
    const cards = Array.from(grid.children)
    return {
      gap:
        grid.getBoundingClientRect().bottom -
        Math.max(...cards.map((card) => card.getBoundingClientRect().bottom)),
      bottom: panel.getBoundingClientRect().bottom,
      viewport: innerHeight,
    }
  })
  expect(layout.gap).toBeLessThan(2)
  expect(layout.bottom).toBeLessThanOrEqual(layout.viewport)
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/hardware-fit-three-gpu.png',
      fullPage: true,
    })
})

test('Hardware combines RAM metrics and matched GPU placement, excluding the output layer', async ({
  page,
}) => {
  await mockApi(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await expect(page.locator('.monitor-hardware')).toContainText('Hardware')
  await expect(page.locator('.monitor-memory')).toHaveCount(0)
  await expect(page.locator('.monitor-cpu')).toContainText('8 / 48')
  await expect(page.locator('.monitor-cpu')).toContainText('Elsewhere')
  await expect(page.locator('.monitor-cpu')).toContainText('llama-server RSS')
  await expect(page.locator('.monitor-gpu')).toContainText('40 / 48')
  await expect(page.locator('.monitor-gpu')).toContainText('Assigned here')
  await expect(page.locator('.monitor-gpu')).toContainText('VRAM')
  await expect(page.locator('#gpu-total-w')).toContainText('210 W')
  const grid = await page.locator('.hardware-grid').evaluate((el) => ({
    scroll: el.scrollHeight,
    height: el.clientHeight,
  }))
  expect(grid.scroll).toBeLessThanOrEqual(grid.height + 1)
  await page
    .locator('.monitor-cpu')
    .getByRole('button', { name: 'More about Assigned model layers' })
    .hover()
  await expect(page.getByRole('tooltip')).toContainText(
    'not a count of fully resident layers',
  )
})

for (const [status, value] of [
  ['no_model', 'No model loaded'],
  ['loading', 'Loading'],
  ['unavailable', 'Unavailable'],
  ['shared', 'Shared across devices'],
]) {
  test(`placement state ${status} never fabricates zero`, async ({ page }) => {
    await mockApi(page, {
      'GET /api/stats': () => ({
        ...stats,
        layer_placement: {
          status,
          devices: [],
          reason:
            status === 'unavailable'
              ? 'Restart with debug logging enabled.'
              : null,
        },
      }),
    })
    await page.goto('/')
    await expect(page.locator('.monitor-cpu .layer-placement')).toContainText(
      value,
    )
    await expect(page.locator('.monitor-gpu .layer-placement')).toContainText(
      value,
    )
    await expect(page.locator('.layer-placement')).not.toContainText([
      '0 /',
      '0 /',
    ])
    if (status === 'unavailable')
      await expect(page.locator('#placement-note')).toContainText('Restart')
  })
}

test('CPU-only placement and Docker RAM remain available without NVML', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      runtime: { containerized: true },
      gpu: { ok: false, devices: [], error: 'NVML unavailable' },
      layer_placement: {
        status: 'available',
        total_layers: 2,
        output_device: 'CPU',
        devices: [{ label: 'CPU', layers: 2 }],
      },
    }),
  })
  await page.goto('/')
  await expect(page.locator('.monitor-cpu')).toContainText('CPU & Docker RAM')
  await expect(page.locator('.monitor-cpu')).toContainText('2 / 2')
  await expect(page.locator('.monitor-cpu')).toContainText('Assigned here')
  await expect(page.locator('.monitor-gpu')).toHaveCount(0)
  await expect(page.locator('#gpu-error')).toContainText('NVML unavailable')
})

test('remapped identities and unmatched accelerators retain their own placement cards', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      layer_placement: {
        ...stats.layer_placement,
        output_device: 'CUDA7',
        devices: [
          { label: 'CPU', layers: 0 },
          { label: 'CUDA7', layers: 24, gpu_index: 0 },
          { label: 'VULKAN0', layers: 24 },
        ],
      },
    }),
  })
  for (const mode of ['Fit', 'Comfortable']) {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/')
      await page.getByRole('button', { name: mode, exact: true }).click()
      await expect(page.locator('.monitor-cpu')).toContainText('0 / 48')
      await expect(page.locator('.monitor-gpu')).toContainText('24 / 48')
      await expect(page.locator('.monitor-placement')).toContainText('VULKAN0')
      await expect(page.locator('.monitor-placement')).toContainText('24 / 48')
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth + 1,
        ),
      ).toBe(true)
      if (process.env.CAPTURE_UI === '1')
        await page.screenshot({
          path: `test-results/hardware-${mode}-${width}.png`,
          fullPage: true,
        })
    }
  }
})
