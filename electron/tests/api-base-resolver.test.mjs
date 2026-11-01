import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { BRIDGE_NOT_READY_MARKER, BRIDGE_NOT_READY_MESSAGE, createApiBaseResolver } from '../api-base.mjs'
import { businessError, notificationFromError } from '../user-errors.mjs'

const mainSource = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')

function bridgeFetchHarness({ base, fetchImpl = globalThis.fetch }) {
  const reports = []
  const consoleLogs = []
  const resolverLogs = []
  const source = mainSource.slice(mainSource.indexOf('function bridgeFetch('), mainSource.indexOf('function startBridge('))
  const createBridgeFetch = new Function('fetch', 'resolveApiBase', 'token', 'reportFatal', 'businessError', 'console', 'quitting', `
    ${source}
    return bridgeFetch
  `)
  const bridgeFetch = createBridgeFetch(
    fetchImpl,
    createApiBaseResolver(() => base, { log: value => resolverLogs.push(value) }),
    'test-token',
    (...args) => reports.push(args),
    businessError,
    { error: (...args) => consoleLogs.push(args) },
    false
  )
  return { bridgeFetch, reports, consoleLogs, resolverLogs }
}

test('resolveApiBase returns the dynamic loopback URL once the bridge is ready', () => {
  const base = 'http://127.0.0.1:54321'
  const logs = []
  const resolveApiBase = createApiBaseResolver(() => base, { log: value => logs.push(value) })
  assert.equal(resolveApiBase(), base)
  assert.equal(resolveApiBase(), base)
  assert.deepEqual(logs, [])
})

test('resolveApiBase throws synchronously and records the marker only once before readiness', () => {
  let base = null
  const logs = []
  const resolveApiBase = createApiBaseResolver(() => base, { log: value => logs.push(value) })
  assert.throws(() => resolveApiBase(), error => error instanceof Error && error.message === BRIDGE_NOT_READY_MESSAGE)
  assert.throws(() => resolveApiBase(), new RegExp(BRIDGE_NOT_READY_MESSAGE))
  assert.equal(logs.length, 1)
  assert.match(logs[0], new RegExp(BRIDGE_NOT_READY_MARKER))
  assert.equal(logs[0].match(new RegExp(BRIDGE_NOT_READY_MARKER, 'g')).length, 1)
  base = 'http://127.0.0.1:43210'
  assert.equal(resolveApiBase(), base)
  assert.equal(logs.length, 1)
})

test('requests before readiness reject through async wrappers without a disconnect fatal', async () => {
  const { bridgeFetch, reports, consoleLogs, resolverLogs } = bridgeFetchHarness({
    base: null,
    fetchImpl: async () => { throw new Error('fetch must not run before readiness') }
  })
  assert.throws(() => bridgeFetch('/api/tree'), new RegExp(BRIDGE_NOT_READY_MESSAGE))
  const readJson = async pathname => bridgeFetch(pathname)
  await assert.rejects(readJson('/api/tree'), new RegExp(BRIDGE_NOT_READY_MESSAGE))
  assert.equal(reports.length, 0)
  assert.equal(consoleLogs.length, 0)
  assert.equal(resolverLogs.filter(value => String(value).includes(BRIDGE_NOT_READY_MARKER)).length, 1)
})

test('ready requests keep the existing URL, authorization and fatal handling', async () => {
  let requested = null
  const ready = bridgeFetchHarness({
    base: 'http://127.0.0.1:51234',
    fetchImpl: async (url, options) => {
      requested = { url, options }
      return new Response('{}', { status: 200 })
    }
  })
  const response = await ready.bridgeFetch('/api/tree', { headers: { 'X-Test': '1' } })
  assert.equal(response.status, 200)
  assert.equal(requested.url, 'http://127.0.0.1:51234/api/tree')
  assert.equal(requested.options.headers.Authorization, 'Bearer test-token')
  assert.equal(requested.options.headers['X-Test'], '1')
  assert.equal(ready.reports.length, 0)

  const failing = bridgeFetchHarness({
    base: 'http://127.0.0.1:51234',
    fetchImpl: async () => { throw new Error('ECONNREFUSED') }
  })
  await assert.rejects(failing.bridgeFetch('/api/tree'), error => {
    const notice = notificationFromError(error)
    return notice.level === 'fatal' && notice.errorType === 'backend'
  })
  assert.equal(failing.reports.length, 1)
  assert.equal(failing.consoleLogs.length, 1)
})

test('main keeps bridgeUrl for local pages, navigation and document origins', () => {
  assert.match(mainSource, /bridgeUrl = `http:\/\/127\.0\.0\.1:\$\{event\.port\}`/)
  assert.match(mainSource, /mainWindow\.loadURL\(bridgeUrl\)/)
  assert.match(mainSource, /headers\.set\('access-control-allow-origin', bridgeUrl\)/)
  assert.match(mainSource, /new URL\(targetUrl\)\.origin !== new URL\(bridgeUrl\)\.origin/)
})

test('startup completes startBridge before creating the window', () => {
  const start = mainSource.indexOf('await startBridge()')
  const window = mainSource.indexOf('createWindow()', start)
  assert.ok(start >= 0, 'startBridge must be awaited during startup')
  assert.ok(window > start, 'createWindow must run only after startBridge resolves')
})
