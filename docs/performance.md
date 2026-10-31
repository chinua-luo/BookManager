# 性能基准与优化记录

本文记录 BookManager P0 性能优化的可复现基准。基准脚本是
[`tools/benchmark_library.py`](../tools/benchmark_library.py)，只使用标准库，
在临时目录中生成一份合成书库并同时测量数据层与本地 HTTP API。

## 复现方式

```bash
python3 tools/benchmark_library.py --items 5000 --repeats 5
python3 tools/benchmark_library.py --items 5000 --repeats 3 --p1   # 额外测导入/复制/缓存/启动
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


## P1 优化与实测

P1 阶段解决的是“单个大文件、整目录导入、启动与缓存、前端首屏”这类非请求级问题。
以下场景由 `--p1` 基准复跑，环境同上（重复 3–5 次取代表值）。

| 场景 | 优化前 | 优化后 | 变化 |
|---|---:|---:|---:|
| 导入 128 MiB 文件总耗时 | 0.225 s | **0.109 s** | 约 2.1× |
| 导入期间另一个写请求最大延迟 | 146.3 ms | **1.5 ms** | 约 97× |
| 打开 256 MiB 文件的缓存复制 | 61.4 ms（`shutil.copy2`） | **9.5 ms（APFS clone）** | 约 6.5× |
| 整目录导入 400 个文件 | 0.81 s | **0.26 s** | 约 3.1× |
| `sync_open_cache_changes`（200 个未修改副本 / 200 MiB） | 186.3 ms | **23.3 ms** | 约 8× |
| `cache_usage`（200 个副本） | 16.4 ms | 13.1 ms | 约 1.25× |
| 启动 BridgeServer（含 200 MiB 过期缓存） | 127.9 ms | **5.2 ms 即可用** | 约 24× |
| 启动缓存清理（后台完成） | 阻塞启动 | 111.2 ms（后台，不阻塞窗口） | 窗口更早可用 |
| `check_storage`（20000 个文件夹） | 4.2 ms 全量检查 | **1.1 ms（正常退出后跳过）** | 约 3.8× |

> `check_storage` 的差距随数据库页数增长而放大；这里用 20000 个文件夹模拟较大库。

### P1-1 导入：一遍读取 + 事务外复制

- 原实现先 `sha256_file` 读一遍源文件，再在 `BEGIN IMMEDIATE` 写事务里复制一遍并复核哈希，
  大文件导入期间其他写请求会被长时间阻塞（实测最大 146 ms）。
- 现改为 `prepare_blob`：在临时目录“边复制边计算哈希”（一次读取），随后用短事务登记；
  `add_local_file` / `replace_document_content` / `import_file` 全部走这条路径。
  失败时由 `release_unreferenced_blob` 清理未被引用的 staged blob。
- 结果：读放大减半，写锁持有时间从 146 ms 级降到 1–2 ms；导入总耗时也下降约一半。

### P1-2 整目录导入：分批提交 + 进度/取消

- 原实现每个文件单独提交、整请求无进度、无法取消。
- 现在先扫描目录统计总数，再按 100 个文件一批事务提交；进度通过
  `GET /api/import/folder/progress/{jobId}` 提供（250 ms 轮询），
  `POST /api/import/folder/cancel/{jobId}` 可取消，取消会删除本次创建的根目录，
  不保留部分导入。
- 结果：400 个文件 0.81 s → 0.26 s（减少逐文件 fsync），前端显示实时进度并可取消。

### P1-3 打开外部程序：写时复制克隆

- 原先每次“用系统默认程序打开”都 `shutil.copy2` 整个 Blob。
- 现在 macOS 使用 `cp -c -p`（APFS clone），Linux 使用 `cp --reflink=auto -p`，
  失败回退 `shutil.copy2`；克隆后用户编辑产生独立副本，不会修改底层 Blob。
- 结果：256 MiB 文件从约 61 ms 降到约 9.5 ms；文件越大收益越明显。

### P1-4 启动与缓存：异步清理 + 干净关机标记 + 未修改副本跳过哈希

- 启动缓存清理改为后台线程，窗口不再等缓存删除完成；新增
  `wait_for_startup_cleanup()` 供测试/关闭流程等待。
- 正常退出时写入 `.bookmanager-clean-shutdown` 标记，下次启动跳过整库
  `PRAGMA quick_check`；异常退出（无标记）仍会完整检查。
- 打开缓存 manifest 记录 copy 时的 size/mtime；未修改且文档哈希未变的副本
  直接跳过 SHA-256，只有真正编辑过的副本才重新哈希并写回；
  写回后刷新 manifest（含合并后的 document_id）。
- `cache_usage` 改为一次目录遍历，而不是每个缓存条目分别 rglob。
- 结果：启动可用时间约 24× 提升；200 MiB 未修改缓存的同步从 186 ms 降到 23 ms。

### P1-5 前端：导入进度、树事件委托、渲染器懒加载

- 文件夹导入显示实时进度对话框（role=status），支持取消；轮询在请求结束时清理，
  切换页面不会残留定时器。
- 文件夹树由“每个节点绑定 click/contextmenu/toggle”改为容器级事件委托，
  减少大量节点的事件监听器；展开状态/当前目录/键盘导航行为保持不变。
- 预览库与 7 个渲染器改为首次打开非文本预览时动态 `import()`；
  启动与文本/DJVU 预览不再加载这些资源。
- 本环境无法执行 `pnpm build`，因此前端首屏包体积/真实 Electron 端到端收益未在此测量；
  代码通过 147 个 `node:test` 与语法检查，正式效果需在可构建环境复测（详见 README）。

## P2：文本预览、WAL、请求并发与搜索结果渲染

P2 目标是不改业务语义、只把剩余的“资源无上限”点收口。基准复用
`tools/benchmark_library.py --p2`（5000 个合成镜像 + 10 MiB 文本文件）。

### P2-1 大文本预览只读取前 2 MiB

- 原先文本预览 `fetch(documentUrl)` 后 `response.text()` 读取整个文件；10 MiB
  日志会完整进入渲染进程再生成超长文本节点。
- bridge 的 `_stream_file` 本来就支持 `Range`；现在渲染层对 `size > 2 MiB` 的
  文本请求 `Range: bytes=0-2097151`（`electron/src/text-preview.js`），预览底部
  显示“文件较大（X MB），仅显示前 2 MB”。小文件/未知大小仍走完整读取。
- 实测同一 10 MiB 文本：HTTP 响应体 **10,485,760 B → 2,097,152 B（约 −80%）**，
  本地回环耗时 12.1 ms → 4.0 ms；渲染进程同时少解码约 8 MiB 文本。

### P2-2 SQLite WAL + busy_timeout

- schema 升到 v4，迁移时执行一次 `PRAGMA journal_mode = WAL`（模式写入数据库文件
  并持久化）；每个连接设置 `PRAGMA busy_timeout = 5000`。
- WAL 让每个请求新建的只读连接在导入/缓存写入提交期间继续读取，不再等待
  rollback journal 的提交锁；写事务之间仍有 5 s 重试窗口。
- 实测 5000 项书库、写线程连续 500 次小事务提交时，读取线程
  `list_items` p50 54.7 → 54.4 ms（基本不变），最坏样本 261.5 → 69.5 ms；
  单次运行噪声较大，但长尾阻塞明显收敛。WAL 文件已包含在迁移复制的
  `LIBRARY_DATA_NAMES` 中，旧库升级测试验证数据不丢失。

### P2-3 bridge 请求并发上限

- `ThreadingHTTPServer` 原先每个连接都新建线程，突发请求会同时创建大量线程和
  `LibraryStore`/SQLite 连接。
- 新增 `MAX_CONCURRENT_REQUESTS = 16` 的 `BoundedSemaphore`，在每个连接的处理
  线程结束时释放；监听 backlog 提高到 64。超过上限的请求排队等待，不受理半开
  请求，也不丢弃连接。
- 回归测试同时发 32 个 `/api/health`，峰值并发处理线程 ≤ 16、32 个请求全部
  返回 200，随后仍可正常服务。

### P2-4 搜索结果分块渲染

- 搜索 API 仍最多返回 500 条；前端原先一次创建全部结果行（镜像/标签最多 500 行，
  底层文件按文档分组）。
- 新增 `electron/src/search-result-view.js`：先渲染 100 个渲染单元，底部
  “显示更多（剩余 N 项）”每次追加 100 个；分组逻辑与点击行为不变。
- 初始 DOM 行数从最多 500 降到 100，结果很多时展开成本由用户按需触发。
  本环境无法构建 Electron，收益以 `node:test` 行为测试与代码路径验证为准。

## 仍然保留的已知上限

- 未实现虚拟列表：前端已加载的页面会保留在 DOM 中，超大文件夹滚动到底后仍会
  占用较多内存；下一步可在增量加载基础上加固定行高虚拟滚动。
- 文件夹排序目前按页内排序，跨页边界不保证全局有序；数据量和排序需求变大时
  应改为服务端全局排序 + 游标分页。
- 标签搜索仍会对 tags 表做 `LIKE`（表很小，14 ms 级）；书库到数万级且标签搜索
  成为瓶颈时可评估 FTS5 trigram。
- 前端只做了事件委托与渲染器懒加载，未做虚拟列表；首屏资源的真实收益需在可构建环境测量。
- 本机无法构建 Electron 产物（原生绑定签名限制），本轮前端渲染改动只经过
  node:test 与语法检查；正式使用前需在可构建环境执行 `pnpm build` 后做一次
  手工验证。
- 文本预览只限制了纯文本路径；PDF/Office/EPUB/图片仍整份交给对应渲染器，
  超大文件的峰值内存由第三方渲染器决定。

## 回归保护

- 新增 `tests/test_schema_versioning.py`：升级只执行一次、缺表自愈、
  每请求不跑维护扫描。
- 新增 `tests/test_store_queries.py`：索引存在且被查询计划使用、
  标签批量查询语句数上限、递归 CTE 统计、分页切片。
- 新增 `tests/test_search_queries.py`：字段/大小写/通配符字面量、
  镜像分组、截断标记、标签搜索。
- `tests/test_bridge_contract.py` 覆盖分页参数校验、上限钳制与
  `truncated` 返回。
- `tests/test_schema_versioning.py` 覆盖 WAL 模式、busy_timeout 与 v3→v4
  升级不丢数据；`tests/test_bridge_concurrency.py` 覆盖 32 请求突发下的并发
  上限与全部成功返回。
- `electron/tests/text-preview.test.mjs`、`electron/tests/search-result-view.test.mjs`
  覆盖 Range 请求边界、截断提示、文件分组与 100 项分块。
- `tools/benchmark_library.py` 可随时复跑上述场景，防止后续退化。
