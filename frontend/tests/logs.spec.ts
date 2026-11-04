import { expect, test } from '@playwright/test'
import { mockApi } from './fixtures'

test('console selects prior runs and resets after retained output is evicted', async ({
  page,
}) => {
  const old = 'a'.repeat(32)
  let reads = 0
  await mockApi(page, {
    'GET /api/launcher/log-runs': () => ({
      items: [
        {
          run_id: old,
          config_name: 'Previous model',
          started_at: 1000,
          ended_at: 1100,
          state: 'exited',
          stop_reason: 'unexpected_exit',
          exit_code: 7,
          gap: true,
          logging_error: null,
          startup_truncated: false,
        },
      ],
      next_cursor: null,
    }),
    'GET /api/launcher/console': (request) => {
      const url = new URL(request.url())
      const historical = url.searchParams.get('run_id') === old
      const first = !url.searchParams.get('cursor')
      if (historical) reads += 1
      return {
        available: true,
        content: historical
          ? first
            ? 'prior loader error\n'
            : reads === 2
              ? 'retained tail\n'
              : ''
          : 'current output\n',
        offset: reads + 100,
        size: 200,
        source: 'filtered',
        path: '/data/logs',
        cursor: 'next',
        reset: historical && reads === 2,
        gap: historical,
      }
    },
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  await page.getByRole('radio', { name: 'Traces', exact: true }).check()
  await expect(page.locator('#console-out')).toContainText('current output')
  await page.getByLabel('Model run').selectOption(old)
  await expect(page.locator('#console-out')).toContainText('prior loader error')
  await expect(page.getByRole('status')).toContainText('no longer retained')
  await expect(page.locator('#console-out')).toContainText('retained tail')
  await expect(page.locator('#console-out')).not.toContainText(
    'prior loader error',
  )
  await expect(page.getByLabel('Model run')).toContainText('exit 7')
})

test('console bounds accumulated text and displays capture errors', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/console': () => ({
      available: true,
      content: 'x'.repeat(600 * 1024) + '\nfinal warning\n',
      offset: 700000,
      source: 'filtered',
      path: '/data/logs',
      cursor: 'next',
      error: 'Disk is full',
    }),
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  await expect(page.locator('#console-out')).toContainText('final warning')
  await expect(page.locator('#console-out')).toContainText(
    'Earlier output omitted',
  )
  expect(
    (await page.locator('#console-out').textContent())!.length,
  ).toBeLessThan(525000)
  await expect(page.getByRole('status')).toContainText('Disk is full')
})

