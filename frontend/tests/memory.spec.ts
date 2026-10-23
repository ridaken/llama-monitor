import { expect, test } from '@playwright/test'
import { mockApi, stats } from './fixtures'

for (const source of ['log', 'nvml', 'nvml-delta']) {
  test(`Hardware embeds model memory with its ${source} source`, async ({
    page,
  }) => {
    await mockApi(page, {
      'GET /api/stats': () => ({
        ...stats,
        split_source: source,
        split:
          source === 'log'
            ? stats.split
            : stats.split.filter((entry) => entry.kind === 'gpu'),
      }),
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/')
    await expect(page.locator('.monitor-split')).toHaveCount(0)
    const gpu = page.locator('.monitor-gpu').first()
    const label =
      source === 'log'
        ? 'llama.cpp buffers \u00b7 logged'
        : source === 'nvml'
          ? 'llama-server VRAM'
          : 'llama-server \u00b7 estimate'
    await expect(gpu.locator('.model-memory')).toContainText(label)
    await expect(gpu.locator('.model-memory')).toContainText('10.0 GB')
    await expect(page.locator('.monitor-cpu')).toContainText('2.0 GB')
    if (source === 'log') {
      await gpu.getByRole('button', { name: 'More about ' + label }).hover()
      await expect(page.getByRole('tooltip')).toContainText(
        'not live physical usage',
      )
      await page.keyboard.press('Escape')
      await expect(page.getByRole('tooltip')).toBeHidden()
    }
    await expect(gpu.locator('.vram-bar')).toHaveCount(
      source === 'nvml' ? 1 : 0,
    )
    if (process.env.CAPTURE_UI === '1') {
      await page
        .locator('.monitor-hardware')
        .screenshot({ path: `test-results/memory-${source}.png` })
    }
  })
}

test('measured VRAM colors llama-server and other usage against full capacity', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      split_source: 'nvml',
      split: [
        { label: 'CUDA0', kind: 'gpu', gpu_index: 0, bytes: 6 * 1024 ** 3 },
      ],
    }),
  })
  await page.goto('/')
  const bar = page.getByRole('img', {
    name: 'VRAM: llama-server 6.0 GB, other usage 4.0 GB, capacity 24.0 GB',
  })
  await expect(bar).toBeVisible()
  await expect(bar.locator('.vram-model')).toHaveCSS(
    'width',
    await bar.evaluate((el) => `${el.clientWidth / 4}px`),
  )
  await expect(page.locator('.vram-legend')).toContainText('Other usage')
  if (process.env.CAPTURE_UI === '1')
    await page
      .locator('.monitor-hardware')
      .screenshot({ path: 'test-results/memory-measured-split.png' })
})

test('logged buffers use confirmed device identity rather than CUDA ordinal', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      gpu: { ok: true, devices: [{ ...stats.gpu.devices[0], index: 7 }] },
      split: [
        { label: 'CUDA0', kind: 'gpu', gpu_index: 7, bytes: 8 * 1024 ** 3 },
      ],
    }),
  })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu .model-memory')).toContainText(
    '8.0 GB',
  )
  await expect(page.locator('.monitor-gpu')).toContainText('GPU 7')
})

test('unmatched log devices retain their own hardware card without guessing', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      split: [{ label: 'CUDA0', kind: 'gpu', bytes: 8 * 1024 ** 3 }],
    }),
  })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu .model-memory')).toHaveCount(0)
  await expect(page.locator('.monitor-placement')).toContainText('CUDA0')
  await expect(page.locator('.monitor-placement .model-memory')).toContainText(
    '8.0 GB',
  )
})

test('oversized allocations remain visible as logged buffers, outside live VRAM', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      split: [
        { label: 'CUDA0', kind: 'gpu', gpu_index: 0, bytes: 30 * 1024 ** 3 },
      ],
    }),
  })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu .model-memory')).toContainText(
    '30.0 GB',
  )
  await expect(page.locator('.monitor-gpu').getByRole('status')).toContainText(
    'Exceeds device capacity',
  )
  await expect(page.locator('.vram-bar')).toHaveCount(0)
  await expect(page.locator('.monitor-gpu')).toContainText('10.0 GB / 24.0 GB')
})

test('inconsistent or missing process readings never fabricate an other-usage segment', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      split_source: 'nvml',
      split: [
        { label: 'CUDA0', kind: 'gpu', gpu_index: 0, bytes: 12 * 1024 ** 3 },
      ],
    }),
  })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu .model-memory')).toContainText(
    '12.0 GB',
  )
  await expect(page.locator('.vram-bar')).toHaveCount(0)
})
