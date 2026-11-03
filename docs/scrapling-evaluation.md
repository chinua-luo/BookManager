# Scrapling 爬虫框架评估与渐进接入方案

评估日期：2026-11-03。BookManager 基线：`1fe60dcd224c077566450e51eaa217525e824a3b`；Scrapling 源码基线：`95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0`（当时默认分支）。最新发布版本为 v0.5，发布于 2026-10-17；默认分支不等于发布制品，正式试点必须重新核对 v0.5 制品并固定依赖。

本文是源码分析和实施建议，未安装 Scrapling、未进行真实站点对照抓取，也未验证其性能或挑战页处理成功率。本次只增加分析文档及 README 入口，不修改爬取行为。

## 结论与决策

**Scrapling 可以独立使用；BookManager 值得选择性接入，当前不建议整体替换。**

- 它本身就是 Python 库，无须运行上游 CLI、MCP 服务或整个项目。优先通过依赖和适配器复用，避免复制源码后长期维护分叉。
- 对当前“小规模公开文档下载并入库”场景，保留现有安全请求、流式文件下载和入库逻辑，收益最明确的试点是复杂页面的解析和站点规则。
- 当真实需求变为大量分页、动态渲染或长期抓取任务时，再分别评估 HTTP 会话、浏览器和 Spider 调度层。
- 如果采样发现主要失败原因是登录、机构授权或页面本来没有公开下载地址，换框架也不能解决，应维持现状。

推荐推进顺序：采集失败样本 → 抽出解析接口 → Scrapling Selector 对照试点 → 按证据决定是否接入其他层。解析试点也必须允许结论为“不引入依赖”。

## 一、能否“独立摘出来”

| 方式 | 可行性与依赖 | 建议 |
| --- | --- | --- |
| 仅用 `scrapling.parser.Selector` | 安装基础包即可，无需浏览器；基础依赖包括 lxml、cssselect、orjson、tld、w3lib、typing_extensions | 首选试点 |
| 用 Fetcher / FetcherSession | 请求底层为 curl_cffi；官方 `fetchers` extra 同时包含 playwright、patchright、browserforge 等浏览器相关依赖 | 有会话或网络兼容需求后再试 |
| 用 StealthySession | 需要浏览器运行环境、生命周期管理及额外的请求边界控制；浏览器二进制的安装是额外部署事项 | 可选独立工作进程，不作默认后端 |
| 用 Spider | 可脱离上游应用，但依赖自己的 engine、scheduler、session、request/response、parser 等模块 | 多页任务确有需求时才考虑 |
| 复制 `spiders/` 或单个 fetcher 文件 | 文件之间及与 parser/engines/core 有交叉依赖；SessionManager 使用 FetcherSession 的内部客户端类型与方法 | 不建议当成可直接搬运的独立文件夹 |

Scrapling 要求 Python >=3.10，与 BookManager 的声明下限一致，但这不代表各平台的原生依赖和浏览器部署已验证。`pip install scrapling` 与 `scrapling[fetchers]` 的依赖成本差别明显；“只用 HTTP Fetcher”也不能据此宣称官方安装方案完全没有浏览器相关依赖。

上游许可证为 BSD-3-Clause。若复制、修改或随软件分发，应保留其版权、条件和免责声明，不应把上游作者名作为产品背书；项目继续保留第三方声明。复制源码还需要自行追踪后续修复，因此没有明确需求时优先依赖复用。[S1][S2][S3]

## 二、两边实际负责的工作

BookManager 当前链路：

```text
Electron 爬取对话框及递归确认
  → POST /api/crawl
  → crawl_into_library
  → SafeHttpClient：URL 校验、DNS 固定、重定向校验
  → LinkCollector：title/h1 与 a[href]
  → 候选链接识别、可选一层同源页面
  → HEAD 预检、限额流式下载、安全文件名
  → LibraryStore.add_local_file：Blob、文档、文件夹与元数据
  → CrawlResult、公开响应、用户提示与操作日志
```

Scrapling 的主要链路：`Spider → Scheduler → CrawlerEngine → SessionManager → HTTP/浏览器 → Response(Selector) → parse → item/下一批 Request`。它处理网页采集和结构化结果，不提供 BookManager 的书库数据模型与入库事务。[S4][S5][S6]

