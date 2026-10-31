import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SEARCH_RESULT_CHUNK_SIZE,
  nextSearchChunk,
  searchMoreLabel,
  searchResultUnits
} from '../src/search-result-view.js'

const entry = (id, documentId, name = `book-${id}`) => ({
  item: { id, documentId, displayName: name },
  folderPath: 'Root',
  size: 10
})

test('non-file searches keep one render unit per entry', () => {
  const units = searchResultUnits('mirrors', [entry(1, 10), entry(2, 11)])
  assert.deepEqual(units.map(unit => unit.kind), ['entry', 'entry'])
  assert.equal(units[0].entries[0].item.id, 1)
})

test('file searches group mirrors of one document and keep singletons as entries', () => {
  const units = searchResultUnits('files', [entry(1, 10), entry(2, 10), entry(3, 11)])
  assert.equal(units.length, 2)
  assert.equal(units[0].kind, 'group')
  assert.deepEqual(units[0].entries.map(value => value.item.id), [1, 2])
  assert.equal(units[1].kind, 'entry')
  assert.equal(units[1].entries[0].item.id, 3)
  assert.deepEqual(searchResultUnits('files', null), [])
})

test('chunks render at most 100 units at a time', () => {
  assert.equal(SEARCH_RESULT_CHUNK_SIZE, 100)
  const units = Array.from({ length: 250 }, (_, index) => ({ kind: 'entry', entries: [entry(index, index)] }))
  const first = nextSearchChunk(units)
  assert.equal(first.units.length, 100)
  assert.equal(first.rendered, 100)
  assert.equal(first.remaining, 150)
  assert.equal(first.hasMore, true)
  const second = nextSearchChunk(units, first.rendered)
  assert.equal(second.units.length, 100)
  assert.equal(second.rendered, 200)
  const last = nextSearchChunk(units, second.rendered)
  assert.equal(last.units.length, 50)
  assert.equal(last.hasMore, false)
  assert.equal(last.remaining, 0)
})

test('the chunker tolerates stale offsets and empty results', () => {
  const units = [{ kind: 'entry', entries: [entry(1, 1)] }]
  assert.equal(nextSearchChunk(units, 99).units.length, 0)
  assert.equal(nextSearchChunk(null, 0).hasMore, false)
  assert.deepEqual(nextSearchChunk(units, -5).units, units)
  assert.equal(searchMoreLabel(150), '显示更多（剩余 150 项）')
  assert.equal(searchMoreLabel(-1), '显示更多（剩余 0 项）')
})
