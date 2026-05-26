# codex-session-path-fixer

`codex-session-path-fixer` 是一个零依赖的 Python CLI 工具，用来修复 Codex 从 Windows 迁移到 macOS/Linux 后，本地聊天记录里失效的旧工作目录路径。

## 工具用途

Codex 的本地聊天记录通常保存在：

```text
~/.codex/sessions
~/.codex/archived_sessions
```

如果你之前在 Windows 上使用 Codex，session 文件里的 `cwd` 可能仍然是 Windows 路径，例如：

```text
D:\projects\demo-app
D:\\projects\\api-service
D:/projects/web-client
```

迁移到 macOS/Linux 后，项目目录可能已经变成：

```text
/Users/you/Projects/demo-app
/Users/you/Projects/api-service
/Users/you/Projects/web-client
```

这个工具会扫描 Codex 的 session 文本文件，把旧路径前缀批量替换成新路径前缀，让历史对话重新指向正确的本地项目目录。

它不依赖固定用户名、盘符或项目目录名。每个人只需要按自己的电脑结构传入 `--old` 和 `--new`：

```text
Windows 旧前缀: D:\projects
macOS 新前缀:  /Users/you/Projects
Linux 新前缀:  /home/you/projects
```

## 特性

- 默认只扫描 `~/.codex/sessions` 和 `~/.codex/archived_sessions`
- 默认 dry-run，不加 `--apply` 不会修改任何文件
- 支持 Windows 单反斜杠、JSON 双反斜杠、正斜杠三种路径写法
- 只处理 UTF-8 文本文件，无法 UTF-8 解码的文件会跳过
- `--apply` 时默认自动备份 session 目录
- 跳过 `auth.json`、`state_*.sqlite`、`logs_*.sqlite`、`goals_*.sqlite` 等敏感或数据库文件
- 不依赖第三方 Python 包

## 安装

克隆仓库：

```bash
git clone https://github.com/niuMocc/codex-session-path-fixer.git
cd codex-session-path-fixer
```

查看帮助：

```bash
python3 codex_path_fixer.py --help
```

Python 3.9+ 推荐使用。

## 使用说明

### 方式一：交互式向导

普通用户推荐直接运行向导：

```bash
python3 codex_path_fixer.py
```

也可以显式指定：

```bash
python3 codex_path_fixer.py --interactive
```

向导会依次询问：

- 旧 Windows 路径前缀
- 新 macOS/Linux 路径前缀
- Codex home 路径，默认 `~/.codex`
- 是否在真正修改前创建备份

向导会先自动执行 dry-run，列出将会修改的文件。只有你确认后，它才会真正写入修改。

### 方式二：命令行参数

也可以直接传入参数。先 dry-run，确认哪些文件会被修改：

```bash
python3 codex_path_fixer.py \
  --old 'D:\projects' \
  --new '/Users/you/Projects'
```

确认输出无误后再真正修改：

```bash
python3 codex_path_fixer.py \
  --old 'D:\projects' \
  --new '/Users/you/Projects' \
  --apply
```

指定自定义 Codex home：

```bash
python3 codex_path_fixer.py \
  --old 'D:\projects' \
  --new '/Users/you/Projects' \
  --codex-home '/path/to/.codex' \
  --apply
```

关闭自动备份：

```bash
python3 codex_path_fixer.py \
  --old 'D:\projects' \
  --new '/Users/you/Projects' \
  --apply \
  --no-backup
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `--old` | 旧路径前缀，例如 `D:\projects` |
| `--new` | 新路径前缀，例如 `/Users/you/Projects` 或 `/home/you/projects` |
| `--codex-home` | Codex home 路径，默认 `~/.codex` |
| `--apply` | 真正写入修改；不加时只 dry-run |
| `--interactive` | 启动交互式向导；不传 `--old` 和 `--new` 时默认进入向导 |
| `--backup` | 修改前备份，默认开启 |
| `--no-backup` | 使用 `--apply` 时关闭备份 |

## 备份位置

使用 `--apply` 且未关闭备份时，工具会先复制 `sessions` 和 `archived_sessions`。

如果桌面目录存在，备份会写入：

```text
~/Desktop/backups/codex-session-path-fixer-YYYYMMDD-HHMMSS
```

如果桌面目录不存在，备份会写入当前目录：

```text
./backups/codex-session-path-fixer-YYYYMMDD-HHMMSS
```

Dry-run 不会创建备份，因为它不会修改文件。

## 输出内容

命令会输出：

- 找到的文件数量
- 扫描的 UTF-8 文本文件数量
- 匹配文件数量
- 实际修改文件数量
- 被跳过的受保护文件数量
- 被跳过的非 UTF-8 文件数量
- 每个被修改文件的路径；dry-run 时显示将会被修改的文件路径

## 注意事项

- 建议先关闭 Codex，再执行 `--apply`，避免 session 文件正在被写入。
- Windows 路径建议用引号包起来，例如：`'D:\projects'`。
- 第一次运行一定先 dry-run。
- 修改后先保留备份，确认历史对话能正常打开后再删除。
- 本工具不会修改 `auth.json` 或 sqlite 数据库文件。

## License

MIT
