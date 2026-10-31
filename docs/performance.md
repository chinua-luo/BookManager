# 性能基准与优化记录

本文记录 BookManager P0 性能优化的可复现基准。基准脚本是
[`tools/benchmark_library.py`](../tools/benchmark_library.py)，只使用标准库，
在临时目录中生成一份合成书库并同时测量数据层与本地 HTTP API。

## 复现方式

```bash
python3 tools/benchmark_library.py --items 5000 --repeats 5
python3 tools/benchmark_library.py --items 5000 --repeats 5 --no-api   # 仅数据层
python3 tools/benchmark_library.py --items 5000 --repeats 5 --json     # 机器可读
```

脚本会：

1. 在临时目录创建书库，导入 1 个真实小文件作为底层文档；
2. 插入 N 个共用一个 `document` 的合成镜像（含标签、名称元数据）；
3. 测量 `LibraryStore` 打开、文件夹列表、文件夹统计、三种搜索；
4. 启动真实 bridge 进程，测量 `/api/tree`、`/api/settings`、`/api/tags`、
   `/api/folders/{id}/contents` 与三种搜索，并记录响应体大小。

本记录环境：macOS（APFS）、Python 3.14、本机 SSD、5000 个合成镜像、重复 5 次取 p50。
优化前数字为同一脚本在优化前提交上运行（重复 3 次取 p50）。数值会随机器变化，
请在同一台机器上对比同一脚本的改动前后结果。

## 结果总览

### API / 用户可感知路径

| 场景 | 优化前 p50 | 优化后 p50 | 变化 |
|---|---:|---:|---:|
| `GET /api/tree` | 196.3 ms | 1.6 ms | **约 123×** |
| `GET /api/settings` | 193.6 ms | 1.2 ms | **约 161×** |
| `GET /api/tags` | 193.4 ms | 1.8 ms | **约 107×** |
| 打开文件夹（5000 项，旧：全量返回） | 353.0 ms / 约 2.2 MB | **8.2 ms / 93.4 KiB（首页 200 项）** | 约 43× |
| `POST /api/search`（底层文件） | 322.8 ms | 66.5 ms | 约 5× |
| `POST /api/search`（镜像文件） | 232.3 ms | 13.2 ms | 约 18× |
| `POST /api/search`（标签） | 221.0 ms | 19.9 ms | 约 11× |

### 数据层

| 场景 | 优化前 p50 | 优化后 p50 | 变化 |
|---|---:|---:|---:|
| `LibraryStore` 打开/关闭 | 189.8 ms | **0.5 ms** | 约 380× |
| `list_items`（全部 5000 项，测试/兼容路径） | 115.7 ms | 51.6 ms | 约 2.2× |
| 文件夹首页（200 项，HTTP） | —（不存在） | 7.6 ms / 93.4 KiB | 新增 |
| `search_unique_documents`（5000 项） | 123.2 ms | 59.9 ms | 约 2× |
| `search_mirror_items` | 31.3 ms | 6.7 ms | 约 4.7× |
| `search_tagged_items` | 25.4 ms | 14.0 ms | 约 1.8× |
| `folder_stats` | 1.3 ms | 1.9 ms | 基本不变（本来就只查聚合） |

> 说明：`search_unique_documents` 的合成数据中 5000 个镜像都属于同一个文档，
> 属于对“底层文件搜索”最不利的分布；实际书库里同一文档通常只有 1–3 个镜像，
> 该项会更快。

## 每项优化做了什么

### 1. 版本化 schema，把全量扫描移出请求路径

- 每次 `LibraryStore.__init__` 原本都会重新执行建表脚本、全量
  `_migrate_item_name_data`（遍历并重写所有 items 的 name_data）以及对 blobs
  目录的 `rglob` 清理。
- 现以 `PRAGMA user_version` 记录 schema 版本：新库或旧库只升级一次，
  后续请求只建立连接并做一次轻量完整性检查；陈旧临时文件清理只在启动
  用的长生命周期 store 上执行（`run_maintenance=True`），bridge 的每请求
  store 跳过（`run_maintenance=False`）。
- 因此 `/api/tree`、`/api/settings`、`/api/tags` 这类小请求从约 190 ms
  降到 1–2 ms，且不再随条数线性增长。

### 2. 索引、批量标签与路径查询

- 新增 `idx_items_folder_name(folder_id, display_name)`：文件夹列表的过滤与
  排序直接走索引（测试中用 `EXPLAIN QUERY PLAN` 断言）。
- 列表/搜索结果的标签由“每条一次查询”改为按 500 个 id 分块批量查询。
- 文件夹路径由逐层查询改为一次读取 `folders` 表后在内存中记忆化解析；
  子文件夹集合改用递归 CTE 一次查询。
- `name_data` 由每行两次 `json.loads` 改为一次解析。

### 3. 文件夹内容分页 + 前端增量加载

- API 新增 `offset/limit`（默认 0/200，上限 1000）并返回
  `pagination: {offset, limit, total, hasMore}`；`/api/folders/{id}/contents`
  默认只返回第一页。
- 前端每页 200 项增量渲染：滚动接近底部（300 px，rAF 节流）自动加载下一页，
  最后一行按 `↓` 也会加载；计数显示“已加载 X / Y 项”。
- 切换文件夹使用请求序号弃用过期响应，避免旧页追加到新目录。
- 效果：5000 项目录首次打开从约 2.2 MB / 353 ms 降到约 93 KiB / 8 ms。

### 4. 搜索下推到 SQL，并显式报告截断

- `items` 新增三列规范化搜索列（`main_title_norm` / `subtitle_norm` /
  `authors_norm`），在写入与旧库升级时维护；搜索条件在 SQL 中过滤，
  不再“先取 5 万行再在 Python 里筛选”。
- 用户输入中的 `%`、`_`、`\` 按字面匹配；结果达到上限时返回
  `truncated: true`，前端状态栏提示“结果过多，请细化搜索条件”。
- 移除了原先 5 万行后的静默漏结果问题。

## 仍然保留的已知上限

- 未实现虚拟列表：前端已加载的页面会保留在 DOM 中，超大文件夹滚动到底后仍会
  占用较多内存；下一步可在增量加载基础上加固定行高虚拟滚动。
- 文件夹排序目前按页内排序，跨页边界不保证全局有序；数据量和排序需求变大时
  应改为服务端全局排序 + 游标分页。
- 标签搜索仍会对 tags 表做 `LIKE`（表很小，14 ms 级）；书库到数万级且标签搜索
  成为瓶颈时可评估 FTS5 trigram。
- 大文件导入仍是单遍读取前的“先哈希再复制”两次读取、复制位于写事务内（P1 项，
  本次未改）；打开缓存仍整文件复制（可改用 APFS clonefile / reflink）。
- 本机无法构建 Electron 产物（原生绑定签名限制），本轮前端分页只经过
  node:test 与语法检查；正式使用前需在可构建环境执行 `pnpm build` 后做一次
  手工验证。

## 回归保护

- 新增 `tests/test_schema_versioning.py`：升级只执行一次、缺表自愈、
  每请求不跑维护扫描。
- 新增 `tests/test_store_queries.py`：索引存在且被查询计划使用、
  标签批量查询语句数上限、递归 CTE 统计、分页切片。
- 新增 `tests/test_search_queries.py`：字段/大小写/通配符字面量、
  镜像分组、截断标记、标签搜索。
- `tests/test_bridge_contract.py` 覆盖分页参数校验、上限钳制与
  `truncated` 返回。
- `tools/benchmark_library.py` 可随时复跑上述场景，防止后续退化。
