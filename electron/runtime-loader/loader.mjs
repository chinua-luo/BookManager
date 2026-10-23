import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const loaderDir = path.dirname(fileURLToPath(import.meta.url))
const config = JSON.parse(readFileSync(path.join(loaderDir, 'bookmanager-config.json'), 'utf8'))
const projectDir = process.env.BOOKMANAGER_PROJECT_DIR || config.projectDir
const pythonExecutable = process.env.BOOKMANAGER_PYTHON || config.pythonExecutable

if (!projectDir) throw new Error('BookManager project path is unavailable.')
if (!pythonExecutable) throw new Error('BookManager Python path is unavailable.')

process.env.BOOKMANAGER_PROJECT_DIR = projectDir
process.env.BOOKMANAGER_PYTHON = pythonExecutable

await import(pathToFileURL(path.join(projectDir, 'electron', 'main.mjs')).href)
