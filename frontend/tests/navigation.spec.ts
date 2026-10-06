import { expect, test } from '@playwright/test'
import { config, historyPage, launcherState, mockApi, stats } from './fixtures'

test('hash navigation keeps an unsaved draft and supports Back', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/')
  await page.locator('#manage-open').click()
  await page.locator('#lx-name').fill('Edited name')
  await page.locator('#history-open').click()
  await expect(page).toHaveURL(/#history$/)
  await page.goBack()
  await expect(page).toHaveURL(/#manage$/)
  await expect(page.locator('#lx-name')).toHaveValue('Edited name')
  await expect(page.locator('#lx-dirty-mark')).toBeVisible()
})

test('manage actions and default setting keep server state visible', async ({
  page,
}) => {
  const calls = await mockApi(page, {
    'POST /api/configs/default': () => ({
      ...launcherState,
      settings: { ...launcherState.settings, default_config: null },
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-default')).toContainText('Default')
  await page.locator('#lx-default').click()
  await expect
    .poll(() => calls.includes('POST /api/configs/default'))
    .toBe(true)
  await page.locator('#lx-launch').click()
  await expect(page.getByText('Running · Qwen test')).toBeVisible()
  await page.locator('#lx-restart').click()
  await expect
    .poll(() => calls.includes('POST /api/launcher/restart'))
    .toBe(true)
  await page.locator('#lx-stop').click()
  await expect.poll(() => calls.includes('POST /api/launcher/stop')).toBe(true)
})

test('flag descriptions, collapse preference and keyboard entry work', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/#manage')
  await expect(page.locator('.lx-flag-row').first()).toContainText(
    'Context size',
  )
  await page.locator('#lx-flags-label').click()
  await expect(page.locator('#lx-flags-summary')).toContainText('-fa')
  await page.reload()
  await expect(page.locator('#lx-flags-summary')).toBeVisible()
  await page.locator('#lx-flags-summary').click()
  await page.locator('#lx-flag-pick').fill('ctx')
  await page.locator('#lx-flag-pick').press('Enter')
  await expect(page.locator('.lx-flag-row')).toHaveCount(3)
  await page.locator('#lx-flag-pick').focus()
  await page.locator('#lx-flag-pick').press('ArrowDown')
  await page.locator('#lx-flag-pick').press('Enter')
  await expect(
    page.locator('.lx-flag-row').last().locator('.lx-flag'),
  ).toHaveValue('-fa')
})

test('navigation, help and dialogs work from the keyboard', async ({
  page,
}) => {
  await mockApi(page)
  await page.goto('/')
  const help = page.getByRole('button', { name: 'More about Session' })
  await help.focus()
  await expect(page.getByRole('tooltip')).toBeVisible()
  await page.keyboard.press('Escape')
  const manage = page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('link', { name: 'Manage' })
  await manage.focus()
  await page.keyboard.press('Enter')
  await expect(manage).toHaveAttribute('aria-current', 'page')
  await page.locator('#lx-delete').click()
  await expect(page.getByRole('dialog')).toContainText('Delete configuration?')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('#lx-delete')).toBeFocused()
})

test('history cursor paging and coverage warning remain visible', async ({
  page,
}) => {
  const calls = await mockApi(page, {
    'GET /api/history': (req) => {
      const cursor = new URL(req.url()).searchParams.get('cursor')
      return {
        ...historyPage,
        items: cursor ? [] : historyPage.items,
        next_cursor: cursor ? null : 'opaque-next',
        log_status: { ...historyPage.log_status, gap: true },
      }
    },
  })
  await page.goto('/#history')
  await expect(page.locator('#history-status')).toContainText('Coverage gap')
  await page.locator('#history-next').click()
  await expect
    .poll(() => calls.some((call) => call.includes('cursor=opaque-next')))
    .toBe(true)
  await expect(page.locator('#history-body')).toContainText(
    'No observed generations',
  )
  await page.locator('#history-prev').click()
  await expect(page.locator('#history-body')).toContainText('Qwen test')
})

test('narrow viewport keeps navigation and primary information accessible', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockApi(page)
  await page.goto('/')
  await expect(
    page.getByRole('navigation', { name: 'Main navigation' }),
  ).toBeVisible()
  await expect(page.locator('#s-model')).toBeVisible()
  await expect(page.locator('#t-decode')).toBeVisible()
  await page.locator('#manage-open').click()
  await expect(page.locator('#lx-launch')).toBeVisible()
  await page.locator('#history-open').click()
  await expect(page.locator('#history-state')).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true)
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/history-mobile.png',
      fullPage: true,
    })
})

