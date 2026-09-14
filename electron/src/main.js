import { mountViewer } from '@file-viewer/web'
import ebookRenderer from '@file-viewer/renderer-epub'
import imageRenderer from '@file-viewer/renderer-image'
import pdfRenderer from '@file-viewer/renderer-pdf'
import pptxRenderer from '@file-viewer/renderer-pptx'
import spreadsheetRenderer from '@file-viewer/renderer-spreadsheet'
import textRenderer from '@file-viewer/renderer-text'
import wordRenderer from '@file-viewer/renderer-word'
import './style.css'

const $ = selector => document.querySelector(selector)
const folderTree = $('#folder-tree'); const contentList = $('#content-list')
const contentTitle = $('#content-title'); const contentCount = $('#content-count')
const previewTitle = $('#preview-title'); const preview = $('#preview'); const status = $('#status')
const appMenu = $('#app-menu'); const contextElement = $('#context-menu'); const modalRoot = $('#modal-root')
const workspace = $('.workspace'); const workspaceSplitter = $('#workspace-splitter')
const textExtensions = new Set(['.txt', '.md', '.py', '.json', '.xml', '.html', '.htm', '.css', '.js', '.csv', '.log'])
let tree = []; let selectedFolderId = null; let selectedItem = null; let contextTarget = null; let editable = null; let shortcuts = {}
const sortModes = new Map()

function setSidebarWidth(requestedWidth, persist = false) {
  const bounds = workspace.getBoundingClientRect()
  const minSidebar = Math.min(240, Math.max(120, bounds.width - 287))
  const maxSidebar = Math.max(minSidebar, bounds.width - 287)
  const width = Math.round(Math.max(minSidebar, Math.min(maxSidebar, requestedWidth)))
  workspace.style.setProperty('--sidebar-width', `${width}px`)
  workspaceSplitter.setAttribute('aria-valuemin', String(minSidebar))
  workspaceSplitter.setAttribute('aria-valuemax', String(maxSidebar))
  workspaceSplitter.setAttribute('aria-valuenow', String(width))
  if (persist) localStorage.setItem('bookmanager.sidebarWidth', String(width))
}

const savedSidebarWidth = Number(localStorage.getItem('bookmanager.sidebarWidth'))
if (Number.isFinite(savedSidebarWidth) && savedSidebarWidth > 0) requestAnimationFrame(() => setSidebarWidth(savedSidebarWidth))
workspaceSplitter.addEventListener('pointerdown', event => {
  if (event.button !== 0) return
  event.preventDefault(); workspace.classList.add('resizing'); workspaceSplitter.setPointerCapture(event.pointerId)
  const move = pointer => setSidebarWidth(pointer.clientX - workspace.getBoundingClientRect().left)
  const end = pointer => {
    setSidebarWidth(pointer.clientX - workspace.getBoundingClientRect().left, true); workspace.classList.remove('resizing')
    if (workspaceSplitter.hasPointerCapture(pointer.pointerId)) workspaceSplitter.releasePointerCapture(pointer.pointerId)
    workspaceSplitter.removeEventListener('pointermove', move); workspaceSplitter.removeEventListener('pointerup', end); workspaceSplitter.removeEventListener('pointercancel', cancel)
  }
  const cancel = () => { workspace.classList.remove('resizing'); workspaceSplitter.removeEventListener('pointermove', move); workspaceSplitter.removeEventListener('pointerup', end); workspaceSplitter.removeEventListener('pointercancel', cancel) }
  workspaceSplitter.addEventListener('pointermove', move); workspaceSplitter.addEventListener('pointerup', end); workspaceSplitter.addEventListener('pointercancel', cancel)
})
workspaceSplitter.addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
  event.preventDefault(); const current = workspaceSplitter.getBoundingClientRect().left - workspace.getBoundingClientRect().left
  setSidebarWidth(current + (event.key === 'ArrowLeft' ? -16 : 16), true)
})
window.addEventListener('resize', () => {
  const current = workspaceSplitter.getBoundingClientRect().left - workspace.getBoundingClientRect().left
  setSidebarWidth(current)
})

const viewerHost = document.createElement('div'); viewerHost.className = 'viewer-host'; preview.append(viewerHost)
const viewer = mountViewer(viewerHost, { options: { rendererMode: 'replace', renderers: [pdfRenderer, wordRenderer, spreadsheetRenderer, pptxRenderer, ebookRenderer, imageRenderer, textRenderer], locale: 'zh-CN', theme: 'light', toolbar: false, search: false, fit: 'width', pdf: { defaultNavigationVisible: false, thumbnails: false } } })

function message(value) { status.textContent = value }
function error(errorValue) { console.error(errorValue); message(`操作失败：${errorValue instanceof Error ? errorValue.message : errorValue}`) }
function html(value) { return String(value).replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]) }
function size(value) { const units = ['B', 'KB', 'MB', 'GB']; let number = Number(value) || 0; for (const unit of units) { if (number < 1024 || unit === 'GB') return unit === 'B' ? `${number} B` : `${number.toFixed(1)} ${unit}`; number /= 1024 } }
function closeMenus() { appMenu.querySelectorAll('details').forEach(menu => { menu.open = false }) }
function findFolder(id, nodes = tree, parents = []) { for (const folder of nodes) { if (folder.id === id) return { folder, parents }; const found = findFolder(id, folder.children || [], [...parents, folder]); if (found) return found } return null }
function currentSort(id) { const found = findFolder(id); for (const node of [...(found?.parents || []), found?.folder].reverse()) if (node && sortModes.has(node.id)) return sortModes.get(node.id); return '标准' }
function sortedItems(items, folderId) { const series = currentSort(folderId) === '序列'; return [...items].sort((left, right) => { const a = left.nameParts || {}; const b = right.nameParts || {}; if (!series) return (a.mainTitle || left.listName).localeCompare(b.mainTitle || right.listName, 'en', { sensitivity: 'base' }); const ak = [a.seriesAbbr || '', /^\d+$/.test(a.number || '') ? Number(a.number) : Number.MAX_SAFE_INTEGER, a.mainTitle || '']; const bk = [b.seriesAbbr || '', /^\d+$/.test(b.number || '') ? Number(b.number) : Number.MAX_SAFE_INTEGER, b.mainTitle || '']; return ak[0].localeCompare(bk[0]) || ak[1] - bk[1] || ak[2].localeCompare(bk[2]) }) }

