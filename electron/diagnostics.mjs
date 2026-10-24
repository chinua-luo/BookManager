import { appendFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import { format } from 'node:util'

export function createMainLogger(directory, { token = '', fallback = value => process.stderr.write(value) } = {}) {
  return (...values) => {
    const now = new Date()
    let detail = format(...values).replace(/([?&](?:token|key|secret|password|api_key)=)[^&\s]+/gi, '$1<redacted>').replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, '$1<redacted>')
    if (token) detail = detail.split(token).join('<redacted>')
    const line = JSON.stringify({ timestamp: now.toISOString(), level: 'ERROR', process: 'electron-main', detail }) + '\n'
    try {
      const logs = path.join(directory, 'logs')
      mkdirSync(logs, { recursive: true })
      appendFileSync(path.join(logs, `electron-${now.toISOString().slice(0, 10)}.jsonl`), line, 'utf8')
    } catch {
      try { fallback(line) } catch { /* Logging must not recurse or open a dialog. */ }
    }
  }
}