test('Fit shows every Monitor panel in a 1440 × 900 viewport and Comfortable persists', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Fit' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await expect(page.locator('.monitor-panel')).toHaveCount(6)
  const panelBottom = await page
    .locator('.monitor-panel')
    .evaluateAll((panels) =>
      Math.max(...panels.map((panel) => panel.getBoundingClientRect().bottom)),
    )
  expect(panelBottom).toBeLessThanOrEqual(900)
  expect(panelBottom).toBeGreaterThan(850)
  for (const [width, height] of [
    [1440, 900],
    [1920, 1080],
    [2560, 1440],
    [3840, 2160],
  ]) {
    await page.setViewportSize({ width, height })
    if (process.env.CAPTURE_UI === '1')
      await page.screenshot({
        path: `test-results/monitor-fit-${width}.png`,
      })
    const bottom = await page
      .locator('.monitor-gpus')
      .evaluate((panel) => panel.getBoundingClientRect().bottom)
    expect(bottom).toBeGreaterThan(height * 0.88)
    expect(bottom).toBeLessThanOrEqual(height)
    const rowsOverlap = await page.evaluate(() => {
      const features = [...document.querySelectorAll('.monitor-feature')]
      const panels = document.querySelector('.monitor-panels')!
      return features.some(
        (feature) =>
          feature.getBoundingClientRect().bottom >
          panels.getBoundingClientRect().top,
      )
    })
    expect(rowsOverlap).toBe(false)
  }

  await page.getByRole('button', { name: 'Comfortable' }).click()
  await expect(page.locator('.monitor-comfortable')).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('button', { name: 'Comfortable' }),
  ).toHaveAttribute('aria-pressed', 'true')
  await page.setViewportSize({ width: 390, height: 844 })
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/monitor-comfortable-mobile.png',
      fullPage: true,
    })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true)
  await page.getByRole('button', { name: 'Fit' }).click()
  await expect(page.getByRole('button', { name: 'Fit' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  if (process.env.CAPTURE_UI === '1')
    await page.screenshot({
      path: 'test-results/monitor-fit-mobile.png',
      fullPage: true,
    })
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true)
})

test('Fit reflows a four-slot, three-GPU dashboard without horizontal overflow', async ({
  page,
}) => {
  const devices = [
    {
      ...stats.gpu.devices[0],
      index: 0,
      name: 'NVIDIA GeForce RTX 4070 SUPER',
    },
    { ...stats.gpu.devices[0], index: 1, name: 'NVIDIA GeForce RTX 3060 Ti' },
    { ...stats.gpu.devices[0], index: 2, name: 'NVIDIA GeForce RTX 5060 Ti' },
  ]
  await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      model: { ...stats.model, total_slots: 4 },
      slots: {
        busy: 1,
        total: 4,
        list: [
          ...stats.slots.list,
          { id: 2, state: 'idle', ctx_used: 0, ctx_ratio: 0 },
          { id: 3, state: 'idle', ctx_used: 0, ctx_ratio: 0 },
        ],
      },
      gpu: { ok: true, devices },
    }),
  })
  for (const [width, height] of [
    [1920, 1080],
    [3840, 2005],
  ]) {
    await page.setViewportSize({ width, height })
    await page.goto('/')
    await expect(page.locator('.monitor-gpu')).toHaveCount(3)
    await expect(page.locator('.monitor-slot')).toHaveCount(4)
    await expect(page.locator('#status')).toContainText('llama-server online')
    const panelBottom = await page
      .locator('.monitor-gpus')
      .evaluate((panel) => panel.getBoundingClientRect().bottom)
    expect(panelBottom).toBeLessThanOrEqual(height)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true)
    if (process.env.CAPTURE_UI === '1')
      await page.screenshot({
        path: `test-results/monitor-multi-${width}.png`,
      })
  }
})

test('save as new during a guarded switch preserves the saved draft', async ({
  page,
}) => {
  let saved: any
  await mockApi(page, {
    'POST /api/configs': (req) => {
      saved = req.postDataJSON()
      return { configs: [config, saved] }
    },
  })
  await page.goto('/#manage')
  await page.locator('#lx-model').fill('C:/Models/second.gguf')
  await page.locator('#lx-config').click()
  await page.getByRole('option', { name: 'New configuration' }).click()
  await page
    .locator('#lx-modal')
    .getByRole('button', { name: 'Save as new' })
    .click()
  await page.locator('#lx-name-input').fill('Second model')
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Save', exact: true })
    .click()
  await expect.poll(() => saved?.name).toBe('Second model')
  expect(saved.model_path).toBe('C:/Models/second.gguf')
  await expect(page.locator('#lx-model')).toHaveValue('')
})

