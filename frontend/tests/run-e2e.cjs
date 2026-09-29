const { spawn, spawnSync } = require('node:child_process')
const http = require('node:http')
const net = require('node:net')
const path = require('node:path')

const root = path.resolve(__dirname, '..')
const reservePort = () =>
  new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolve(port))
    })
    server.on('error', reject)
  })
const ready = (port) =>
  new Promise((resolve) => {
    const request = http.get(`http://127.0.0.1:${port}/`, (response) => {
      response.resume()
      resolve(response.statusCode === 200)
    })
    request.on('error', () => resolve(false))
    request.setTimeout(1000, () => {
      request.destroy()
      resolve(false)
    })
  })
const waitForReady = async (port) => {
  for (let i = 0; i < 60; i++) {
    if (await ready(port)) return
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error('FastAPI browser-test server did not start')
}

async function main() {
  const port = await reservePort()
  const env = { ...process.env, UI_TEST_PORT: String(port) }
  const server = spawn(
    'python',
    [path.resolve(root, '../tests/ui_server.py')],
    {
      cwd: root,
      env,
      stdio: 'ignore',
      windowsHide: true,
    },
  )
  try {
    await waitForReady(port)
    const args = process.argv.slice(2)
    if (args[0] === '--') args.shift()
    const result = spawnSync(
      process.execPath,
      [require.resolve('@playwright/test/cli'), 'test', ...args],
      {
        cwd: root,
        env,
        stdio: 'inherit',
        windowsHide: true,
      },
    )
    if (result.error) throw result.error
    process.exitCode = result.status ?? 1
  } finally {
    server.kill()
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
