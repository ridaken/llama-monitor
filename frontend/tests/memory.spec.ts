import { expect, test } from '@playwright/test'
import { mockApi, stats } from './fixtures'

for (const source of ['log', 'nvml', 'nvml-delta']) {
  test(`model memory explains ${source} and its share bar`, async ({
    page,
  }) => {
    await mockApi(page, {
      'GET /api/stats': () => ({ ...stats, split_source: source }),
    })
    await page.goto('/')
    const tile = page.locator('.monitor-split')
    await expect(tile.locator('.monitor-panel-header')).toContainText(
      'Model memory',
    )
    await tile.getByRole('button', { name: 'More about Model memory' }).hover()
    await expect(page.getByRole('tooltip')).toContainText(
      'not how full its VRAM is',
    )
    await expect(page.getByRole('tooltip')).toContainText('mapped files')
    await expect(page.locator('#split-note')).toContainText(
      source === 'log'
        ? 'not live usage'
        : source === 'nvml'
          ? 'excludes CPU RAM'
          : 'other apps can affect it',
    )
    await expect(tile.getByRole('status')).toHaveCount(0)
  })
}

test('oversized log allocations are flagged without changing hardware readings', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      split: [{ label: 'CUDA0', kind: 'gpu', bytes: 30 * 1024 ** 3 }],
    }),
  })
  await page.goto('/')
  await expect(
    page.locator('.monitor-split').getByRole('status'),
  ).toContainText('exceed a GPU’s capacity')
  await expect(page.locator('#split-legend')).toContainText('30.0 GB')
  await expect(page.locator('.monitor-gpu')).toContainText('10.0 GB / 24.0 GB')
})