function setPreviewTitle(title) {
  previewTitle.replaceChildren()
  const track = document.createElement('span'); track.className = 'preview-title-track'; track.textContent = title
  previewTitle.append(track)
  requestAnimationFrame(() => {
    if (track.scrollWidth <= previewTitle.clientWidth) return
    track.textContent = `${title}     ${title}`
    requestAnimationFrame(() => {
      track.style.setProperty('--marquee-shift', `${Math.ceil(track.scrollWidth / 2)}px`)
      track.style.setProperty('--marquee-duration', `${Math.max(10, title.length * 0.28)}s`)
      track.classList.add('marquee')
    })
  })
}
function clearPreview() { editable = null; viewerHost.hidden = true; preview.replaceChildren() }
function showPreviewNode(node) { clearPreview(); preview.replaceChildren(node) }
function showViewerHost() { clearPreview(); preview.replaceChildren(viewerHost); viewerHost.hidden = false }
function folderSummary(payload) { setPreviewTitle(payload.folder.name); const section = document.createElement('section'); section.className = 'folder-summary'; section.innerHTML = `<h2>文件夹信息</h2><dl><dt>子文件夹</dt><dd>${payload.stats.folderCount}</dd><dt>内部文件</dt><dd>${payload.stats.itemCount}</dd><dt>底层唯一文件</dt><dd>${payload.stats.uniqueDocumentCount}</dd><dt>占用空间</dt><dd>${size(payload.stats.uniqueSize)}</dd></dl>`; showPreviewNode(section) }

function modal(title, content, options = {}) {
  modalRoot.hidden = false
  const cover = document.createElement('div'); cover.className = `modal-backdrop${options.nested ? ' nested' : ''}`
  const panel = document.createElement('section'); panel.className = `modal${options.wide ? ' wide' : ''}`
  const header = document.createElement('header'); header.textContent = title
  const body = document.createElement('div'); body.className = 'modal-body'; typeof content === 'string' ? body.innerHTML = content : body.append(content)
  const footer = document.createElement('footer'); footer.className = 'modal-footer'
  const close = () => { cover.remove(); modalRoot.hidden = !modalRoot.childElementCount }
  for (const item of options.buttons || []) { const button = document.createElement('button'); button.type = 'button'; button.className = `dialog-button${item.primary ? ' primary' : ''}${item.danger ? ' danger' : ''}`; button.textContent = item.label; button.addEventListener('click', () => item.action(close)); footer.append(button) }
  const primary = footer.querySelector('.primary')
  panel.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.defaultPrevented && !event.isComposing && primary) { event.preventDefault(); primary.click() } })
  header.addEventListener('pointerdown', event => {
    if (event.button !== 0) return
    const bounds = panel.getBoundingClientRect(); const startX = event.clientX; const startY = event.clientY; const originX = bounds.left; const originY = bounds.top
    panel.style.position = 'fixed'; panel.style.left = `${originX}px`; panel.style.top = `${originY}px`; panel.classList.add('dragging'); header.setPointerCapture(event.pointerId)
    const move = pointer => { const maxX = Math.max(8, window.innerWidth - panel.offsetWidth - 8); const maxY = Math.max(8, window.innerHeight - panel.offsetHeight - 8); panel.style.left = `${Math.max(8, Math.min(maxX, originX + pointer.clientX - startX))}px`; panel.style.top = `${Math.max(8, Math.min(maxY, originY + pointer.clientY - startY))}px` }
    const end = () => { panel.classList.remove('dragging'); header.removeEventListener('pointermove', move); header.removeEventListener('pointerup', end); header.removeEventListener('pointercancel', end) }
    header.addEventListener('pointermove', move); header.addEventListener('pointerup', end); header.addEventListener('pointercancel', end)
  })
  cover.addEventListener('mousedown', event => { if (event.target === cover) close() }); panel.append(header, body, footer); cover.append(panel); if (options.nested) modalRoot.append(cover); else modalRoot.replaceChildren(cover)
  return { close, panel, body }
}
function ask(title, text, danger = false) { return new Promise(resolve => modal(title, `<p class="dialog-hint">${html(text)}</p>`, { buttons: [{ label: '取消', action: close => { close(); resolve(false) } }, { label: '确认', primary: !danger, danger, action: close => { close(); resolve(true) } }] })) }
function promptValue(title, label, initial = '') { return new Promise(resolve => { const form = document.createElement('form'); form.className = 'form-stack'; const field = document.createElement('label'); field.textContent = label; const input = document.createElement('input'); input.value = initial; field.append(input); form.append(field); const dialog = modal(title, form, { buttons: [{ label: '取消', action: close => { close(); resolve(null) } }, { label: '确定', primary: true, action: close => { const value = input.value.trim(); close(); resolve(value || null) } }] }); form.addEventListener('submit', event => { event.preventDefault(); dialog.panel.querySelector('.primary').click() }); input.focus() }) }
function tagValues(input) { return input.value.split(',').map(value => value.trim()).filter(Boolean) }
async function openTagPicker(input) {
  const result = await window.bookManager.get('/api/tags'); const selected = new Map(tagValues(input).map(value => [value.toLocaleLowerCase(), value]))
  const content = document.createElement('div'); content.className = 'form-stack'; const hint = document.createElement('p'); hint.className = 'dialog-hint'; hint.textContent = '点击标签可选择或取消；也可以直接在命名窗口输入标签。'; const selectedPills = document.createElement('div'); selectedPills.className = 'tag-pills'; const newTag = document.createElement('input'); newTag.placeholder = '输入新标签'; const addTag = document.createElement('button'); addTag.type = 'button'; addTag.className = 'dialog-button'; addTag.textContent = '添加标签'; const newTagRow = document.createElement('div'); newTagRow.className = 'tag-input-row'; newTagRow.append(newTag, addTag); content.append(hint, selectedPills, newTagRow)
  const refreshSelected = () => { selectedPills.replaceChildren(); if (!selected.size) { selectedPills.textContent = '尚未选择标签。'; return } for (const [key, value] of selected) { const button = document.createElement('button'); button.type = 'button'; button.className = 'selected'; button.textContent = value; button.addEventListener('click', () => { selected.delete(key); refreshSelected() }); selectedPills.append(button) } }
  const addTypedTag = () => { const value = newTag.value.trim(); if (!value) return; selected.set(value.toLocaleLowerCase(), value); newTag.value = ''; refreshSelected() }; addTag.addEventListener('click', addTypedTag); newTag.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addTypedTag() } }); refreshSelected()
  for (const [label, values] of [['常用标签', result.common || []], ['一般标签', result.general || []]]) {
    const section = document.createElement('section'); section.className = 'tag-picker-section'; const heading = document.createElement('h3'); heading.textContent = label; const pills = document.createElement('div'); pills.className = 'tag-pills'
    if (!values.length) pills.textContent = '没有标签。'
    values.forEach(entry => { const button = document.createElement('button'); button.type = 'button'; button.textContent = `${entry.name} (${entry.count})`; const refresh = () => button.classList.toggle('selected', selected.has(entry.name.toLocaleLowerCase())); refresh(); button.addEventListener('click', () => { const key = entry.name.toLocaleLowerCase(); if (selected.has(key)) selected.delete(key); else selected.set(key, entry.name); refresh(); refreshSelected() }); pills.append(button) }); section.append(heading, pills); content.append(section)
  }
  modal('选择标签', content, { nested: true, buttons: [{ label: '取消', action: close => close() }, { label: '确定', primary: true, action: close => { input.value = [...selected.values()].join(', '); input.dispatchEvent(new Event('input', { bubbles: true })); close() } }] })
}

