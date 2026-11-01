import { randomUUID } from 'node:crypto'
import { link, mkdir, open, readFile, unlink } from 'node:fs/promises'
import path from 'node:path'
import { startupFailure } from './startup-checks.mjs'

// The installation identity lives in Electron's userData directory. It is
// deliberately independent from library_identity/library_id: it never enters
// the library database, the migrated BookManagerData directory, the renderer,
// or the bridge ready event.
export const INSTANCE_ID_FILENAME = '.instance-id'
export const INSTANCE_LOCK_FILENAME = '.instance-id.lock'

const DEFAULT_LOCK_TIMEOUT_MS = 5000
const LOCK_RETRY_MS = 25
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function sleep(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds))
}

// Returns null only when the identity file does not exist yet. Every other
// failure is surfaced as a curated startup failure so callers never receive
// file content, raw paths, or filesystem error codes in the user-facing dialog.
async function readIdentityFile(filename) {
  let content
  try {
    content = await readFile(filename, 'utf8')
  } catch (error) {
    if (error?.code === 'ENOENT') return null
    throw startupFailure('instance_identity_unavailable')
  }
  const identity = content.trim()
  if (!UUID_V4_PATTERN.test(identity)) throw startupFailure('instance_identity_invalid')
  return identity
}

// Write the whole temporary file first, then publish it with a hard link. The
// link is atomic and refuses to replace an existing identity, so a failed or
// concurrent startup can never silently overwrite a valid installation ID.
async function publishIdentity(directory, identity) {
  const filename = path.join(directory, INSTANCE_ID_FILENAME)
  const temporary = path.join(directory, `.instance-id.${process.pid}.${randomUUID()}.tmp`)
  try {
    let handle
    try {
      handle = await open(temporary, 'wx', 0o600)
      await handle.writeFile(`${identity}\n`, 'utf8')
      await handle.sync()
    } catch {
      throw startupFailure('instance_identity_unavailable')
    } finally {
      await handle?.close().catch(() => {})
    }
    try {
      await link(temporary, filename)
      return identity
    } catch (error) {
      if (error?.code === 'EEXIST') {
        const existing = await readIdentityFile(filename)
        if (existing) return existing
      }
      // An unreadable or corrupt target must not be replaced either.
      throw startupFailure('instance_identity_unavailable')
    }
  } finally {
    await unlink(temporary).catch(() => {})
  }
}

/**
 * Read the installation instance ID, creating it exactly once on first start.
 *
 * @param {string} directory Electron's userData directory.
 * @param {{ lockTimeoutMs?: number, onLockAcquired?: () => Promise<void> }} [options]
 *   lockTimeoutMs bounds how long a start waits for another start that is
 *   creating the identity. onLockAcquired is an injection point for tests that
 *   need to hold the exclusive lock while another process waits.
 * @returns {Promise<string>} a stable UUID v4.
 */
export async function ensureInstanceIdentity(directory, { lockTimeoutMs = DEFAULT_LOCK_TIMEOUT_MS, onLockAcquired } = {}) {
  if (typeof directory !== 'string' || directory.length === 0) throw startupFailure('instance_identity_unavailable')
  const timeout = Number.isFinite(lockTimeoutMs) && lockTimeoutMs > 0 ? lockTimeoutMs : DEFAULT_LOCK_TIMEOUT_MS
  try {
    await mkdir(directory, { recursive: true })
  } catch {
    throw startupFailure('instance_identity_unavailable')
  }

  const filename = path.join(directory, INSTANCE_ID_FILENAME)
  const existing = await readIdentityFile(filename)
  if (existing) return existing

  const deadline = Date.now() + timeout
  const lockFilename = path.join(directory, INSTANCE_LOCK_FILENAME)
  let lock
  for (;;) {
    try {
      lock = await open(lockFilename, 'wx', 0o600)
      break
    } catch (error) {
      if (error?.code !== 'EEXIST') throw startupFailure('instance_identity_unavailable')
      // Another start owns the lock. It publishes only complete files, so a
      // successful read here is always a complete identity.
      const published = await readIdentityFile(filename)
      if (published) return published
      const remaining = deadline - Date.now()
      if (remaining <= 0) throw startupFailure('instance_identity_timeout')
      await sleep(Math.min(LOCK_RETRY_MS, remaining))
    }
  }

  try {
    if (onLockAcquired) await onLockAcquired()
    // Re-check under the lock: a previous holder may have published just before
    // releasing, and that identity must win over generating a second one.
    const published = await readIdentityFile(filename)
    if (published) return published
    return await publishIdentity(directory, randomUUID())
  } finally {
    await lock.close().catch(() => {})
    await unlink(lockFilename).catch(() => {})
  }
}
