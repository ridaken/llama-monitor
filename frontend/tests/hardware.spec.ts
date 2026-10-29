import { expect, test } from '@playwright/test'
import { mockApi, stats } from './fixtures'

const gpuNames = [
  'NVIDIA GeForce RTX 4070 SUPER',
  'NVIDIA GeForce RTX 3060 Ti',
  'NVIDIA GeForce RTX 5060 Ti',
]
const hardwareStats = (count: number) => ({
  ...stats,
  split: stats.split.filter((entry) => entry.kind === 'cpu' || count > 1),
  gpu: {
    ...stats.gpu,
    devices: Array.from({ length: count - 1 }, (_, index) => ({
      ...stats.gpu.devices[0],
      index,
      name: gpuNames[index % gpuNames.length],
    })),
  },
})

for (const mode of ['Fit', 'Comfortable']) {
  for (const widerFont of [false, true]) {
    test(`${mode} Hardware stays stable during power updates${widerFont ? ' with wider fonts' : ''}`, async ({
      page,
    }) => {
      test.setTimeout(60_000)
      await page.clock.install()
      let power: number | null = 31
      await mockApi(page, {
        'GET /api/stats': () => {
          const data = hardwareStats(4)
          return {
            ...data,
            gpu: {
              ...data.gpu,
              devices: data.gpu.devices.map((device, index) => ({
                ...device,
                power: index === 0 ? power : 4,
              })),
            },
          }
        },
      })
      await page.setViewportSize({ width: 3840, height: 1440 })
      await page.goto('/')
      await page.getByRole('button', { name: mode, exact: true }).click()
      await expect(page.locator('.monitor-gpu')).toHaveCount(3)
      if (widerFont)
        await page.addStyleTag({
          content: '.monitor-hardware { font-family: Arial, sans-serif; }',
        })
      const panel = page.locator('.monitor-hardware')
      await panel.locator('.monitor-panel-content').evaluate((el) => {
        ;(el as HTMLElement).style.paddingInline = '0px'
      })
      const gpu = page.locator('.monitor-gpu').first()
      const badge = gpu.locator('.monitor-gpu-power')
      const historyLength = () =>
        gpu
          .locator('svg')
          .first()
          .evaluate(
            (el) =>
              el
                .querySelector('polyline')!
                .getAttribute('points')!
                .trim()
                .split(/\s+/).length,
          )
      const measure = () =>
        gpu.evaluate((card) => {
          const header = card.querySelector('.monitor-gpu-header')!
          const name = header.querySelector('strong')!
          const range = document.createRange()
          range.selectNodeContents(name)
          const lines = new Set(
            Array.from(range.getClientRects(), (rect) => Math.round(rect.top)),
          ).size
          const panel = card
            .closest('.monitor-hardware')!
            .getBoundingClientRect()
          return {
            lines,
            badgeWidth: header
              .querySelector('.monitor-gpu-power')!
              .getBoundingClientRect().width,
            nameHeight: name.getBoundingClientRect().height,
            headerHeight: header.getBoundingClientRect().height,
            cardHeight: card.getBoundingClientRect().height,
            panelTop: panel.top + window.scrollY,
            panelBottom: panel.bottom + window.scrollY,
            panelHeight: panel.height,
          }
        })
      const setWidth = async (width: number) => {
        await panel.evaluate((el, width) => {
          ;(el as HTMLElement).style.width = `${width}px`
        }, width)
        await page.clock.runFor(200)
      }
      // Locate this font and density's actual one/two-line boundary while
      // retaining four columns, then exercise both sides through real polling.
      let narrow = 1330
      let wide = 2400
      await setWidth(narrow)
      expect((await measure()).lines).toBeGreaterThan(1)
      await setWidth(wide)
      expect((await measure()).lines).toBe(1)
      while (wide - narrow > 1) {
        const middle = Math.floor((narrow + wide) / 2)
        await setWidth(middle)
        if ((await measure()).lines === 1) wide = middle
        else narrow = middle
      }
      for (const width of [narrow - 2, wide + 2]) {
        await setWidth(width)
        const baseline = await measure()
        expect(baseline.lines).toBe(width < wide ? 2 : 1)
        for (power of [31, 32, 9, 10, 99, 100, 999, 1000, null]) {
          const previousSamples = await historyLength()
          await page.clock.fastForward(1100)
          await expect(badge).toHaveText(`${power ?? '—'} W`)
          await expect(page.locator('#gpu-total-w')).toHaveText(
            `Total draw ${(power ?? 0) + 8} W`,
          )
          await expect.poll(historyLength).toBeGreaterThan(previousSamples)
          await page.clock.runFor(100)
          const current = await measure()
          expect(current.lines).toBe(baseline.lines)
          for (const key of [
            'badgeWidth',
            'nameHeight',
            'headerHeight',
            'cardHeight',
            'panelTop',
            'panelBottom',
            'panelHeight',
          ] as const)
            expect(Math.abs(current[key] - baseline[key]), key).toBeLessThan(1)
          if (process.env.CAPTURE_UI === '1' && (power === 9 || power === 1000))
            await panel.screenshot({
              path: `test-results/hardware-power-${mode}-${widerFont ? 'wide-font' : 'default-font'}-${width}-${power}.png`,
            })
        }
      }
      // The reservation is a minimum, so unexpectedly large readings remain
      // readable instead of being clipped to the usual four-digit budget.
      const reservedWidth = (await measure()).badgeWidth
      power = 123456789
      await page.clock.fastForward(1100)
      await expect(badge).toHaveText('123456789 W')
      expect((await measure()).badgeWidth).toBeGreaterThan(reservedWidth)
      expect(
        await badge.evaluate((el) => el.scrollWidth <= el.clientWidth + 1),
      ).toBe(true)
    })
  }

  for (const count of [1, 2, 3, 4, 5, 6]) {
    test(`${mode} Hardware lays out ${count} cards using available section width`, async ({
      page,
    }) => {
      await mockApi(page, { 'GET /api/stats': () => hardwareStats(count) })
      await page.setViewportSize({ width: 2560, height: 1440 })
      await page.goto('/')
      await page.getByRole('button', { name: mode, exact: true }).click()
      const grid = page.locator('.hardware-grid')
      await expect(grid.locator(':scope > *')).toHaveCount(count)
      const measure = () =>
        grid.evaluate((el) => {
          const cards = Array.from(el.children).map((card) =>
            card.getBoundingClientRect(),
          )
          return {
            columns: cards.filter(
              (card) => Math.abs(card.top - cards[0].top) < 1,
            ).length,
            rows: new Set(cards.map((card) => Math.round(card.top))).size,
            equalWidths: cards.every(
              (card) => Math.abs(card.width - cards[0].width) < 1,
            ),
            fits: Array.from(el.querySelectorAll<HTMLElement>('*')).every(
              (child) => child.scrollWidth <= child.clientWidth + 1,
            ),
            pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
          }
        })
      await expect
        .poll(async () => (await measure()).columns)
        .toBe(Math.min(4, count))
      expect((await measure()).rows).toBe(Math.ceil(count / 4))
      expect((await measure()).equalWidths).toBe(true)
      expect((await measure()).fits).toBe(true)
      if (count === 4 && process.env.CAPTURE_UI === '1')
        await page.locator('.monitor-hardware').screenshot({
          path: `test-results/hardware-four-${mode}.png`,
        })

      // Change only the section width, as if another dashboard tile took space.
      // Zero the content's horizontal padding to exercise exact content widths.
      await page
        .locator('.monitor-hardware .monitor-panel-content')
        .evaluate((el) => {
          ;(el as HTMLElement).style.paddingInline = '0px'
        })
      for (const [width, columns] of [
        [1328, 4],
        [1327, 3],
        [992, 3],
        [991, 2],
        [656, 2],
        [655, 1],
        [656, 2],
        [992, 3],
        [1328, 4],
      ]) {
        await page.locator('.monitor-hardware').evaluate((el, width) => {
          ;(el as HTMLElement).style.width = `${width + 2}px`
        }, width)
        await expect
          .poll(async () => (await measure()).columns)
          .toBe(Math.min(columns, count))
        expect((await measure()).equalWidths).toBe(true)
        expect((await measure()).fits).toBe(true)
      }
      await page
        .locator('.monitor-hardware, .monitor-hardware .monitor-panel-content')
        .evaluateAll((elements) =>
          elements.forEach((el) => el.removeAttribute('style')),
        )
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        await expect.poll(async () => (await measure()).pageFits).toBe(true)
        expect((await measure()).fits).toBe(true)
        if (width === 320) expect((await measure()).columns).toBe(1)
      }
    })
  }

  test(`${mode} Hardware wraps long names and includes unmatched accelerators`, async ({
    page,
  }) => {
    await mockApi(page, {
      'GET /api/stats': () => ({
        ...hardwareStats(3),
        layer_placement: {
          ...stats.layer_placement,
          devices: [
            ...stats.layer_placement.devices,
            { label: 'VULKAN0', name: 'Unmatched accelerator', layers: 0 },
          ],
        },
        gpu: {
          ...stats.gpu,
          devices: hardwareStats(3).gpu.devices.map((device) => ({
            ...device,
            name: 'AnExtremelyLongUnbrokenAcceleratorName'.repeat(3),
            power: 1234,
          })),
        },
      }),
    })
    await page.setViewportSize({ width: 2560, height: 1440 })
    await page.goto('/')
    await page.getByRole('button', { name: mode, exact: true }).click()
    await expect(page.locator('.monitor-placement')).toContainText('VULKAN0')
    for (const width of [2560, 1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1440 })
      await expect
        .poll(() =>
          page.locator('.hardware-grid').evaluate((el) => {
            const cards = Array.from(el.children)
            return (
              cards.every((card) =>
                Array.from(card.querySelectorAll<HTMLElement>('*')).every(
                  (child) => child.scrollWidth <= child.clientWidth + 1,
                ),
              ) && document.documentElement.scrollWidth <= innerWidth + 1
            )
          }),
        )
        .toBe(true)
      for (const badge of await page
        .locator('.monitor-gpu > div:first-child > div')
        .all()) {
        await expect(badge).toHaveText('1234 W')
        expect(
          await badge.evaluate((el) => getComputedStyle(el).whiteSpace),
        ).toBe('nowrap')
      }
      if (width === 2560) {
        const tops = await page
          .locator('.hardware-grid > *')
          .evaluateAll((cards) =>
            cards.map((card) => card.getBoundingClientRect().top),
          )
        expect(Math.max(...tops) - Math.min(...tops)).toBeLessThan(1)
      }
    }
  })

  test(`${mode} Hardware keeps four unloaded devices inline`, async ({
    page,
  }) => {
    await mockApi(page, {
      'GET /api/stats': () => ({
        ...hardwareStats(4),
        layer_placement: {
          status: 'no_model',
          devices: [],
          reason: 'No model loaded',
        },
      }),
    })
    await page.setViewportSize({ width: 2560, height: 1440 })
    await page.goto('/')
    await page.getByRole('button', { name: mode, exact: true }).click()
    await expect(page.locator('.layer-placement')).toHaveCount(4)
    await expect(page.locator('.layer-placement')).toContainText(
      Array(4).fill('No model loaded'),
    )
    const footer = page.locator('.monitor-hardware-footer')
    const alignment = await footer.evaluate((el) => {
      const note = el.querySelector('#placement-note')!
      const total = el.querySelector('#gpu-total-w')!
      return {
        offset: Math.abs(
          note.getBoundingClientRect().top - total.getBoundingClientRect().top,
        ),
        noteSize: getComputedStyle(note).fontSize,
        totalSize: getComputedStyle(total).fontSize,
      }
    })
    expect(alignment.offset).toBeLessThan(1)
    expect(alignment.noteSize).toBe(alignment.totalSize)
    await expect
      .poll(() =>
        page.locator('.hardware-grid > *').evaluateAll((cards) => {
          const tops = cards.map((card) => card.getBoundingClientRect().top)
          return Math.max(...tops) - Math.min(...tops)
        }),
      )
      .toBeLessThan(1)
    if (process.env.CAPTURE_UI === '1')
      await page
        .locator('.monitor-hardware')
        .screenshot({ path: `test-results/hardware-unloaded-${mode}.png` })
    await page.setViewportSize({ width: 320, height: 900 })
    await expect
      .poll(() =>
        footer.evaluate((el) => {
          const total = el
            .querySelector('#gpu-total-w')!
            .getBoundingClientRect()
          const note = el
            .querySelector('#placement-note')!
            .getBoundingClientRect()
          return (
            el.scrollWidth <= el.clientWidth + 1 && total.top >= note.bottom - 1
          )
        }),
      )
      .toBe(true)
  })
}