function closeContext() { contextTarget = null; contextElement.hidden = true; contextElement.replaceChildren() }
function editableControl(target) { return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement }
const inputHistory = new WeakMap()
function rememberInput(input) {
  const history = inputHistory.get(input) || { values: [], index: -1 }
  if (history.values[history.index] !== input.value) {
    history.values.splice(history.index + 1)
    history.values.push(input.value)
    if (history.values.length > 80) history.values.shift()
    history.index = history.values.length - 1
  }
  inputHistory.set(input, history)
}
function restoreInputHistory(input, direction) {
  const history = inputHistory.get(input); if (!history) return
  const next = Math.max(0, Math.min(history.values.length - 1, history.index + direction)); if (next === history.index) return
  history.index = next; input.value = history.values[next]; input.setSelectionRange(input.value.length, input.value.length); input.dispatchEvent(new Event('input', { bubbles: true }))
}
document.addEventListener('focusin', event => { if (editableControl(event.target)) rememberInput(event.target) }, true)
document.addEventListener('input', event => { if (editableControl(event.target)) rememberInput(event.target) }, true)
document.addEventListener('keydown', event => {
  if (!(event.metaKey || event.ctrlKey) || !editableControl(event.target)) return
  const key = event.key.toLowerCase(); if (!['c', 'x', 'v', 'z', 'a'].includes(key)) return
  const input = event.target; const start = input.selectionStart ?? 0; const end = input.selectionEnd ?? start
  if ((key === 'x' || key === 'v') && input.readOnly) return
  event.preventDefault(); event.stopPropagation()
  if (key === 'a') { input.select(); return }
  if (key === 'z') { restoreInputHistory(input, event.shiftKey ? 1 : -1); return }
  if (key === 'c') { void window.bookManager.clipboardWriteText(input.value.slice(start, end)); return }
  if (key === 'x') { void window.bookManager.clipboardWriteText(input.value.slice(start, end)); input.setRangeText('', start, end, 'start'); input.dispatchEvent(new Event('input', { bubbles: true })); return }
  window.bookManager.clipboardReadText().then(text => { input.setRangeText(text, start, end, 'end'); input.dispatchEvent(new Event('input', { bubbles: true })) }).catch(error)
}, true)
function contextButton(label, action, parent = contextElement) { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.dataset.action = action; parent.append(button); return button }
function openContext(event, target) {
  event.preventDefault(); contextTarget = target; contextElement.replaceChildren()
  if (target.kind === 'folder') {
    contextButton('打开文件夹', 'folder-open'); contextButton('新建子文件夹', 'folder-create'); contextButton('重命名文件夹', 'folder-rename'); contextButton('移动', 'folder-move')
    const sortButton = contextButton('排序方式', '')
    const sortMenu = document.createElement('div'); sortMenu.className = 'context-submenu'; sortMenu.hidden = true; sortMenu.style.top = '124px'
    contextButton(currentSort(target.folder.id) === '标准' ? '标准（当前）' : '标准', 'sort-standard', sortMenu)
    contextButton(currentSort(target.folder.id) === '序列' ? '序列（当前）' : '序列', 'sort-series', sortMenu)
    sortButton.addEventListener('click', event => { event.stopPropagation(); sortMenu.hidden = !sortMenu.hidden })
    contextElement.append(sortMenu); contextButton('删除文件夹', 'folder-delete')
  } else {
    contextButton('用系统默认方式打开', 'file-open'); contextButton('打开底层位置', 'file-location'); contextButton('复制文件名', 'file-copy'); contextButton('删除', 'file-delete'); contextButton('删除底层文件', 'file-delete-underlying'); contextButton('重命名', 'file-rename'); contextButton('移动', 'file-move'); contextButton('镜像到文件夹', 'file-mirror'); contextButton('替换底层文件', 'file-replace')
  }
  contextElement.style.left = `${Math.min(event.clientX, window.innerWidth - 210)}px`; contextElement.style.top = `${Math.min(event.clientY, window.innerHeight - 340)}px`; contextElement.hidden = false
}