test('binary browsing updates settings and supported flags', async ({
  page,
}) => {
  let settings: any
  const calls = await mockApi(page, {
    'GET /api/browse': (req) => {
      const url = new URL(req.url())
      return {
        path: url.searchParams.get('path') || 'C:/bin',
        parent: 'C:/',
        dirs: [],
        files: ['C:/bin/llama-server-new.exe'],
      }
    },
    'POST /api/launcher/settings': (req) => {
      settings = req.postDataJSON()
      return {
        ...launcherState,
        settings: { ...launcherState.settings, ...settings },
      }
    },
  })
  await page.goto('/#manage')
  await page.locator('#lx-bin-browse').click()
  await page.getByRole('dialog').getByText('llama-server-new.exe').click()
  await expect
    .poll(() => settings?.llama_server_path)
    .toBe('C:/bin/llama-server-new.exe')
  await expect(page.locator('#lx-bin-path')).toContainText(
    'llama-server-new.exe',
  )
  expect(
    calls.filter((call) => call === 'GET /api/launcher/flags').length,
  ).toBeGreaterThanOrEqual(2)
})

test('saved configuration does not warn merely because a server is running', async ({
  page,
}) => {
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      status: { state: 'running', config_name: config.name },
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-config')).toContainText(config.name)
  const warned = await page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(warned).toBe(false)
})

test('older saved configurations load with current form defaults', async ({
  page,
}) => {
  const older = {
    name: 'Older model',
    model_path: 'C:/Models/old.gguf',
    flags: [{ flag: '-c', value: '2048' }],
  }
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      settings: { ...launcherState.settings, default_config: older.name },
      configs: [older],
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-config')).toContainText('Older model')
  await expect(page.locator('#lx-port')).toHaveValue('8001')
  await expect(page.locator('#lx-log-prompts')).not.toBeChecked()
  await expect(page.locator('#lx-dirty-mark')).toHaveCount(0)
})

test('built page works with the real isolated FastAPI API', async ({
  page,
}) => {
  test.setTimeout(30_000)
  await page.goto('/')
  await expect(page.locator('#status')).toContainText('unreachable', {
    timeout: 20_000,
  })
  await page.locator('#manage-open').click()
  await expect(page.getByText('Launch & manage')).toBeVisible()
  await page.locator('#history-open').click()
  await expect(page.locator('#history-open')).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.locator('#history-body')).toContainText(
    'No observed generations',
  )
})

test('visual review captures monitor, manage and history', async ({ page }) => {
  test.skip(process.env.CAPTURE_UI !== '1')
  test.setTimeout(40_000)
  await mockApi(page)
  await page.goto('/')
  await expect(page.locator('#s-model')).toHaveText(stats.model.name)
  await page.screenshot({
    path: 'test-results/monitor-desktop.png',
    fullPage: true,
  })
  await page.locator('#manage-open').click()
  await expect(page.locator('#manage-open')).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.screenshot({
    path: 'test-results/manage-desktop.png',
    fullPage: true,
  })
  await page.locator('#history-open').click()
  await expect(page.locator('#history-open')).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.locator('#history-body')).toContainText(config.name)
  await page.screenshot({
    path: 'test-results/history-desktop.png',
    fullPage: true,
  })
  await page.setViewportSize({ width: 820, height: 1180 })
  await page.screenshot({
    path: 'test-results/history-tablet.png',
    fullPage: true,
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({
    path: 'test-results/history-mobile.png',
    fullPage: true,
  })
  await page.locator('#manage-open').click()
  await expect(page.locator('#manage-open')).toHaveAttribute(
    'aria-current',
    'page',
  )
  await page.screenshot({
    path: 'test-results/manage-mobile.png',
    fullPage: true,
  })
  await page
    .getByRole('navigation', { name: 'Main navigation' })
    .getByRole('link', { name: 'Monitor' })
    .click()
  await expect(
    page
      .getByRole('navigation', { name: 'Main navigation' })
      .getByRole('link', { name: 'Monitor' }),
  ).toHaveAttribute('aria-current', 'page')
  await page.screenshot({
    path: 'test-results/monitor-mobile.png',
    fullPage: true,
  })
})

test('visual review captures offline, empty and error states', async ({
  page,
}) => {
  test.skip(process.env.CAPTURE_UI !== '1')
  await mockApi(page, {
    'GET /api/stats': () => ({ online: false, active: false }),
    'GET /api/history': () => ({
      ...historyPage,
      items: [],
      models: [],
    }),
  })
  await page.goto('/')
  await expect(page.locator('#status')).toContainText('unreachable')
  await page.screenshot({
    path: 'test-results/monitor-offline.png',
    fullPage: true,
  })
  await page.locator('#history-open').click()
  await expect(page.locator('#history-open')).toHaveAttribute(
    'aria-current',
    'page',
  )
  await expect(page.locator('#history-body')).toContainText(
    'No observed generations',
  )
  await page.screenshot({
    path: 'test-results/history-empty.png',
    fullPage: true,
  })
  await page.unroute('**/api/**')
  await mockApi(page, {
    'GET /api/stats': () => ({ online: false, active: false }),
    'GET /api/history': () => ({
      status: 500,
      body: { error: 'Test failure' },
    }),
  })
  await page.reload()
  await expect(page.locator('#history-status')).toContainText('Test failure')
  await page.screenshot({
    path: 'test-results/history-error.png',
    fullPage: true,
  })
})