test('Fit trend labels and values fit with wider fonts at large density', async ({
  page,
}) => {
  await mockApi(page)
  await page.setViewportSize({ width: 2560, height: 1440 })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu')).toHaveCount(1)
  // Fit can grow text to 21.6px; Arial also exercises wider glyphs than the
  // Windows system font used by local browser tests.
  await page.addStyleTag({
    content:
      '.monitor-gpu-trend { font: 21.6px Arial, sans-serif !important; }',
  })
  const overflows = await page
    .locator('.monitor-gpu-trend > span')
    .evaluateAll((spans) =>
      spans
        .filter((span) => span.scrollWidth > span.clientWidth + 1)
        .map(
          (span) =>
            `${span.textContent}: ${span.scrollWidth} > ${span.clientWidth}`,
        ),
    )
  expect(overflows).toEqual([])
})

test('Hardware preserves 60-sample histories when cards reflow', async ({
  page,
}) => {
  test.setTimeout(60_000)
  await page.clock.install()
  let loaded = true
  let sample = 0
  await mockApi(page, {
    'GET /api/stats': () => {
      const data = hardwareStats(4)
      sample++
      return {
        ...data,
        layer_placement: loaded
          ? {
              ...stats.layer_placement,
              devices: [
                { label: 'CPU', layers: 0 },
                ...[0, 1, 2].map((index) => ({
                  label: `CUDA${index}`,
                  gpu_index: index,
                  layers: 16,
                })),
              ],
            }
          : { status: 'no_model', devices: [], reason: 'No model loaded' },
        gpu: {
          ...data.gpu,
          devices: data.gpu.devices.map((device) => ({
            ...device,
            temp: 40 + (sample % 5),
            util_gpu: (sample % 7) * 10,
            power: 30 + (sample % 7) * 20,
          })),
        },
      }
    },
  })
  await page.setViewportSize({ width: 2560, height: 1440 })
  await page.goto('/')
  const historyLength = () =>
    page
      .locator('.monitor-gpu svg')
      .first()
      .evaluate(
        (el) =>
          el
            .querySelector('polyline')
            ?.getAttribute('points')
            ?.trim()
            .split(/\s+/).length ?? 1,
      )
  await expect(page.locator('.monitor-gpu')).toHaveCount(3)
  for (let count = 2; count <= 60; count++) {
    await page.clock.fastForward(1100)
    await expect.poll(historyLength).toBeGreaterThanOrEqual(count)
  }
  for (const state of ['loaded', 'unloaded']) {
    loaded = state === 'loaded'
    await page.clock.fastForward(1100)
    if (!loaded)
      await expect(page.locator('.layer-placement')).toContainText(
        Array(4).fill('No model loaded'),
      )
    for (const mode of ['Fit', 'Comfortable']) {
      await page.getByRole('button', { name: mode, exact: true }).click()
      if (process.env.CAPTURE_UI === '1')
        await page.locator('.monitor-hardware').screenshot({
          path: `test-results/hardware-history-${state}-${mode}.png`,
        })
      await page.setViewportSize({ width: 390, height: 900 })
      expect(await historyLength()).toBe(60)
      expect(
        await page
          .locator('.monitor-gpu svg')
          .first()
          .evaluate((el) => el.getBoundingClientRect().width),
      ).toBeGreaterThan(0)
      await page.setViewportSize({ width: 2560, height: 1440 })
      expect(await historyLength()).toBe(60)
    }
  }
})

