# 查找“超过两年且内容相似”的文件（交互式确认删除）

这个小工具会扫描你指定的目录，找出**正文内容相似**的文件组（适合找“不同时间的修改版本”），并把其中**超过两年**的旧版本列出来，让你逐组确认是否删除。

默认是**干跑（dry-run）**：只打印要删的文件，不会真的删除。确认无误后再开 `--action trash` 或 `--action delete`。

## 依赖

- Python 3.10+（建议 3.11/3.12）
- 仅使用标准库即可运行
- `--action trash` 在 macOS 上会尝试用 Finder 移到废纸篓；在其它系统建议安装 `send2trash`

## 运行示例

扫描你的 Documents 和 Desktop（默认扫描你的 Home 目录）：

```bash
python3 find_similar_old_files.py ~/Documents ~/Desktop
```

## GUI（更友好）

如果你更喜欢点选界面，可以运行：

```bash
python3 similar_file_cleaner_gui.py
```

界面里可以：

- 添加/移除要扫描的目录
- 选择文件类型（DOCX/TXT/MD）
- 调整常见参数（相似度阈值等）
- 扫描后勾选要处理的旧版本文件，再选择 `trash`（推荐）或 `delete`

只扫描 Word（docx）并写一份 JSON 报告：

```bash
python3 find_similar_old_files.py ~/Documents --ext docx --report report.json
```

确认后把旧版本移到废纸篓（推荐）：

```bash
python3 find_similar_old_files.py ~/Documents --action trash
```

确认后永久删除（不可恢复，谨慎）：

```bash
python3 find_similar_old_files.py ~/Documents --action delete
```

自动同意（不逐组询问）：

```bash
python3 find_similar_old_files.py ~/Documents --action trash --yes
```

## 常用参数

- `--age-years 2`：超过多少年算“旧文件”（默认 2 年）
- `--min-similarity 0.82`：相似度阈值（越大越严格）
- `--shingle-size 3`：分词后 N-gram 的 N（越小越宽松）
- `--max-hamming 10`：SimHash 预筛选阈值（越大越宽松，速度可能变慢）
- `--max-token-diff-ratio 0.5`：限制候选文件“长度差异”（越大越宽松）
- `--cross-ext`：允许不同扩展名之间比较（默认只比较相同扩展名）
- `--exclude-dir PATTERN`：排除目录名（可重复）
- `--exclude-file PATTERN`：排除文件名（可重复）
- `--cache .autodelete_cache.sqlite3`：指纹缓存（默认开启；设为空字符串可禁用）

## 注意

- 本工具目前支持：`.docx`、`.xlsx/.xlsm`、`.pdf`、`.txt`、`.md`（可用 `--ext` 自定义）
- `.doc`（老 Word 二进制格式）不在默认支持范围内
- PDF 文本提取依赖文件本身是否包含可提取文字；部分扫描版 PDF 可能无法提取到正文，因此会被跳过
- 全盘扫描会很慢并且可能遇到权限问题；建议从你主要存文档的位置开始（Documents、Desktop、Downloads 等）
- 如果在非交互环境运行（例如重定向输出、cron），默认不会弹出确认输入；需要加 `--yes` 才会执行动作
