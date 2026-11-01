import assert from 'node:assert/strict'
import test from 'node:test'
import os from 'node:os'
import path from 'node:path'
import { once } from 'node:events'
import { spawn } from 'node:child_process'
import { chmod, mkdir, mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises'
import { ensureInstanceIdentity, INSTANCE_ID_FILENAME, INSTANCE_LOCK_FILENAME } from '../instance-identity.mjs'
import { startupFailure } from '../startup-checks.mjs'
import { notificationFromError } from '../user-errors.mjs'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const VALID_UUID = '3f4b39c2-1de6-4c7a-9b1d-2f5a6c7d8e9f'
const moduleUrl = new URL('../instance-identity.mjs', import.meta.url).href
const userErrorsUrl = new URL('../user-errors.mjs', import.meta.url).href

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'bookmanager-instance-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  return root
}

function noticeType(error) {
  return notificationFromError(error).errorType
}

async function createChildScript(directory) {
  const script = path.join(directory, 'instance-child.mjs')
  await writeFile(script, `
import { ensureInstanceIdentity } from ${JSON.stringify(moduleUrl)}
import { notificationFromError } from ${JSON.stringify(userErrorsUrl)}

const [, , userDataDirectory, mode] = process.argv
const send = message => new Promise(resolve => process.send(message, resolve))
const waitFor = predicate => new Promise(resolve => {
  const listener = message => {
    if (!predicate(message)) return
    process.off('message', listener)
    resolve(message)
  }
  process.on('message', listener)
})

async function run() {
  let identity
  if (mode === 'hold') {
    identity = await ensureInstanceIdentity(userDataDirectory, {
      lockTimeoutMs: 5000,
      onLockAcquired: async () => {
        await send({ event: 'locked' })
        await waitFor(message => message === 'release')
      }
    })
  } else {
    await send({ event: 'ready' })
    await waitFor(message => message === 'go')
    identity = await ensureInstanceIdentity(userDataDirectory)
  }
  await send({ event: 'result', identity })
}

run().then(() => process.exit(0)).catch(async error => {
  await send({ event: 'error', errorType: notificationFromError(error).errorType, message: error.message })
  process.exit(1)
})
`)
  return script
}

function startChild(script, userDataDirectory, mode) {
  const child = spawn(process.execPath, [script, userDataDirectory, mode], {
    stdio: ['ignore', 'pipe', 'pipe', 'ipc']
  })
  const inbox = []
  const waiters = []
  let stderr = ''
  child.stderr.on('data', chunk => { stderr += chunk })
  child.on('message', message => {
    const waiter = waiters.shift()
    if (waiter) waiter.settle(message)
    else inbox.push(message)
  })
  child.on('exit', code => {
    const error = new Error(`child exited (${code}) before sending a message; stderr: ${stderr.trim()}`)
    for (const waiter of waiters.splice(0)) waiter.fail(error)
  })
  function next(timeoutMs = 5000) {
    if (inbox.length) return Promise.resolve(inbox.shift())
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`timed out waiting for child message; stderr: ${stderr.trim()}`)), timeoutMs)
      waiters.push({
        settle: message => { clearTimeout(timer); resolve(message) },
        fail: error => { clearTimeout(timer); reject(error) }
      })
    })
  }
  return {
    child,
    next,
    stderr: () => stderr,
    send: message => child.send(message)
  }
}

async function stopChild(handle) {
  const { child } = handle
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  const exited = once(child, 'exit').catch(() => {})
  child.kill('SIGKILL')
  await exited
}

test('first start creates a valid UUID v4 identity file', async t => {
  const directory = await fixture(t)
  const identity = await ensureInstanceIdentity(directory)
  assert.match(identity, UUID_V4)
  assert.equal((await readFile(path.join(directory, INSTANCE_ID_FILENAME), 'utf8')).trim(), identity)
  assert.deepEqual(await readdir(directory), [INSTANCE_ID_FILENAME])
})

test('restarts keep the identity and the migrated library directory stays untouched', async t => {
  const directory = await fixture(t)
  const library = path.join(directory, 'BookManagerData')
  await mkdir(library)
  const first = await ensureInstanceIdentity(directory)
  const filename = path.join(directory, INSTANCE_ID_FILENAME)
  const bytes = await readFile(filename)
  const second = await ensureInstanceIdentity(directory)
  assert.equal(second, first)
  assert.deepEqual(await readFile(filename), bytes)
  assert.deepEqual(await readdir(library), [])
  assert.deepEqual((await readdir(directory)).sort(), ['BookManagerData', INSTANCE_ID_FILENAME].sort())
  assert.equal((await readdir(directory)).includes(INSTANCE_LOCK_FILENAME), false)
})

test('corrupt identity content is fatal and never overwritten', async t => {
  const directory = await fixture(t)
  const filename = path.join(directory, INSTANCE_ID_FILENAME)
  const bytes = Buffer.from('  not-a-uuid  \n', 'utf8')
  await writeFile(filename, bytes)
  await assert.rejects(ensureInstanceIdentity(directory), error => {
    const notice = notificationFromError(error)
    assert.equal(notice.level, 'fatal')
    assert.equal(notice.errorType, 'instance_identity_invalid')
    assert.doesNotMatch(notice.msg, /not-a-uuid/)
    return true
  })
  assert.deepEqual(await readFile(filename), bytes)
  assert.deepEqual(await readdir(directory), [INSTANCE_ID_FILENAME])
})

