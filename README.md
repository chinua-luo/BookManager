# BookManager

BookManager 是一个本地电子书与文档管理工具。它使用 SQLite 保存虚拟文件夹、镜像文件和元数据，使用 SHA-256 内容寻址存储保证同一份内容只保留一个底层文件，并提供 Electron 桌面界面与内嵌文档预览。

## 主要功能

- **单一底层文件**：导入内容按 SHA-256 去重；同一文档可以镜像到多个虚拟文件夹，并在不同位置使用不同名称。
- **层级书库**：支持创建、重命名、移动、删除文件夹，以及标准排序和系列编号排序。
- **导入与拖放**：可导入单个文件或完整目录；目录层级会转换为书库层级，文件统一进入底层 Blob 库。
- **结构化命名**：保存系列、编号、主标题、副标题、版本、作者、备注和标签，并支持从文件名及 PDF、DJVU、EPUB 元数据生成候选信息。
- **文档预览**：右侧内嵌预览 PDF、EPUB、DOCX、XLSX、PPTX、常见图片和纯文本；DJVU 使用 Python/DjVuLibre 渲染回退。
- **搜索与标签**：可分别搜索底层唯一文件、镜像文件、标题和标签；同一底层文件的多个镜像会分组显示。
- **系统集成**：双击或右键可用系统默认程序打开文件；打开缓存中的改动可同步回底层文件。
- **缓存与数据迁移**：可设置缓存保留时间、容量上限和退出清理策略，也可迁移整个数据目录。
- **系列爬取接口**：接受普通网址或 Springer 系列编号，将结果导入以系列全称命名的新文件夹。

## 命名规则

结构化文件名格式为：

```text
系列缩写编号 主标题 - 副标题 - 版本信息 _ 作者.扩展名
```

- 各字段可以单独为空，但组合后的名称不能为空。
- 纯数字系列编号自动补足三位。
- 英文或中文第一版均省略版本信息，其余版本按所选语言生成。
- 备注与标签只保存为元数据，不写入文件名。
- 标题可选择规则化大小写或保持原样。

## 架构

```text
run.py                  启动入口，优先启动 Electron，缺失时回退到 Tk 界面
bookmanager/
  store.py              SQLite、文件夹、镜像和 Blob 存储
  bridge.py             Electron 与 Python 之间的受控本地 API
  naming.py             命名解析与规则化
  metadata.py           文档元数据提取
  rendering.py          PDF/DJVU/EPUB 回退渲染
  crawler.py            系列爬取接口
electron/
  main.mjs              Electron 主进程
  preload.cjs           受控渲染进程接口
  src/                   左侧书库与右侧预览界面
```

Electron 只通过 `document_id` 请求文件，Python 后端负责解析实际 Blob 路径，不向渲染页面暴露真实数据目录。

### 数据模型

- `blobs`：以 SHA-256 为名称保存真实文件，相同内容只保存一次。
- `documents`：底层文档身份及其当前 Blob 引用。
- `items`：文件夹内的镜像文件，保存名称、备注、标签和文档引用。

替换某个文档的底层内容后，所有引用该 `document` 的镜像会同步更新。

## 安装与运行

Python 依赖：

```bash
python -m pip install -r requirements.txt
```

从源码启动 Electron 界面：

```bash
cd electron
pnpm install
pnpm start
```

包含匹配平台的 `electron/runtime` 与已构建前端时，也可以直接运行：

```bash
python run.py
```

Windows 可双击 `start_windows.bat`。macOS/Linux 可使用 `python3 run.py`。PDF 处理需要 PyMuPDF，DJVU 完整预览需要系统安装 DjVuLibre。

## 数据目录

默认数据位于项目根目录的 `BookManagerData/`：

```text
library.db       书库结构、镜像、元数据和 Hash 索引
blobs/           SHA-256 底层文件
open_cache/      使用系统默认程序打开的临时副本
preview_cache/   文档预览缓存
tmp/             导入和渲染过程中的临时文件
```

书库数据、缓存、依赖目录和 Electron 二进制不会提交到 Git。可在软件的 `设置 > 数据位置` 中把全部数据迁移到其他空目录。

## 开发与验证

项目协作规则见 [`AGENTS.md`](AGENTS.md)。每次改动必须补充或更新相关测试，通过全部验证后创建独立 Git commit。

当前基础验证命令：

```bash
python -m compileall -q bookmanager run.py
cd electron
pnpm build
```

## 许可证

本项目代码采用 [GNU Affero General Public License v3.0](LICENSE)（`AGPL-3.0-only`）。使用、修改、分发本项目，或通过网络向用户提供其功能时，须遵守该许可证；第三方依赖继续适用各自的许可证。

## 爬取限制

爬取功能不会绕过登录、机构权限、版权限制或站点访问控制。页面不提供公开下载链接时，程序仅保存可合法访问的内容或源页面，供后续处理。
