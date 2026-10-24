import { expect, test } from '@playwright/test'
import { gamingState, launcherState, mockApi, stats } from './fixtures'

const result = {
  route: 'proxy',
  model: 'Test model',
  target: 'http://127.0.0.1:8001',
  run_id: null,
  elapsed_seconds: 1.25,
  request_id: 'test-request',
  finish_reason: 'stop',
  message: {
    content:
      'for i in range(1, 101):\n    print(i)\n<script>window.injected=true</script>',
    reasoning_content: 'Some reasoning',
  },
  usage: null,
}

for (const route of ['direct', 'proxy']) {
  test(`one click sends the fixed test and displays ${route} result safely`, async ({
    page,
  }) => {
    let sends = 0
    const calls = await mockApi(page, {
      'POST /api/inference/test': (request) => {
        sends++
        expect(request.postDataJSON()).toEqual({})
        return {
          ...result,
          route,
          request_id: route === 'proxy' ? result.request_id : null,
        }
      },
    })
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/#monitor')
      await page
        .getByRole('button', { name: 'Test prompt', exact: true })
        .click()
      const dialog = page.getByRole('dialog', { name: 'Model test' })
      await expect(dialog).toBeVisible()
      await expect(dialog).toContainText('FizzBuzz')
      await expect(dialog).toContainText(
        route === 'proxy' ? 'Via proxy' : 'Direct',
      )
      await expect(dialog).toContainText('1.3s')
      await expect(page.getByLabel('Model reply')).toHaveText(
        result.message.content,
      )
      await expect(dialog).toContainText('Test model')
      expect(
        await page.evaluate(() => (window as any).injected),
      ).toBeUndefined()
      await page.getByText('Reasoning', { exact: true }).click()
      await expect(
        page.getByText('Some reasoning', { exact: true }),
      ).toBeVisible()
      if (route === 'proxy') await expect(dialog).toContainText('test-request')
      else await expect(dialog).not.toContainText('Request ID')
      const rect = await dialog.boundingBox()
      expect(rect!.x).toBeGreaterThanOrEqual(0)
      expect(rect!.x + rect!.width).toBeLessThanOrEqual(width + 1)
      expect(rect!.y).toBeGreaterThanOrEqual(0)
      expect(rect!.y + rect!.height).toBeLessThanOrEqual(901)
      if (process.env.CAPTURE_UI === '1')
        await page.screenshot({
          path: `test-results/test-prompt-${route}-${width}.png`,
        })
      await dialog.getByRole('button', { name: 'Close', exact: true }).click()
      await expect(dialog).not.toBeVisible()
    }
    expect(sends).toBe(3)
    expect(
      calls.filter((call) => call === 'POST /api/inference/test'),
    ).toHaveLength(3)
  })
}

test('pending test disables duplicate sends and closing does not resend', async ({
  page,
}) => {
  let release!: () => void
  const response = new Promise<void>((resolve) => {
    release = resolve
  })
  let sends = 0
  await mockApi(page, {
    'POST /api/inference/test': async () => {
      sends++
      await response
      return result
    },
  })
  await page.goto('/#monitor')
  await page.getByRole('button', { name: 'Test prompt', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('Sending test prompt')
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(
    page.getByRole('button', { name: 'Testing…', exact: true }),
  ).toBeDisabled()
  release()
  await expect(
    page.getByRole('button', { name: 'Test prompt', exact: true }),
  ).toBeEnabled()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  expect(sends).toBe(1)
})

test('shows proxy and timeout failures without retry', async ({ page }) => {
  let sends = 0
  await mockApi(page, {
    'POST /api/inference/test': () => {
      sends++
      return {
        status: sends === 1 ? 409 : 504,
        body: {
          error:
            sends === 1
              ? 'The enabled proxy is not listening.'
              : 'Test prompt timed out; the model may still finish the request.',
        },
      }
    },
  })
  await page.goto('/#monitor')
  await page.getByRole('button', { name: 'Test prompt', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(
    'proxy is not listening',
  )
  expect(sends).toBe(1)
  await page.getByRole('button', { name: 'Close', exact: true }).click()
  await page.getByRole('button', { name: 'Test prompt', exact: true }).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText(
    'may still finish',
  )
  expect(sends).toBe(2)
})

test('reports reasoning-only and generation-limit results', async ({
  page,
}) => {
  await mockApi(page, {
    'POST /api/inference/test': () => ({
      ...result,
      finish_reason: 'length',
      message: { reasoning_content: 'Thinking about FizzBuzz' },
    }),
  })
  await page.goto('/#monitor')
  await page.getByRole('button', { name: 'Test prompt', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText(
    'generation limit was reached',
  )
  await expect(page.getByRole('dialog')).toContainText('without a final answer')
  await page.getByText('Reasoning', { exact: true }).click()
  await expect(
    page.getByText('Thinking about FizzBuzz', { exact: true }),
  ).toBeVisible()
})

for (const state of ['offline', 'switching']) {
  test(`test prompt is disabled while ${state}`, async ({ page }) => {
    const calls = await mockApi(page, {
      'GET /api/stats': () => ({ ...stats, online: state !== 'offline' }),
      'GET /api/launcher/state': () => ({
        ...launcherState,
        gaming: { ...gamingState, blocked: state === 'switching' },
      }),
      'GET /api/gaming/state': () => ({
        ...gamingState,
        blocked: state === 'switching',
        phase: state === 'switching' ? 'restoring' : 'normal',
      }),
    })
    await page.goto('/#monitor')
    await expect(
      page.getByRole('button', { name: 'Test prompt', exact: true }),
    ).toBeDisabled()
    expect(
      calls.filter((call) => call === 'POST /api/inference/test'),
    ).toHaveLength(0)
  })
}