async function chooseFolder(title, excludedId = null) {
  return new Promise(resolve => {
    let shown = tree[0]; let picked = null; const content = document.createElement('div')
    function render() {
      content.replaceChildren(); const found = findFolder(shown.id); const crumbs = document.createElement('div'); crumbs.className = 'breadcrumb'
      for (const node of [...(found?.parents || []), shown]) { const button = document.createElement('button'); button.type = 'button'; button.textContent = node.name; button.addEventListener('dblclick', () => { shown = node; picked = null; render() }); crumbs.append(button) }
      const hint = document.createElement('p'); hint.className = 'dialog-hint'; hint.textContent = '单击选择目标文件夹；双击文件夹进入其子文件夹。'; const list = document.createElement('div'); list.className = 'choice-list'
      const add = folder => { if (folder.id === excludedId) return; const button = document.createElement('button'); button.type = 'button'; button.textContent = folder.id === shown.id ? `选择当前文件夹：${folder.name}` : folder.name; if (folder.id === picked) button.classList.add('selected'); button.addEventListener('click', () => { picked = folder.id; render() }); button.addEventListener('dblclick', () => { shown = folder; picked = null; render() }); list.append(button) }
      add(shown); (shown.children || []).forEach(add); content.append(crumbs, hint, list)
    }
    render(); modal(title, content, { buttons: [{ label: '取消', action: close => { close(); resolve(null) } }, { label: '确认', primary: true, action: close => { if (!picked) { message('请先选择目标文件夹。'); return } close(); resolve(picked) } }] })
  })
}

