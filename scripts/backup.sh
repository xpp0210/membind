#!/bin/bash
# MemBind 数据库自动备份
# 用法: ./scripts/backup.sh 或通过 systemd timer 触发

set -euo pipefail

DB_PATH="${MEMBIND_DB_PATH:-data/membind.db}"
BACKUP_DIR="${MEMBIND_BACKUP_DIR:-data/backups}"
RETAIN_COUNT="${MEMBIND_BACKUP_RETAIN:-7}"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "数据库文件不存在: $DB_PATH"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M)
BACKUP_FILE="$BACKUP_DIR/membind_${TIMESTAMP}.db"

# 使用 SQLite 的 .backup 命令（安全备份，不会损坏源文件）
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

if [ -f "$BACKUP_FILE" ]; then
    echo "备份成功: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
else
    echo "备份失败!"
    exit 1
fi

# 保留最近 N 份
cd "$BACKUP_DIR"
ls -t membind_*.db 2>/dev/null | tail -n +"$((RETAIN_COUNT + 1))" | xargs -r rm --
REMAINING=$(ls membind_*.db 2>/dev/null | wc -l)
echo "当前保留 $REMAINING 份备份"