test('Fit redistributes spare height into text, padding, and charts on live resize', async ({
  page,
}) => {
  await mockApi(page)
  await page.setViewportSize({ width: 1920, height: 900 })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu')).toHaveCount(1)
  const measure = () =>
    page.evaluate(() => {
      const card = document.querySelector('.monitor-gpu')!
      const row = card.querySelector('.monitor-row > span')!
      return {
        font: parseFloat(getComputedStyle(row).fontSize),
        padding: parseFloat(getComputedStyle(card).paddingTop),
        chart: document
          .querySelector('.monitor-decode .monitor-spark')!
          .getBoundingClientRect().height,
        bottom: document
          .querySelector('.monitor-hardware')!
          .getBoundingClientRect().bottom,
        scroll: document.documentElement.scrollHeight,
      }
    })
  const compact = await measure()
  await page.setViewportSize({ width: 1920, height: 1200 })
  await expect
    .poll(async () => (await measure()).font)
    .toBeGreaterThan(compact.font + 0.5)
  const roomy = await measure()
  expect(roomy.padding).toBeGreaterThan(compact.padding + 1)
  expect(roomy.chart).toBeGreaterThan(compact.chart + 5)
  expect(roomy.bottom).toBeGreaterThan(1080)
  expect(roomy.scroll).toBeLessThanOrEqual(1200)
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/monitor-adaptive-fit.png',
      fullPage: true,
    })
  await page.setViewportSize({ width: 1920, height: 900 })
  await expect
    .poll(async () => (await measure()).scroll)
    .toBeLessThanOrEqual(900)
  expect((await measure()).font).toBeCloseTo(compact.font, 1)
  await page.getByRole('button', { name: 'Comfortable', exact: true }).click()
  await page.setViewportSize({ width: 1920, height: 1200 })
  const comfortable = await measure()
  await page.setViewportSize({ width: 1920, height: 1400 })
  expect((await measure()).font).toBe(comfortable.font)
  expect((await measure()).padding).toBe(comfortable.padding)
  await page.getByRole('button', { name: 'Fit', exact: true }).click()
  await page.setViewportSize({ width: 1920, height: 650 })
  await page.evaluate(() => window.scrollTo(0, 100))
  await page.setViewportSize({ width: 1920, height: 900 })
  await expect
    .poll(async () => (await measure()).scroll)
    .toBeLessThanOrEqual(900)
})

test('Fit refits when the hardware count changes at runtime', async ({
  page,
}) => {
  let gpuCount = 1
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      gpu: {
        ...stats.gpu,
        devices: Array.from({ length: gpuCount }, (_, index) => ({
          ...stats.gpu.devices[0],
          index,
        })),
      },
    }),
  })
  await page.setViewportSize({ width: 1920, height: 1200 })
  await page.goto('/')
  await expect(page.locator('.monitor-gpu')).toHaveCount(1)
  gpuCount = 3
  await expect(page.locator('.monitor-gpu')).toHaveCount(3)
  await expect
    .poll(() =>
      page
        .locator('.hardware-grid')
        .evaluate((el) => el.scrollHeight <= el.clientHeight + 1),
    )
    .toBe(true)
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollHeight))
    .toBeLessThanOrEqual(1200)
  await page.getByRole('link', { name: 'Manage', exact: true }).click()
  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollHeight))
    .toBeLessThanOrEqual(1200)
})

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
  await expect(page.locator('#gpu-total-w')).toHaveCount(0)
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
