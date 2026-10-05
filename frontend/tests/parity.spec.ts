import { expect, test } from '@playwright/test'
import { config, launcherState, mockApi, stats } from './fixtures'

for (const enabled of [true, false]) {
  test(`reload restores running prompt logging (${enabled}) over saved config`, async ({
    page,
  }) => {
    const running = { ...config, log_prompts: enabled }
    const calls = await mockApi(page, {
      'GET /api/launcher/state': () => ({
        ...launcherState,
        configs: [{ ...config, log_prompts: !enabled }],
        status: {
          state: 'running',
          adopted: true,
          config_name: config.name,
          config: running,
        },
      }),
    })
    await page.goto('/#manage')
    await expect(page.locator('#lx-log-prompts')).toBeChecked({
      checked: enabled,
    })
    await expect(page.locator('#lx-dirty-mark')).toBeVisible()
    // Polling must not overwrite subsequent form edits.
    const reads = () =>
      calls.filter((c) => c === 'GET /api/launcher/state').length
    const initialReads = reads()
    await page.locator('#lx-log-prompts').click()
    await expect.poll(reads).toBeGreaterThan(initialReads)
    await expect(page.locator('#lx-log-prompts')).toBeChecked({
      checked: !enabled,
    })
    await page.reload()
    await expect(page.locator('#lx-log-prompts')).toBeChecked({
      checked: enabled,
    })
  })
}

test('reload restores an unnamed running configuration', async ({ page }) => {
  await mockApi(page, {
    'GET /api/launcher/state': () => ({
      ...launcherState,
      status: {
        state: 'running',
        config_name: '',
        config: { ...config, name: '', log_prompts: true, port: 9001 },
      },
    }),
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-log-prompts')).toBeChecked()
  await expect(page.locator('#lx-model')).toHaveValue(config.model_path)
  await expect(page.locator('#lx-port')).toHaveValue('9001')
  await expect(page.locator('#lx-dirty-mark')).toBeVisible()
})

test('saved prompt logging survives reload with the server stopped', async ({
  page,
}) => {
  let saved = { ...config }
  await mockApi(page, {
    'GET /api/launcher/state': () => ({ ...launcherState, configs: [saved] }),
    'POST /api/configs': (request) => {
      saved = request.postDataJSON()
      return { configs: [saved] }
    },
  })
  await page.goto('/#manage')
  await expect(page.locator('#lx-config')).toContainText(config.name)
  for (const enabled of [true, false]) {
    await page.locator('#lx-log-prompts').click()
    await page.locator('#lx-save').click()
    await expect(page.locator('#lx-dirty-mark')).toBeHidden()
    await page.reload()
    await expect(page.locator('#lx-log-prompts')).toBeChecked({
      checked: enabled,
    })
  }
})

test('monitor presents every telemetry group and status', async ({ page }) => {
  const calls = await mockApi(page)
  await page.goto('/')
  await expect(page.locator('#status')).toContainText('online')
  await expect(page.locator('#s-model')).toHaveText('Qwen test')
  await expect(page.locator('#s-ctx')).toHaveText('4,096')
  await expect(page.locator('#s-slots')).toHaveText('1 / 2')
  await expect(page.locator('#s-deferred')).toHaveText('2')
  await expect(page.locator('#s-kv-pct')).toContainText('983')
  await expect(page.locator('#t-decode')).toHaveText('39.4')
  await expect(page.locator('#t-pp')).toContainText('120.2')
  await expect(page.locator('#t-spec')).toContainText('2.40 tok/step')
  await expect(page.locator('#lr-total')).toContainText('0.80s')
  await expect(page.locator('#split-legend')).toContainText('CUDA0')
  await expect(page.locator('#slots')).toContainText('Slot 0')
  await expect(page.locator('#gpus')).toContainText('Test GPU')
  await expect(page.locator('#sysmem')).toContainText('llama-server')
  expect(calls).toContain('GET /api/stats')
})

test('offline and missing metrics remain understandable; idle polls are lite', async ({
  page,
}) => {
  const calls = await mockApi(page, {
    'GET /api/stats': () => ({
      ...stats,
      active: false,
      log_mode: false,
      metrics_enabled: false,
    }),
  })
  await page.goto('/')
  await expect(page.locator('#status')).toContainText('--metrics')
  await expect(page.locator('#updated')).toContainText('idle (http)')
  await expect
    .poll(() => calls.some((call) => call === 'GET /api/stats?lite=1'))
    .toBe(true)
  await page.reload()
  await page.route('**/api/stats*', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ ...stats, online: false, active: false }),
    }),
  )
  await expect(page.locator('#status')).toContainText('unreachable')
})