| 能力 | BookManager 现状 | Scrapling 能提供什么 | 替换判断 |
| --- | --- | --- | --- |
| 静态 HTML 解析 | 标准库 HTMLParser；只收集 title/h1、href、title/aria-label，不采集锚点正文作为 label | CSS、XPath、文本查询、DOM 导航、自适应定位 | 对复杂站点有价值；简单链接页未必需要 |
| 文档候选识别 | 依据部分扩展名、download 路径和 Springer PDF 路径；可接受的文件类型比主动发现的类型更多 | LinkExtractor 有规则、域名、扩展名过滤 | 必须定制，默认配置会过滤 PDF、ZIP 和多种 Office 文档 |
| HTTP 与会话 | urllib/http.client；串行；没有显式跨请求 CookieJar 或连接池复用 | curl_cffi 会话、Cookie、浏览器请求兼容能力 | 可选价值，不证明目标站点成功率更高 |
| 动态页面 | 不执行 JS | 浏览器渲染、页面交互、等待选择器、网络记录 | 确认下载地址依赖 JS 后才有明显价值 |
| 调度 | 普通模式只取当前页直链；递归只一层且最多 5 个子页面 | 并发、队列去重、延迟、AutoThrottle、robots、检查点 | 适合扩大任务规模，当前规模收益有限 |
| 网络边界 | 公网地址校验、固定 DNS、逐跳重定向检查、递归严格同源 | HTTP 默认 `follow_redirects="safe"`；Spider 域名过滤允许子域 | 不能视为现有安全边界的等价替换 |
| 大文件 | 分块写磁盘；按实际读取量限制 | 标准 HTTP Response 转换读取 `response.content` | 文件下载保留现有链路，不能直接改成读取 Response.body |
| 书库、命名与统计 | 原生入库、失败后 HTML 保存、七项原因统计、日志、脱敏提示 | 结构化 item 导出和框架统计 | 保留业务层，编写明确映射 |
| 部署 | 爬虫只依赖标准库；整个 Python 项目另有 pypdf | 基础包增加原生扩展；fetchers/浏览器增加更多依赖 | 用安装体积、启动时间和平台测试衡量成本 |

本地依据：`bookmanager/crawler.py`、`bookmanager/bridge.py::_crawl`、`bookmanager/store.py::add_local_file`、`electron/src/recursive-crawl.mjs`、`electron/src/main.js::crawl`、`electron/user-errors.mjs::crawlSummary`、`requirements.txt`、`tests/test_crawler_security.py`。上游依据见 [S2][S4]–[S9]。

## 三、替换时容易造成的行为退化

1. **默认不保留文档链接。** `LinkExtractor.IGNORED_EXTENSIONS` 包括 pdf、zip、xlsx、docx 等。用于文档发现时要显式设置 `deny_extensions`，之后仍通过本项目的候选识别及安全策略；不能直接采用通用网页爬虫的默认过滤器。
2. **域名白名单不等于同源。** 上游 `_is_domain_allowed` 允许指定域名的子域；本项目递归比较 scheme/host/effective port 三元组。还要覆盖初始跳转、每一跳重定向和实际连接地址。
3. **已有 SSRF 防护不等于完全等价。** 上游静态请求默认把 `"safe"` 传给 curl_cffi 的重定向参数，不能说它完全没有防护；但本次未验证该依赖对初始地址、多 DNS 答案、重解析、代理及浏览器子资源的全部行为。上线前必须证明与现有固定 DNS 策略等价，单纯在请求前调用 `validated_url()` 不够。
4. **链接提取还要再做协议校验。** 上游 LinkExtractor 接受的 scheme 集合包含 `file`；本项目只接受 HTTP(S)。URL canonicalization 默认还会排序查询参数，签名下载地址应保留原始请求 URL，去重键与请求 URL 分开处理。
5. **Response 不是流式文件下载器。** 标准 HTTP 路径从 curl_cffi 的 `response.content` 构造 Selector Response。底层可配置不代表公开调用就实现了本项目的下载上限；收到全部内容再判断大小无法替代分块停止。浏览器网络记录的 body 限制也不能当成整次抓取的流量或磁盘预算。
6. **框架请求数不等于实际网络请求数。** 重试、重定向、HEAD、robots、浏览器子资源均可能额外发请求。配额应在实际发送前统一扣减；并发预留预算，失败也不能把已耗用的网络预算返还。
7. **框架去重与书库去重不同。** URL 指纹不能替代文件内容哈希。检查点恢复也不能保证入库恰好一次，需要任务 ID、下载记录及幂等入库策略。
8. **默认能力需要显式开启。** 上游 `robots_txt_obey=False`、`autothrottle_enabled=False`、`download_delay=0`。引入 Spider 不等于已经具备期望的站点限速策略。
9. **自适应定位可能匹配错误区域。** 当前默认关闭。若启用，应使用站点隔离的可写存储，并用文件类型、标题或标识符复核；不能把“找到相似元素”视为“找到正确书籍”。
10. **恢复状态需要信任边界。** 上游 checkpoint 使用 pickle；只加载应用自行生成、保存在私有任务目录中的文件，不接收外部提供的检查点。Scrapling 的待处理请求恢复也不负责恢复已导入文件状态。