function nameDisplay(parts) { const series = `${parts.seriesAbbr || ''}${parts.number || ''}`; const first = [series, parts.mainTitle].filter(Boolean).join(' '); const edition = !/^1$/.test(parts.edition || '1') ? (parts.editionLanguage === '中文' ? `第${parts.edition}版` : `${parts.edition}${[11, 12, 13].includes(Number(parts.edition) % 100) ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[Number(parts.edition) % 10] || 'th')} Edition`) : ''; const prefix = [first, parts.subtitle, edition].filter(Boolean).join(' - '); return `${prefix}${parts.authors ? `${prefix ? ' _ ' : ''}${parts.authors}` : ''}${parts.extension || ''}` }
function titleCase(value) { return value.replace(/\b([A-Za-z])([A-Za-z']*)\b/g, (_whole, first, rest) => `${first.toUpperCase()}${rest.toLowerCase()}`) }
async function nameDialog(title, item = null, sourcePath = null) {
  const candidate = sourcePath ? (await window.bookManager.post('/api/name-candidate', { path: sourcePath })).nameParts : (item?.nameParts || {})
  return new Promise(resolve => {
    const form = document.createElement('form'); form.className = 'form-grid'; const controls = {}; const keys = [['系列缩写', 'seriesAbbr'], ['编号', 'number'], ['主标题名', 'mainTitle'], ['副标题名', 'subtitle'], ['版本号', 'edition'], ['作者信息', 'authors'], ['扩展名', 'extension']]
    if (sourcePath) { const paste = document.createElement('label'); paste.className = 'span-2'; paste.textContent = '复制文本识别名称字符段'; const textarea = document.createElement('textarea'); textarea.rows = 2; const apply = document.createElement('button'); apply.type = 'button'; apply.className = 'dialog-button'; apply.textContent = '应用文本'; apply.addEventListener('click', async () => { const value = textarea.value.trim(); if (!value) { message('请先粘贴名称文本。'); return } try { const result = await window.bookManager.post('/api/name-parse', { text: value }); const includesExtension = /\.[A-Za-z0-9]{1,10}\s*$/.test(value); keys.forEach(([, key]) => { if (key !== 'extension' || includesExtension) controls[key].value = result.nameParts[key] || '' }); languageSelect.value = result.nameParts.editionLanguage || languageSelect.value; refreshPreview() } catch (issue) { error(issue) } }); paste.append(textarea, apply); form.append(paste) }
    for (const [label, key] of keys) { const field = document.createElement('label'); field.textContent = label; const input = document.createElement('input'); input.value = candidate[key] || (key === 'edition' ? '1' : ''); input.addEventListener('input', refreshPreview); field.append(input); form.append(field); controls[key] = input }
    const language = document.createElement('label'); language.textContent = '版本语言'; const languageSelect = document.createElement('select'); languageSelect.className = 'dialog-select'; languageSelect.innerHTML = '<option>英文</option><option>中文</option>'; languageSelect.value = candidate.editionLanguage || '英文'; languageSelect.addEventListener('change', refreshPreview); language.append(languageSelect); form.append(language)
    const caseOption = document.createElement('label'); caseOption.textContent = '主、副标题格式'; const caseSelect = document.createElement('select'); caseSelect.className = 'dialog-select'; caseSelect.innerHTML = '<option>原样</option><option>驼峰化</option>'; caseOption.append(caseSelect); form.append(caseOption)
    const note = document.createElement('label'); note.className = 'span-2'; note.textContent = '备注（不加入文件名）'; const noteInput = document.createElement('input'); noteInput.value = item?.note || ''; note.append(noteInput); form.append(note)
    const tags = document.createElement('label'); tags.className = 'span-2'; tags.textContent = '标签（以逗号分隔，不加入文件名）'; const tagRow = document.createElement('div'); tagRow.className = 'tag-input-row'; const tagsInput = document.createElement('input'); tagsInput.value = item?.tags?.join(', ') || ''; const selectTags = document.createElement('button'); selectTags.type = 'button'; selectTags.className = 'dialog-button'; selectTags.textContent = '选择标签'; selectTags.addEventListener('click', () => openTagPicker(tagsInput).catch(error)); tagRow.append(tagsInput, selectTags); tags.append(tagRow); form.append(tags)
    const previewName = document.createElement('div'); previewName.className = 'name-preview span-2'; const metadata = document.createElement('p'); metadata.className = 'dialog-hint span-2'; form.append(previewName, metadata)
    function data() { const values = {}; keys.forEach(([, key]) => { values[key] = controls[key].value.trim() }); if (caseSelect.value === '驼峰化') { values.mainTitle = titleCase(values.mainTitle); values.subtitle = titleCase(values.subtitle) }; values.editionLanguage = languageSelect.value; return values }
    function refreshPreview() { previewName.textContent = nameDisplay(data()) || '待补全：文件名整体不能为空' }
    refreshPreview()
    if (sourcePath) { metadata.textContent = '正在后台分析文件元数据…'; window.bookManager.post('/api/name-metadata', { path: sourcePath }).then(result => { metadata.textContent = result.message; if (result.extracted) keys.forEach(([, key]) => { if (!controls[key].value.trim() && result.nameParts[key]) controls[key].value = result.nameParts[key] }); refreshPreview() }).catch(value => { metadata.textContent = `内容分析失败：${value.message}` }) }
    const dialog = modal(title, form, { wide: true, buttons: [{ label: '取消', action: close => { close(); resolve(null) } }, { label: '确定', primary: true, action: close => { const nameParts = data(); if (!nameDisplay(nameParts)) { message('文件名整体不能为空。'); return } close(); resolve({ nameParts, note: noteInput.value.trim(), tags: tagsInput.value.split(',').map(value => value.trim()).filter(Boolean) }) } }] }); form.addEventListener('submit', event => { event.preventDefault(); dialog.panel.querySelector('.primary').click() })
  })
}

async function showItem(item) {
  selectedItem = item; setPreviewTitle(item.displayName); const extension = item.extension.toLowerCase()
  if (textExtensions.has(extension)) { const editor = document.createElement('textarea'); editor.className = 'text-editor'; editor.style.cssText = 'width:100%;height:100%;border:0;padding:14px;resize:none;font:13px/1.55 ui-monospace,Menlo,monospace'; showPreviewNode(editor); editor.value = await fetch(await window.bookManager.documentUrl(item.documentId)).then(response => response.text()); editable = { item, editor }; return }
  if (['.djvu', '.djv'].includes(extension)) { const holder = document.createElement('div'); holder.className = 'djvu-preview'; for (const url of await window.bookManager.djvuPageUrls(item.documentId)) { const image = document.createElement('img'); image.src = url; image.alt = item.displayName; holder.append(image) } showPreviewNode(holder); return }
  showViewerHost(); await viewer.load({ url: await window.bookManager.documentUrl(item.documentId), filename: item.displayName, size: item.size })
}
function markContentSelection(button) { contentList.querySelectorAll('.content-row.selected').forEach(row => row.classList.remove('selected')); button.classList.add('selected') }
function fileIconClass(extension) {
  const ext = String(extension || '').replace(/^\./, '').toLowerCase()
  if (ext === 'pdf') return 'file-icon-pdf'
  if (['epub', 'mobi', 'azw', 'azw3'].includes(ext)) return 'file-icon-ebook'
  if (['djvu', 'djv'].includes(ext)) return 'file-icon-djvu'
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return 'file-icon-document'
  if (['xls', 'xlsx', 'ods', 'csv'].includes(ext)) return 'file-icon-sheet'
  if (['ppt', 'pptx', 'odp'].includes(ext)) return 'file-icon-slides'
  if (['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tif', 'tiff', 'heic'].includes(ext)) return 'file-icon-image'
  if (['txt', 'md', 'log', 'json', 'xml', 'html', 'htm'].includes(ext)) return 'file-icon-text'
  return 'file-icon-other'
}
function fileRow(item) { const button = document.createElement('button'); button.className = 'content-row file-row'; button.type = 'button'; button.innerHTML = `<span class="file-icon ${fileIconClass(item.extension)}">${html(item.extension.slice(1).toUpperCase() || 'FILE')}</span><span>${html(item.listName)}</span><small>${size(item.size)}</small>`; button.addEventListener('click', () => { markContentSelection(button); showItem(item).catch(error) }); button.addEventListener('dblclick', () => openDefault(item).catch(error)); button.addEventListener('contextmenu', event => openContext(event, { kind: 'file', item })); return button }
async function selectFolder(id) {
  selectedFolderId = id; selectedItem = null; const payload = await window.bookManager.folder(id)
  contentTitle.textContent = payload.folder.name; contentCount.textContent = `${payload.folders.length + payload.items.length} 项`; contentList.replaceChildren()
  ;[...payload.folders].sort((a, b) => a.name.localeCompare(b.name)).forEach(folder => {
    const button = document.createElement('button'); button.className = 'content-row folder-row'; button.type = 'button'; button.innerHTML = `<span class="folder-icon">DIR</span><span>${html(folder.name)}</span>`
    button.addEventListener('click', () => { markContentSelection(button); selectFolder(folder.id).then(() => contentList.focus()).catch(error) }); button.addEventListener('contextmenu', event => openContext(event, { kind: 'folder', folder })); contentList.append(button)
  })
  sortedItems(payload.items, id).forEach(item => contentList.append(fileRow(item))); folderSummary(payload)
}
function renderTree(nodes) { const list = document.createElement('ul'); nodes.forEach((folder, index) => { const item = document.createElement('li'); const details = document.createElement('details'); details.open = index === 0; const summary = document.createElement('summary'); summary.addEventListener('click', event => { if (event.target === summary) selectFolder(folder.id).catch(error) }); const button = document.createElement('button'); button.type = 'button'; button.dataset.folderId = String(folder.id); button.textContent = folder.name; button.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); selectFolder(folder.id).catch(error) }); button.addEventListener('contextmenu', event => openContext(event, { kind: 'folder', folder })); summary.append(button); details.append(summary); if (folder.children.length) details.append(renderTree(folder.children)); item.append(details); list.append(item) }); return list }
async function refresh(select = selectedFolderId) { const data = await window.bookManager.tree(); tree = data.folders; folderTree.replaceChildren(renderTree(tree)); const id = findFolder(select) ? select : tree[0]?.id; if (id) await selectFolder(id) }

async function openDefault(item = selectedItem) { if (!item) throw new Error('请先选择文件'); await window.bookManager.openDocument(item.id); message(`已用系统默认方式打开：${item.displayName}`) }
async function createFolder(parent = selectedFolderId) { const name = await promptValue('新建文件夹', '文件夹名称'); if (!name) return; const result = await window.bookManager.post('/api/folders', { parentId: parent, name }); await refresh(result.folder.id); message('已新建文件夹。') }
async function renameFolder(folder) { const name = await promptValue('重命名文件夹', '新的文件夹名称', folder.name); if (!name) return; await window.bookManager.post(`/api/folders/${folder.id}/rename`, { name }); await refresh(folder.id); message('文件夹已重命名。') }
async function moveFolder(folder) { const target = await chooseFolder('选择移动目标文件夹', folder.id); if (!target) return; await window.bookManager.post(`/api/folders/${folder.id}/move`, { targetFolderId: target }); await refresh(folder.id); message('文件夹已移动。') }
async function deleteFolder(folder) { if (!(await ask('删除文件夹', `删除“${folder.name}”及其子文件夹、镜像文件？底层 Hash 文件不会删除。`, true))) return; await window.bookManager.post(`/api/folders/${folder.id}/delete`, {}); await refresh(folder.parentId || tree[0]?.id); message('文件夹及镜像文件已删除。') }
async function importFile() { const path = await window.bookManager.selectFile(); if (!path) return; const details = await nameDialog('导入文件', null, path); if (!details) return; await window.bookManager.post('/api/import/file', { path, folderId: selectedFolderId, ...details }); await refresh(selectedFolderId); message('已导入文件并创建镜像。') }
async function importFolder() { const path = await window.bookManager.selectFolder('选择要添加的文件夹'); if (!path) return; const text = await promptValue('为文件夹内书籍添加标签', '标签（以逗号分隔，可留空）'); const result = await window.bookManager.post('/api/import/folder', { path, folderId: selectedFolderId, tags: text ? text.split(',') : [] }); await refresh(result.folderId); message(`已添加文件夹，导入 ${result.fileCount} 个文件。`) }
async function renameItem(item = selectedItem) { if (!item) throw new Error('请先选择文件'); const details = await nameDialog('按规则重命名', item); if (!details) return; await window.bookManager.post(`/api/items/${item.id}/rename`, details); await refresh(selectedFolderId); message('已重命名；底层文件未复制或改名。') }
async function moveItem(item) { const target = await chooseFolder('选择移动目标文件夹'); if (!target) return; await window.bookManager.post(`/api/items/${item.id}/move`, { targetFolderId: target }); await refresh(selectedFolderId); message('镜像文件已移动。') }
async function mirrorItem(item) { const target = await chooseFolder('选择镜像目标文件夹'); if (!target) return; await window.bookManager.post(`/api/items/${item.id}/mirror`, { targetFolderId: target }); await refresh(selectedFolderId); message('镜像已创建。') }
async function deleteItem(item) { if (!(await ask('删除文件', `删除当前文件夹中的镜像文件？\n${item.displayName}\n底层 Hash 文件不会删除。`, true))) return; await window.bookManager.post(`/api/items/${item.id}/delete`, {}); await refresh(selectedFolderId); message('已删除当前镜像文件。') }
async function deleteUnderlying(item) { if (!(await ask('删除底层文件', `永久删除“${item.displayName}”的底层文件？所有镜像都会删除，此操作无法撤回。`, true))) return; const result = await window.bookManager.post(`/api/items/${item.id}/delete-underlying`, {}); await refresh(selectedFolderId); message(`已删除底层文件及 ${result.removedItemCount} 个镜像文件。`) }
async function replaceDocument(item) { const path = await window.bookManager.selectFile(); if (!path || !(await ask('替换底层文件', '替换后所有镜像位置会显示新内容。'))) return; await window.bookManager.post(`/api/documents/${item.documentId}/replace`, { path }); await refresh(selectedFolderId); message('底层文档已替换。') }
async function saveText() { if (!editable) { message('当前预览不是可编辑文本。'); return } await window.bookManager.post(`/api/documents/${editable.item.documentId}/replace-text`, { text: editable.editor.value, displayName: editable.item.displayName }); await refresh(selectedFolderId); message('文本已保存为新的 Hash 版本；所有镜像同步更新。') }

function searchResults(result) {
  selectedItem = null; setPreviewTitle(result.kind === 'files' ? '底层文件搜索结果' : result.kind === 'tags' ? '标签搜索结果' : '镜像文件搜索结果')
  const holder = document.createElement('section'); holder.className = 'search-results'; holder.style.cssText = 'height:100%;overflow:auto;padding:10px'
  const list = document.createElement('div'); list.className = 'result-list'
  const row = entry => { const button = document.createElement('button'); button.className = 'result-row'; button.type = 'button'; button.innerHTML = `<span>${html(entry.item.displayName)}</span><small>${html(entry.folderPath)}</small><small>${size(entry.item.size)}</small>`; button.addEventListener('click', () => showItem(entry.item).catch(error)); button.addEventListener('dblclick', () => openDefault(entry.item).catch(error)); button.addEventListener('contextmenu', event => openContext(event, { kind: 'file', item: entry.item })); return button }
  if (result.kind === 'files') {
    const groups = new Map()
    result.results.forEach(entry => { const key = String(entry.item.documentId); groups.set(key, [...(groups.get(key) || []), entry]) })
    groups.forEach(entries => {
      if (entries.length === 1) { list.append(row(entries[0])); return }
      const group = document.createElement('details'); group.className = 'search-group'
      const heading = document.createElement('summary'); heading.className = 'group-row'; heading.textContent = `${entries[0].item.displayName}（${entries.length} 个镜像文件）`
      const mirrors = document.createElement('div'); mirrors.className = 'result-list search-group-items'; entries.forEach(entry => mirrors.append(row(entry)))
      group.append(heading, mirrors); list.append(group)
    })
  } else result.results.forEach(entry => list.append(row(entry)))
  if (!list.childElementCount) list.textContent = '没有匹配结果。'
  holder.append(list); showPreviewNode(holder); message(`搜索完成：${result.results.length} 个匹配项。`)
}
function openSearch() {
  const content = document.createElement('div'); content.className = 'form-stack'
  content.innerHTML = `<p class="dialog-hint">仅填写“底层文件”、“镜像文件”或“标签搜索”其中一项。</p><div class="search-columns"><section class="search-section"><h3>底层文件</h3><label>主标题<input data-kind="files" data-key="mainTitle"></label><label>副标题<input data-kind="files" data-key="subtitle"></label><label>作者名<input data-kind="files" data-key="authors"></label></section><section class="search-section"><h3>镜像文件</h3><label>主标题<input data-kind="mirrors" data-key="mainTitle"></label><label>副标题<input data-kind="mirrors" data-key="subtitle"></label><label>作者名<input data-kind="mirrors" data-key="authors"></label></section></div><section class="search-section"><h3>标签搜索</h3><label>标签名称<div class="tag-input-row"><input data-kind="tags" data-key="query"><button type="button" class="dialog-button" data-action="select-tags">选择标签</button></div></label></section>`
  content.querySelector('[data-action="select-tags"]').addEventListener('click', () => openTagPicker(content.querySelector('input[data-kind="tags"]')).catch(error))
  modal('搜索资料库', content, { wide: true, buttons: [{ label: '取消', action: close => close() }, { label: '搜索', primary: true, action: async close => { const grouped = {}; content.querySelectorAll('input[data-kind]').forEach(input => { (grouped[input.dataset.kind] ||= {})[input.dataset.key] = input.value.trim() }); const active = Object.entries(grouped).filter(([, values]) => Object.values(values).some(Boolean)); if (active.length !== 1) { message('请仅填写一个搜索范围。'); return } close(); const [kind, values] = active[0]; searchResults(await window.bookManager.post('/api/search', { kind, ...values })) } }] })
}
async function crawl() { const source = await promptValue('爬取网络系列数据', '输入网址或 Springer 系列编号'); if (!source) return; message('正在爬取，请稍候…'); const result = await window.bookManager.post('/api/crawl', { source, folderId: selectedFolderId }); await refresh(result.folderId); message(`爬取完成：${result.folderName}，导入 ${result.downloaded} 个文件。`) }
async function dataLocation() { const settings = await window.bookManager.get('/api/settings'); const content = document.createElement('div'); content.className = 'form-stack data-location'; const heading = document.createElement('p'); heading.className = 'dialog-hint'; heading.textContent = '当前数据位置'; const location = document.createElement('input'); location.readOnly = true; location.value = settings.dataHome; location.title = settings.dataHome; const hint = document.createElement('p'); hint.className = 'dialog-hint'; hint.textContent = '迁移目标必须是空文件夹。'; content.append(heading, location, hint); modal('数据位置', content, { buttons: [{ label: '关闭', action: close => close() }, { label: '选择新位置并迁移', primary: true, action: async close => { const path = await window.bookManager.selectFolder('选择新的数据文件夹'); if (!path || !(await ask('迁移数据', `将全部书籍数据迁移到：\n${path}`))) return; await window.bookManager.post('/api/settings/data/migrate', { path }); close(); await refresh(); message('数据已迁移到新位置。') } }] }) }
const cachePresets = {
  '平衡模式': { openDays: 7, renderDays: 30, maxGb: 4, targetGb: 3, clearOpenOnExit: false },
  '节省磁盘模式': { openDays: 0, renderDays: 7, maxGb: 1, targetGb: 0.75, clearOpenOnExit: true },
  '预览优先模式': { openDays: 14, renderDays: 90, maxGb: 10, targetGb: 8, clearOpenOnExit: false },
  '仅容量控制': { openDays: 0, renderDays: 0, maxGb: 4, targetGb: 3, clearOpenOnExit: false },
  '完全手动': { openDays: 0, renderDays: 0, maxGb: 0, targetGb: 0, clearOpenOnExit: false }
}
async function cacheSettings() {
  const current = (await window.bookManager.get('/api/settings')).cache
  const content = document.createElement('div'); content.className = 'form-grid'
  const policyNames = [...Object.keys(cachePresets), '自定义']
  content.innerHTML = `<label class="span-2">清理方案<select class="dialog-select" data-key="policy">${policyNames.map(name => `<option${current.policy === name ? ' selected' : ''}>${name}</option>`).join('')}</select></label><label>打开缓存保留天数<input data-key="openDays" type="number" min="0" value="${current.openDays}"></label><label>渲染缓存保留天数<input data-key="renderDays" type="number" min="0" value="${current.renderDays}"></label><label>总缓存上限（GB）<input data-key="maxBytes" type="number" min="0" step="0.1" value="${(current.maxBytes / 1024 ** 3).toFixed(2)}"></label><label>回收目标容量（GB）<input data-key="targetBytes" type="number" min="0" step="0.1" value="${(current.targetBytes / 1024 ** 3).toFixed(2)}"></label><label class="span-2 cache-exit-toggle"><input data-key="clearOpenOnExit" type="checkbox"${current.clearOpenOnExit ? ' checked' : ''}>退出软件时清理打开缓存</label><p class="dialog-hint span-2">当前占用：打开缓存 ${size(current.usage.open_cache)}；渲染缓存 ${size(current.usage.render_cache)}</p>`
  const value = key => content.querySelector(`[data-key="${key}"]`)
  value('policy').addEventListener('change', () => {
    const preset = cachePresets[value('policy').value]
    if (!preset) return
    value('openDays').value = preset.openDays; value('renderDays').value = preset.renderDays
    value('maxBytes').value = preset.maxGb; value('targetBytes').value = preset.targetGb
    value('clearOpenOnExit').checked = preset.clearOpenOnExit
  })
  modal('缓存管理', content, { buttons: [
    { label: '清理打开缓存', action: async () => { if (await ask('清理缓存', '确定清理全部打开缓存吗？')) { const result = await window.bookManager.post('/api/settings/cache/clear', { cache: 'open_cache' }); message(`已释放 ${size(result.reclaimed)}。`) } } },
    { label: '清理渲染缓存', action: async () => { if (await ask('清理缓存', '确定清理全部渲染缓存吗？')) { const result = await window.bookManager.post('/api/settings/cache/clear', { cache: 'render_cache' }); message(`已释放 ${size(result.reclaimed)}。`) } } },
    { label: '关闭', action: close => close() },
    { label: '保存设置', primary: true, action: async close => { const selected = value('policy').value; const requested = { openDays: Number(value('openDays').value), renderDays: Number(value('renderDays').value), maxGb: Number(value('maxBytes').value), targetGb: Number(value('targetBytes').value), clearOpenOnExit: value('clearOpenOnExit').checked }; const preset = cachePresets[selected]; const matchesPreset = preset && preset.openDays === requested.openDays && preset.renderDays === requested.renderDays && preset.maxGb === requested.maxGb && preset.targetGb === requested.targetGb && preset.clearOpenOnExit === requested.clearOpenOnExit; await window.bookManager.post('/api/settings/cache', { policy: matchesPreset ? selected : '自定义', openDays: requested.openDays, renderDays: requested.renderDays, maxBytes: Math.round(requested.maxGb * 1024 ** 3), targetBytes: Math.round(requested.targetGb * 1024 ** 3), clearOpenOnExit: requested.clearOpenOnExit }); close(); message('缓存设置已保存。') }}]
  })
}
function help() { modal('功能介绍', '<div class="form-stack"><p>添加：导入文件或整个文件夹。</p><p>新建文件夹：在当前文件夹下创建虚拟文件夹。</p><p>打开：用系统默认程序打开文件，或显示底层 Hash 文件。</p><p>按规则重命名：编辑系列、标题、版本、作者、备注与标签。</p><p>爬取系列：输入网址或 Springer 系列编号。</p><p>右键菜单：文件夹可新建、重命名、移动、排序、删除；文件可打开、复制、移动、镜像、替换、删除。</p></div>', { buttons: [{ label: '关闭', primary: true, action: close => close() }] }) }
async function shortcutDialog() { shortcuts = (await window.bookManager.get('/api/shortcuts')).shortcuts || {}; const actions = [['import-file', '导入文件'], ['create-folder', '新建文件夹'], ['open-selected', '打开文件'], ['rename-selected', '按规则重命名'], ['search', '搜索'], ['crawl', '爬取系列'], ['save-text', '保存文本改动']]; const content = document.createElement('div'); content.className = 'shortcut-grid'; actions.forEach(([action, label]) => { const name = document.createElement('div'); name.textContent = label; const input = document.createElement('input'); input.readOnly = true; input.placeholder = '点击后按键设置；退格清除'; input.value = shortcuts[action] || ''; input.addEventListener('keydown', async event => { event.preventDefault(); const accelerator = event.key === 'Backspace' ? '' : `${event.metaKey || event.ctrlKey ? 'CommandOrControl+' : ''}${event.altKey ? 'Alt+' : ''}${event.shiftKey ? 'Shift+' : ''}${event.key.length === 1 ? event.key.toUpperCase() : event.key}`; try { shortcuts = (await window.bookManager.post('/api/settings/shortcuts', { action, accelerator })).shortcuts; input.value = shortcuts[action] || '' } catch (issue) { error(issue) } }); content.append(name, input) }); modal('快捷键', content, { wide: true, buttons: [{ label: '关闭', primary: true, action: close => close() }] }) }

async function contextAction(action) { const target = contextTarget; closeContext(); if (!target) return; const folder = target.folder; const item = target.item; if (action === 'folder-open') return selectFolder(folder.id); if (action === 'folder-create') return createFolder(folder.id); if (action === 'folder-rename') return renameFolder(folder); if (action === 'folder-move') return moveFolder(folder); if (action === 'folder-delete') return deleteFolder(folder); if (action === 'sort-standard' || action === 'sort-series') { sortModes.set(folder.id, action === 'sort-series' ? '序列' : '标准'); await selectFolder(selectedFolderId); message(`当前文件夹及子文件夹已按“${sortModes.get(folder.id)}”排序。`); return } if (action === 'file-open') return openDefault(item); if (action === 'file-location') return window.bookManager.showLocation(item.id); if (action === 'file-copy') { await window.bookManager.copyFileName(item.displayName); message('已复制文件名。'); return } if (action === 'file-delete') return deleteItem(item); if (action === 'file-delete-underlying') return deleteUnderlying(item); if (action === 'file-rename') return renameItem(item); if (action === 'file-move') return moveItem(item); if (action === 'file-mirror') return mirrorItem(item); if (action === 'file-replace') return replaceDocument(item) }
async function action(name) { closeMenus(); if (name === 'import-file') return importFile(); if (name === 'add-folder') return importFolder(); if (name === 'create-folder') return createFolder(); if (name === 'open-selected') return openDefault(); if (name === 'show-location-selected') { if (!selectedItem) throw new Error('请先选择文件'); return window.bookManager.showLocation(selectedItem.id) } if (name === 'rename-selected') return renameItem(); if (name === 'save-text') return saveText(); if (name === 'crawl') return crawl(); if (name === 'data-location') return dataLocation(); if (name === 'cache-settings') return cacheSettings(); if (name === 'help') return help(); if (name === 'shortcuts') return shortcutDialog(); if (name === 'search') return openSearch() }

function moveListSelection(event, container, folderIdForParent) {
  if (!['ArrowUp', 'ArrowDown', 'ArrowLeft'].includes(event.key)) return
  const rows = [...container.querySelectorAll('button')].filter(button => button.offsetParent !== null); if (!rows.length) return
  if (event.key === 'ArrowLeft') {
    const folderId = folderIdForParent(); const parent = findFolder(folderId)?.parents.at(-1)
    if (parent) { event.preventDefault(); selectFolder(parent.id).catch(error) }
    return
  }
  event.preventDefault()
  const current = rows.indexOf(document.activeElement); const next = current < 0 ? (event.key === 'ArrowDown' ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)))
  rows[next].focus(); if (container === contentList) markContentSelection(rows[next])
}
contentList.addEventListener('keydown', event => moveListSelection(event, contentList, () => selectedFolderId))
folderTree.addEventListener('keydown', event => moveListSelection(event, folderTree, () => Number(document.activeElement?.dataset.folderId) || selectedFolderId))