test('console filters metadata locally and keeps diagnostic continuations across polls', async ({
  page,
}) => {
  await page.clock.install({ time: new Date('2026-11-04T12:00:00Z') })
  await page.clock.pauseAt(new Date('2026-11-04T12:00:01Z'))
  let entries = [
    { text: 'device allocation detail', type: 'log', level: 'debug' },
    { text: 'future informational detail', type: 'log', level: 'info' },
    { text: 'signal handler installed', type: 'log', level: 'info' },
    { text: 'Server launched.', type: 'lifecycle' },
    { text: 'Model ready.', type: 'lifecycle' },
    { text: 'Server exited: code 0; reason manual_stop.', type: 'lifecycle' },
    { text: 'slot processing task', level: 'debug' },
    { text: 'slot print_timing: total time = 20 ms', level: 'debug' },
    { text: 'structured diagnostic', level: 'WARNING' },
    {
      text: 'Server exited: code 7; reason unexpected_exit.',
      type: 'lifecycle',
    },
    {
      text: '\u001b[31mCUDA allocation failed\u001b[0m\nadditional allocation context',
      level: 'info',
    },
  ]
  const requests: URL[] = []
  await mockApi(page, {
    'GET /api/launcher/console': (request) => {
      requests.push(new URL(request.url()))
      const batch = entries
      entries = []
      return {
        available: true,
        content: batch.map((entry) => entry.text).join('\n'),
        entries: batch,
        offset: requests.length * 100,
        source: 'filtered',
        path: '/data/logs',
        cursor: 'cursor-' + requests.length,
      }
    },
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  const out = page.locator('#console-out')
  const events = page.getByRole('radio', { name: 'Events', exact: true })
  const errors = page.getByRole('radio', { name: 'Errors', exact: true })
  const traces = page.getByRole('radio', { name: 'Traces', exact: true })
  await expect(events).toBeChecked()
  await expect(out).toContainText('Model ready.')
  await expect(out).toContainText('total time = 20 ms')
  await expect(out).toContainText('structured diagnostic')
  await expect(out).not.toContainText('allocation detail')
  await expect(out).not.toContainText('future informational detail')
  await events.focus()
  await page.keyboard.press('ArrowRight')
  await expect(errors).toBeChecked()
  await expect(out).toContainText('CUDA allocation failed')
  await expect(out).toContainText('additional allocation context')
  await expect(out).not.toContainText('signal handler installed')
  await expect(out).toContainText('Server exited: code 7')
  await expect(out).not.toContainText('Server launched.')
  await expect(out).not.toContainText('Server exited: code 0')
  await expect(out).not.toContainText('processing task')
  await traces.check()
  await expect(out).toContainText('device allocation detail')
  await expect(out).not.toContainText('\u001b')
  expect(requests).toHaveLength(1)
  await errors.check()
  entries = [{ text: '    driver stack frame', level: 'info' }]
  await page.clock.runFor(1000)
  await expect(out).toContainText('driver stack frame')
  expect(requests[1].searchParams.get('cursor')).toBe('cursor-1')
  entries = [{ text: 'ordinary next record', level: 'info' }]
  await page.clock.runFor(1000)
  await traces.check()
  await expect(out).toContainText('ordinary next record')
  await errors.check()
  await expect(out).not.toContainText('ordinary next record')
  await page.locator('#console-close').click()
  await page.locator('#lx-console').click()
  await expect(events).toBeChecked()
})

for (const width of [390, 1440]) {
  test(`console filters fit and preserve scroll-follow behavior at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.clock.install({ time: new Date('2026-11-04T12:00:00Z') })
    await page.clock.pauseAt(new Date('2026-11-04T12:00:01Z'))
    let entries = Array.from({ length: 150 }, (_, index) => ({
      text: `processing task ${index}`,
    }))
    await mockApi(page, {
      'GET /api/launcher/console': () => {
        const batch = entries
        entries = []
        return {
          available: true,
          entries: batch,
          content: '',
          offset: 100,
          cursor: 'next',
          path: '/data/logs',
        }
      },
    })
    await page.goto('/#monitor')
    await page.locator('#lx-console').click()
    const out = page.locator('#console-out')
    await expect(out).toContainText('processing task 149')
    await page.clock.runFor(50)
    expect(
      await out.evaluate(
        (element) =>
          element.scrollHeight - element.clientHeight - element.scrollTop,
      ),
    ).toBeLessThan(24)
    const box = await page
      .getByRole('group', { name: 'Output level' })
      .boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(
      await out.evaluate((element) => element.clientHeight),
    ).toBeGreaterThan(100)
    await out.evaluate((element) => {
      element.scrollTop = 0
    })
    await out.dispatchEvent('scroll')
    entries = [{ text: 'processing task while scrolled up' }]
    await page.clock.runFor(1000)
    await expect(out).toContainText('while scrolled up')
    await page.clock.runFor(50)
    expect(await out.evaluate((element) => element.scrollTop)).toBe(0)
    await out.evaluate((element) => {
      element.scrollTop = element.scrollHeight
    })
    await out.dispatchEvent('scroll')
    entries = [{ text: 'processing task while following' }]
    await page.clock.runFor(1000)
    await expect(out).toContainText('while following')
    await page.clock.runFor(50)
    expect(
      await out.evaluate(
        (element) =>
          element.scrollHeight - element.clientHeight - element.scrollTop,
      ),
    ).toBeLessThan(24)
    await page.screenshot({ path: `test-results/console-filters-${width}.png` })
  })
}

test('console clears hidden output and continuation context on empty resets and source changes', async ({
  page,
}) => {
  await page.clock.install({ time: new Date('2026-11-04T12:00:00Z') })
  await page.clock.pauseAt(new Date('2026-11-04T12:00:01Z'))
  let response = {
    available: true,
    content: 'old hidden trace\nold error\n',
    entries: [
      { text: 'old hidden trace' },
      { text: 'old error', level: 'error' },
    ],
    offset: 100,
    source: 'filtered',
    path: '/data/managed',
    cursor: 'next',
    reset: false,
    error: '',
    gap: false,
  }
  await mockApi(page, { 'GET /api/launcher/console': () => response })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  const out = page.locator('#console-out')
  await expect(out).toContainText('old error')
  response = { ...response, content: '', entries: [], reset: true, gap: true }
  await page.clock.runFor(1000)
  await expect(out).toHaveText('(log is empty)')
  await page.getByRole('radio', { name: 'Traces', exact: true }).check()
  await expect(out).not.toContainText('old hidden trace')
  await expect(page.getByRole('status')).toContainText('no longer retained')
  response = {
    ...response,
    content: 'new ordinary detail\n',
    entries: [{ text: '    new ordinary detail' }],
    reset: false,
    gap: false,
    offset: 200,
  }
  await page.clock.runFor(1000)
  await expect(out).toContainText('new ordinary detail')
  await page.getByRole('radio', { name: 'Events', exact: true }).check()
  await expect(out).toHaveText('No output matches this filter')
  response = {
    ...response,
    entries: [{ text: 'another error', level: 'error' }],
    offset: 250,
  }
  await page.clock.runFor(1000)
  await expect(out).toContainText('another error')
  response = {
    ...response,
    content: 'external frame\n',
    entries: [{ text: '    external frame' }],
    source: 'external',
    path: '/data/external',
    offset: 300,
    error: 'Capture unavailable',
  }
  await page.clock.runFor(1000)
  await expect(out).toHaveText('No output matches this filter')
  await expect(page.getByRole('status')).toHaveText('Capture unavailable')
  await page.getByRole('radio', { name: 'Traces', exact: true }).check()
  await expect(out).toContainText('external frame')
  await expect(out).not.toContainText('new ordinary detail')
  response = {
    ...response,
    content: '',
    entries: [],
    path: '/data/replacement',
    offset: 400,
  }
  await page.clock.runFor(1000)
  await expect(out).toHaveText('(log is empty)')
})

test('console preserves the filter across run selection and bounds hidden records before filtering', async ({
  page,
}) => {
  const old = 'd'.repeat(32)
  await mockApi(page, {
    'GET /api/launcher/log-runs': () => ({
      items: [
        {
          run_id: old,
          config_name: 'Older model',
          started_at: 1000,
          state: 'exited',
          exit_code: 0,
        },
      ],
      next_cursor: null,
    }),
    'GET /api/launcher/console': (request) => {
      const historical =
        new URL(request.url()).searchParams.get('run_id') === old
      const entries = historical
        ? [{ text: 'prior run warning', level: 'warn' }]
        : [
            { text: 'early warning evicted by hidden records', level: 'warn' },
            { text: 'x'.repeat(600 * 1024), level: 'debug' },
            { text: 'latest warning', level: 'warn' },
          ]
      return {
        available: true,
        entries,
        content: entries.map((entry) => entry.text).join('\n'),
        offset: 700000,
        source: 'filtered',
        path: '/data/logs',
        cursor: 'next',
      }
    },
  })
  await page.goto('/#monitor')
  await page.locator('#lx-console').click()
  const out = page.locator('#console-out')
  await expect(out).toContainText('latest warning')
  await expect(out).toContainText('Earlier output omitted')
  await expect(out).not.toContainText('early warning')
  await page.getByRole('radio', { name: 'Traces', exact: true }).check()
  await expect(out).toContainText('xxxx')
  expect((await out.textContent())!.length).toBeLessThan(525000)
  await page.getByRole('radio', { name: 'Errors', exact: true }).check()
  await page.getByLabel('Model run').selectOption(old)
  await expect(
    page.getByRole('radio', { name: 'Errors', exact: true }),
  ).toBeChecked()
  await expect(out).toContainText('prior run warning')
  await expect(out).not.toContainText('latest warning')
  await page.getByRole('radio', { name: 'Traces', exact: true }).check()
  await expect(out).not.toContainText('xxxx')
})
