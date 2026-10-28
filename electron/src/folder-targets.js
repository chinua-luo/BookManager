export const MAX_IMPORT_TARGETS = 100

export function folderTargets(tree, ids = null) {
  const paths = new Map()
  function visit(nodes, parents = []) {
    for (const folder of nodes) {
      const path = [...parents, folder.name]
      paths.set(folder.id, path.join(' / '))
      visit(folder.children || [], path)
    }
  }
  visit(tree)
  return [...new Set(ids || paths.keys())].map(id => ({ id, path: paths.get(id) || `已删除的文件夹（编号：${id}）`, missing: !paths.has(id) }))
}
