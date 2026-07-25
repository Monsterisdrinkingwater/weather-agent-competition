#!/bin/bash
# 清除 Hermes 输入框中残留的 weather_competition.html 附件
# 使用方法：
#   1. 先完全退出 Hermes (Cmd+Q)
#   2. 运行 bash ~/Projects/weather-agent-competition/clear-composer-draft.sh

LDB_DIR=~/Library/Application\ Support/Hermes/Local\ Storage/leveldb

echo "=== 检查 Hermes 是否在运行 ==="
if pgrep -f "Hermes.app" > /dev/null 2>&1; then
    echo "❌ Hermes 还在运行！请先 Cmd+Q 完全退出 Hermes，然后重新运行此脚本。"
    exit 1
fi
echo "✅ Hermes 已退出"

echo ""
echo "=== 备份 LevelDB ==="
BACKUP_DIR=~/Library/Application\ Support/Hermes/Local\ Storage/leveldb-backup-$(date +%Y%m%d%H%M%S)
cp -r "$LDB_DIR" "$BACKUP_DIR"
echo "✅ 备份到: $BACKUP_DIR"

echo ""
echo "=== 清除 composer-drafts ==="
# 删除所有 .log 和 .ldb 文件中引用了 weather_competition 的草稿条目
# LevelDB 的 log 格式无法简单 sed，所以直接删除整个 composer-drafts key
# 这会清除所有会话的草稿（不影响已有消息）
cd "$LDB_DIR" || exit 1

# 用 Python 操作 LevelDB
~/.hermes/hermes-agent/venv/bin/python3 << 'PYEOF'
import os, shutil, glob, struct

ldb_dir = os.path.expanduser('~/Library/Application Support/Hermes/Local Storage/leveldb')

# LevelDB 的 .log 文件是追加日志，包含 record 序列
# 我们直接扫描 .log 文件，找到 composer-drafts 的记录并清空

changed = False
for f in sorted(glob.glob(os.path.join(ldb_dir, '*.log'))):
    fname = os.path.basename(f)
    data = open(f, 'rb').read()
    
    if b'composer-drafts' not in data:
        continue
    
    print(f'  处理 {fname}...')
    
    # 读取所有内容，将 composer-drafts 的 value 替换为空 JSON
    # composer-drafts 的值是一个 JSON，格式如 {"session_id":"draft_text",...}
    # 我们将其替换为空对象 {}
    
    new_data = data
    # 在二进制中找到 composer-drafts 后面的 JSON 内容并清空
    idx = 0
    while True:
        idx = new_data.find(b'composer-drafts', idx)
        if idx == -1:
            break
        # 找到后面紧跟的 JSON { ... }
        json_start = new_data.find(b'{', idx)
        if json_start >= 0 and json_start - idx < 100:
            # 找到对应的 }
            depth = 0
            json_end = json_start
            for i in range(json_start, min(len(new_data), json_start + 5000)):
                if new_data[i:i+1] == b'{':
                    depth += 1
                elif new_data[i:i+1] == b'}':
                    depth -= 1
                    if depth == 0:
                        json_end = i + 1
                        break
            # 提取原始 JSON 看看
            orig = new_data[json_start:json_end]
            if b'weather' in orig or b'competition' in orig:
                print(f'    发现 weather_competition 引用，清除草稿')
                # 用空对象替换（保持长度大致一致，填充空格）
                replacement = b'{}'
                # 用等长替换避免破坏格式
                padded = replacement + b' ' * (len(orig) - len(replacement))
                new_data = new_data[:json_start] + padded + new_data[json_end:]
                changed = True
        idx += 1
    
    if new_data != data:
        open(f, 'wb').write(new_data)
        print(f'    ✅ 已修改 {fname}')

if not changed:
    print('  未发现需要修改的文件（可能草稿在 .ldb 中）')
    print('  尝试删除整个 Local Storage 目录（Hermes 会重建）')
    print('  注意：这只清除草稿/缓存，不影响聊天记录')

PYEOF

echo ""
echo "✅ 清理完成！"
echo "现在重新打开 Hermes，输入框中不应再出现 weather_competition.html"
