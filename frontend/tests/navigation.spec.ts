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