test('unreadable or unwritable identity locations map to unavailable', async t => {
  const directoryCase = await fixture(t)
  await mkdir(path.join(directoryCase, INSTANCE_ID_FILENAME))
  await assert.rejects(ensureInstanceIdentity(directoryCase), error => noticeType(error) === 'instance_identity_unavailable')

  if (process.platform === 'win32' || process.getuid?.() === 0) return

  const unreadable = await fixture(t)
  const filename = path.join(unreadable, INSTANCE_ID_FILENAME)
  await writeFile(filename, `${VALID_UUID}\n`)
  await chmod(filename, 0o000)
  try {
    await assert.rejects(ensureInstanceIdentity(unreadable), error => noticeType(error) === 'instance_identity_unavailable')
  } finally {
    await chmod(filename, 0o600)
  }

  const unwritable = await fixture(t)
  await chmod(unwritable, 0o500)
  try {
    await assert.rejects(ensureInstanceIdentity(unwritable), error => noticeType(error) === 'instance_identity_unavailable')
  } finally {
    await chmod(unwritable, 0o700)
  }
})

test('two processes racing on first start converge on one identity', { timeout: 10000 }, async t => {
  const directory = await fixture(t)
  const script = await createChildScript(await fixture(t))
  const children = [startChild(script, directory, 'create'), startChild(script, directory, 'create')]
  try {
    const ready = await Promise.all(children.map(handle => handle.next()))
    assert.deepEqual(ready, [{ event: 'ready' }, { event: 'ready' }])
    for (const handle of children) handle.send('go')
    const results = await Promise.all(children.map(handle => handle.next(8000)))
    for (const [index, result] of results.entries()) {
      assert.equal(result.event, 'result', `child ${index} failed: ${JSON.stringify(result)} ${children[index].stderr()}`)
      assert.match(result.identity, UUID_V4)
    }
    assert.equal(results[0].identity, results[1].identity)
    assert.equal((await readFile(path.join(directory, INSTANCE_ID_FILENAME), 'utf8')).trim(), results[0].identity)
    assert.deepEqual(await readdir(directory), [INSTANCE_ID_FILENAME])
  } finally {
    await Promise.all(children.map(stopChild))
  }
})

test('a start waiting on a held lock times out within the injected limit', { timeout: 10000 }, async t => {
  const directory = await fixture(t)
  const script = await createChildScript(await fixture(t))
  const holder = startChild(script, directory, 'hold')
  try {
    const locked = await holder.next()
    assert.equal(locked.event, 'locked')
    const started = Date.now()
    await assert.rejects(
      ensureInstanceIdentity(directory, { lockTimeoutMs: 500 }),
      error => noticeType(error) === 'instance_identity_timeout'
    )
    const elapsed = Date.now() - started
    assert.ok(elapsed >= 400, `waited too little before timing out: ${elapsed}ms`)
    assert.ok(elapsed < 3000, `waited too long before timing out: ${elapsed}ms`)
    holder.send('release')
    const result = await holder.next(8000)
    assert.equal(result.event, 'result')
    assert.match(result.identity, UUID_V4)
    assert.equal((await readFile(path.join(directory, INSTANCE_ID_FILENAME), 'utf8')).trim(), result.identity)
    assert.deepEqual(await readdir(directory), [INSTANCE_ID_FILENAME])
  } finally {
    await stopChild(holder)
  }
})

async function simulateStartup(initializeInstanceIdentity, effects) {
  const steps = []
  try {
    steps.push('diagnostics')
    await initializeInstanceIdentity()
    steps.push('identity')
    await effects.startBackend()
    steps.push('backend')
    effects.createWindow()
    steps.push('window')
  } catch (error) {
    const notice = notificationFromError(error)
    effects.report(notice.level === 'fatal' ? notice : notificationFromError(startupFailure()))
  }
  return steps
}

test('startup stops before the backend and window when the identity is fatal', async t => {
  const directory = await fixture(t)
  await writeFile(path.join(directory, INSTANCE_ID_FILENAME), 'corrupted-identity\n')
  const effects = { backend: 0, window: 0, reports: [] }
  const steps = await simulateStartup(
    () => ensureInstanceIdentity(directory),
    {
      startBackend: async () => { effects.backend += 1 },
      createWindow: () => { effects.window += 1 },
      report: notice => effects.reports.push(notice)
    }
  )
  assert.deepEqual(steps, ['diagnostics'])
  assert.equal(effects.backend, 0)
  assert.equal(effects.window, 0)
  assert.equal(effects.reports.length, 1)
  assert.equal(effects.reports[0].level, 'fatal')
  assert.equal(effects.reports[0].errorType, 'instance_identity_invalid')
  assert.doesNotMatch(effects.reports[0].msg, /corrupted-identity/)
})

test('main wires diagnostics before the identity and the backend after it', async () => {
  const source = await readFile(new URL('../main.mjs', import.meta.url), 'utf8')
  const logger = source.indexOf('createMainLogger(')
  const identity = source.indexOf("await ensureInstanceIdentity(app.getPath('userData'))")
  const backend = source.indexOf('await startBridge()')
  const window = source.indexOf('createWindow()', backend)
  assert.ok(logger >= 0, 'main process diagnostics logger is missing')
  assert.ok(identity > logger, 'the identity must be read after diagnostics are ready')
  assert.ok(backend > identity, 'the backend must start after the identity is available')
  assert.ok(window > backend, 'the window must be created after the backend starts')
  assert.match(source, /import \{ ensureInstanceIdentity \} from '\.\/instance-identity\.mjs'/)
})