appMenu.querySelectorAll('summary').forEach(summary => summary.addEventListener('click', () => { for (const menu of appMenu.querySelectorAll('details')) if (menu !== summary.parentElement) menu.open = false }))
appMenu.addEventListener('click', event => { const button = event.target.closest('button[data-action]'); if (button) action(button.dataset.action).catch(error) }); $('#search-button').addEventListener('click', () => action('search').catch(error)); contextElement.addEventListener('click', event => { const button = event.target.closest('button[data-action]'); if (button) contextAction(button.dataset.action).catch(error) }); document.addEventListener('pointerdown', event => { if (!appMenu.contains(event.target)) closeMenus(); if (!contextElement.hidden && !contextElement.contains(event.target)) closeContext() }, true); window.addEventListener('resize', () => { closeMenus(); closeContext() })
window.addEventListener('keydown', event => { if (event.target.matches('input,textarea,select')) return; const key = `${event.metaKey || event.ctrlKey ? 'CommandOrControl+' : ''}${event.altKey ? 'Alt+' : ''}${event.shiftKey ? 'Shift+' : ''}${event.key.length === 1 ? event.key.toUpperCase() : event.key}`; const name = Object.entries(shortcuts).find(([, accelerator]) => accelerator === key)?.[0]; if (name) { event.preventDefault(); action(name).catch(error) } })
async function bootstrap() { await refresh(); shortcuts = (await window.bookManager.get('/api/shortcuts')).shortcuts || {}; message('资料库已连接') }
bootstrap().catch(error); window.bookManager.onMenuAction(payload => { if (payload?.name) action(payload.name).catch(error) })