test('configuration form preserves flag order and disabled state when saved', async ({
  page,
}) => {
  let saved: any
  await mockApi(page, {
    'POST /api/configs': (req) => {
      saved = req.postDataJSON()
      return { configs: [saved] }
    },
  })
  await page.goto('/')
  await page.locator('#manage-open').click()
  await expect(page.locator('#lx-config')).toContainText('Qwen test')
  await expect(page.locator('.lx-flag-row')).toHaveCount(2)
  await expect(
    page.locator('.lx-flag-row').nth(1).locator('.lx-en'),
  ).not.toBeChecked()
  await page.locator('#lx-flag-pick').fill('ctx')
  await expect(page.locator('#lx-flag-list')).toContainText('Context size')
  await page.locator('#lx-flag-pick').fill('--custom-opt')
  await page.locator('#lx-flag-add').click()
  await page
    .locator('.lx-flag-row')
    .nth(2)
    .locator('.lx-val')
    .fill('custom value')
  await page.locator('#lx-save').click()
  await expect.poll(() => saved?.flags.length).toBe(3)
  expect(saved.flags).toEqual([
    ...config.flags,
    { flag: '--custom-opt', value: 'custom value' },
  ])
})

test('dirty switch guard and file browser keep draft safe', async ({
  page,
}) => {
  const calls = await mockApi(page)
  await page.goto('/')
  await page.locator('#manage-open').click()
  await page.locator('#lx-model').fill('C:/Models/changed.gguf')
  await expect(page.locator('#lx-dirty-mark')).toBeVisible()
  const warned = await page.evaluate(() => {
    const e = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(e)
    return e.defaultPrevented
  })
  expect(warned).toBe(true)
  await page.locator('#lx-config').click()
  await page.getByRole('option', { name: 'New configuration' }).click()
  await expect(page.locator('#lx-modal')).toContainText('Unsaved changes')
  await page.locator('#lx-modal').getByText('Discard changes').click()
  await expect(page.locator('#lx-model')).toHaveValue('')
  await page.locator('#lx-model-browse').click()
  await expect(page.getByRole('dialog')).toContainText('qwen.gguf')
  await page.getByRole('dialog').getByText('qwen.gguf').click()
  await expect(page.locator('#lx-model')).toHaveValue('C:/Models/qwen.gguf')
  expect(calls.some((call) => call.startsWith('GET /api/browse'))).toBe(true)
})

test('launch errors and console tail behavior are visible', async ({
  page,
}) => {
  let reads = 0
  await mockApi(page, {
    'POST /api/launcher/launch': () => ({
      status: 400,
      body: { error: 'Invalid model' },
    }),
    'GET /api/launcher/console': () => {
      reads += 1
      return reads === 1
        ? {
            available: true,
            content: 'first\n',
            offset: 6,
            size: 6,
            path: 'C:/test/llama.log',
          }
        : {
            available: true,
            content: 'rotated\n',
            offset: 4,
            size: 8,
            path: 'C:/test/llama.log',
          }
    },
  })
  await page.goto('/')
  await page.locator('#manage-open').click()
  await page.locator('#lx-launch').click()
  await expect(page.locator('#lx-msg')).toContainText('Invalid model')
  await page.locator('#lx-console').click()
  await expect(page.locator('#console-out')).toContainText('first')
  await expect(page.locator('#console-out')).toContainText('rotated')
  await expect(page.locator('#console-out')).not.toContainText('first')
  await page.locator('#console-close').click()
  const stoppedAt = reads
  await page.waitForTimeout(1200)
  expect(reads).toBe(stoppedAt)
})

test('history filters, prompt viewing and confirmed clearing work', async ({
  page,
}) => {
  const calls = await mockApi(page)
  await page.goto('/')
  await page.locator('#history-open').click()
  await expect(page.locator('#history-body')).toContainText('Qwen test')
  await page.locator('#history-state').click()
  await page.getByRole('option', { name: 'Complete', exact: true }).click()
  await expect
    .poll(() => calls.some((call) => call.includes('state=complete')))
    .toBe(true)
  await page.locator('#history-body').getByText('View prompt').click()
  await expect(page.locator('#history-prompt-text')).toHaveText(
    'Secret test prompt',
  )
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click()
  await page.locator('#history-clear').click()
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click()
  expect(calls).not.toContain('DELETE /api/history')
  await page.locator('#history-clear').click()
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Clear history' })
    .click()
  await expect.poll(() => calls.includes('DELETE /api/history')).toBe(true)
})