以上是根据源码识别的集成风险与待验证事项，不是声称已在上游复现漏洞。[S4]–[S10]

## 四、推荐方案：解析层先行，下载与入库保留

### 阶段 0：确认值得解决的问题（估计 1–2 人日）

整理 20–30 个有代表性的公开页面：静态直链、Springer 系列/书籍页、分页、JS 加载页、无扩展名下载和不可公开获取页面。每个样本人工标注文档真值及失败类别，不把原本不可访问的文档算成漏抓。

记录有效文档链接召回率、错误候选比例、成功入库数、网络请求数、耗时、内存峰值，以及失败原因。保留脱敏 HTML fixtures；Cookie、访问令牌和带敏感查询串的下载 URL 不进入仓库。真实站点验证单独执行，自动化默认使用可重复 fixtures。

### 阶段 1：建立可替换解析接口（估计 2–3 人日）

在现有 `crawl_into_library` 内抽出 `PageExtractor.extract(html, final_url) → PageInfo(title, links)`，`links` 统一保留 href、label 和可选来源位置。先用现有 LinkCollector 实现接口，维持行为，再提供 `ScraplingPageExtractor`。

- 安装并锁定经核对的基础包和依赖版本；不安装 `[all]`，不接入 MCP。
- 输入仍来自 `_read_limited`，限制 HTML 大小；Selector 采用 `adaptive=False`、`huge_tree=False`。
- 第一组测试保持当前 title/h1、href 和 label 兼容；锚点文本、站点规则带来的改善单独验收。
- 解析后仍调用本项目的 `download_candidates` / `recursive_link_candidates`、SafeHttpClient、文件名净化、限额下载和 `add_local_file`。
- 用内部配置选择 `stdlib` 或 `scrapling`；默认保持现有实现。缺少可选依赖时可回退并记录原因，解析错误应可诊断，不能静默吞掉。
- 给 Springer 等站点增加独立规则提取器，只识别可公开访问的下载入口，不将通用解析器当作站点业务规则。

建议初始验收门槛：静态 fixtures 的现有正确候选全部保留；复杂页样本有效链接召回率比现状至少提升 10 个百分点且不增加错误入库；全部安全与契约测试通过。性能、安装体积和内存门槛先在阶段 0 定基线后明确。这里的数值是建议的决策阈值，不是实测收益。

如果基础包的额外能力没有用到，直接改进 LinkCollector 或使用更小的解析依赖可能更划算；这应作为试点对照组。

### 阶段 2：按问题选择 HTTP 或浏览器扩展

**HTTP 会话试点（估计 3–5 人日，不含底层网络安全兼容工作）：** 仅当 Cookie、会话复用或 HTTP 兼容是主要失败原因时开展。定义独立 `PageFetcher`，验证 curl_cffi 的地址固定、TLS 主机名、重定向与预算机制；不能证明等价时继续使用 SafeHttpClient。文件下载继续流式处理。如果页面链接需要会话 Cookie，须显式设计受域和路径限制的凭据交接，不能发现链接后丢掉会话，也不能向跨源下载盲目转发 Cookie。

**浏览器试点（估计 5–8 人日，不含跨平台浏览器打包）：** 仅对已确认的动态公开页面按站点开启，用独立工作进程、临时 profile、硬超时和可取消任务管理。浏览器主页面、iframe、XHR/fetch、子资源、弹窗、重定向及其他出站通道都应受策略约束；地址固定与内网阻断优先由可强制执行的出站代理或进程级网络隔离实现，不能只依靠页面 route 规则。未能建立相同边界时，不向任意用户输入 URL 开放浏览器模式。

不将失败请求自动无条件升级为浏览器，也不承诺挑战页一定可处理。继续遵守 README 已声明的公开访问范围。

### 阶段 3：确需大规模任务时再换 Spider（估计 5–10 人日）

触发条件是现有一层/5 页/8 文件模式不能满足明确需求，并且产品决定扩展任务规模。此时引入 Spider 只负责页面发现与结构化结果，通过事件队列交给受控下载器，下载完成后由单一书库写入方执行入库，避免让并发抓取回调跨线程共享 SQLite 连接。

长任务要同步增加任务 API（创建、状态、进度、取消），明确取消与恢复语义。现有 `/api/crawl` 保留兼容入口或明确版本化；不能只把 `spider.start()` 塞进请求处理函数就宣称完成迁移。七项原因统计保持原有含义，框架 item 数不能直接赋给 `downloaded` 或 `success`。

默认下载链路与配额不随框架改变。要扩容，应作为独立产品变更交付，明确新的同源策略、并发预算和用户确认方式。

所有人日是初步开发估计，各阶段有独立退出条件；没有实际样本和部署测量前，不构成确定交期。

## 五、必须保留的验收条件

| 类别 | 自动化或实测内容 |
| --- | --- |
| 解析 | 相对路径、片段去重、签名 query、畸形 HTML、编码、title/h1、label、PDF/EPUB/ZIP/Office、无扩展名链接 |
| 网络 | 初始内网、混合公网/私网 DNS、多次解析变化、IPv4/IPv6、重定向到内网/跨源、超出跳转数、浏览器出站边界 |
| 配额 | 普通 100 请求/最多 40 下载；递归 20 请求/5 子页/8 下载；HEAD 和跳转计入，重试及并发不超发 |
| 文件 | HTML 10 MiB、单文件 200 MiB；当前成功下载计费逻辑不退化；进一步测试无 Content-Length、中途超限、部分失败、清理和磁盘不足 |
| 总预算语义 | 当前代码只把成功写完的下载字节加入 total_bytes；失败临时文件和 HTML 开销没有完整计入。“512 MiB”不应被解读为严格总网络流量上限。扩展并发前定义累计网络量、临时盘占用和成功入库量三种预算，并分别测试 |
| 入库 | 安全文件名、危险扩展名追加 `.download`、内容哈希去重、部分失败、无下载时保存源 HTML、取消后临时文件回收 |
| 结果契约 | `folderId/folderName/downloaded/messages/stats/recursiveEnabled` 不变，七项统计不混用；HTML 降级时 downloaded 可为 1 而 success 为 0，必须保留并区分 |
| 恢复与并发 | 书库单写入方、重复事件幂等、任务恢复不重复入库、私有检查点、取消不继续新增下载 |
| 部署 | Python 支持版本、目标 OS/CPU 的原生依赖安装、离线/无浏览器回退、浏览器升级、冷启动/内存/安装体积 |

现有 `tests/test_crawler_security.py` 有 24 个相关测试，是行为基线；还需覆盖 `tests/test_response_contract.py`、`tests/test_operation_log_bridge.py`、`electron/tests/recursive-crawl.test.mjs` 和用户提示测试。现有测试不等于上述迁移条件已经全部覆盖。

每阶段新增或更新行为测试，运行全部 Python、Electron 测试、Python 编译检查和前端构建，通过后分别提交；新增用户可见能力同步 README。回滚首先关闭后端选择开关，确保无需回退书库格式。

## 六、本次文档交付验证

- Python 3.12 全量自动化测试：238 项通过；Electron 自动化测试：182 项通过。
- Python compileall、前端生产构建、文档本地链接检查和 Git 空白检查通过。
- 首次测试受本机端口绑定限制阻断，调整运行权限后执行。之后导出日志测试出现过一次文件尚不存在的时序失败，完整重跑通过；本次未修改该测试或运行代码。
- 前端构建保留体积较大及可选 PDF CJK 离线字体未安装的提示；不影响构建退出状态。
- 上述验证针对当前 BookManager 与文档，不代表 Scrapling 集成已验证。

## 七、上游证据

以下源码链接固定于评估提交；发布版接入前另做版本核对。

- [S1：BSD-3-Clause LICENSE](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/LICENSE)
- [S2：Python 版本、基础依赖与 extras](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/pyproject.toml)
- [S3：v0.5 发布说明及破坏性变更提醒](https://github.com/D4Vinci/Scrapling/releases/tag/v0.5)
- [S4：Spider 默认设置与公开入口](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/spiders/spider.py)
- [S5：调度执行、域名过滤与重试](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/spiders/engine.py)
- [S6：SessionManager 与底层客户端耦合](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/spiders/session.py)
- [S7：LinkExtractor 默认扩展名、协议与 canonicalization](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/spiders/links.py)
- [S8：HTTP 请求、重试及 safe 重定向参数](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/engines/static.py)
- [S9：Response 转换与完整 HTTP body](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/engines/toolbelt/convertor.py)
- [S10：pickle 检查点](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/spiders/checkpoint.py)
- [S11：Selector 构造、自适应与 huge_tree 默认值](https://github.com/D4Vinci/Scrapling/blob/95cf9019ab5a5d6f3e9654b7ef1c6a26e29dc5c0/scrapling/parser.py)
