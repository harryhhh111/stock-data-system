#!/usr/bin/env bash
# 每日数据库备份：pg_dump 自定义格式 → 本地暂存（校验）→ 切片上传 COS → 清理。
#
# 为什么切片：cosfs 挂载参数 ensure_diskfree=10240（MB），大文件上传时 cosfs
# 会在本地暂存约等量的数据，需要 空闲-暂存 > 10G；整文件上传（2.3G+）在 12G 空闲时
# 仍会 EIO。切片 700M 后每片上传峰值约 0.7-1.4G，安全通过。恢复时先合并：
#   cat stock_data_*.dump.part-* > /tmp/full.dump && pg_restore -d stock_data --clean --if-exists /tmp/full.dump
# 每个备份带 .manifest 文件：整包 sha256/大小/分片清单，合并后务必核对整包 sha256。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="/lhcos-data/database"
RETENTION_DAYS=14
PART_SIZE="700M"
TS="$(date +%Y%m%d_%H%M%S)"
STAGE="/tmp/stock_backup_${TS}.dump"
DUMP_NAME="stock_data_${TS}.dump"

cd "$PROJECT_ROOT"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${STOCK_DB_HOST:=127.0.0.1}"
: "${STOCK_DB_PORT:=5432}"
: "${STOCK_DB_USER:=postgres}"
: "${STOCK_DB_PASSWORD:=stock_data_2024}"
: "${STOCK_DB_NAME:=stock_data}"

if [ ! -d "$BACKUP_DIR" ]; then
  echo "ERROR: COS 未挂载（$BACKUP_DIR 不存在），先挂载 cosfs" >&2
  exit 1
fi

# 先清理过期备份腾挪本地空间（分片 + 旧整包命名都覆盖）
find "$BACKUP_DIR" -maxdepth 1 \( -name 'stock_data_20*.dump.part-*' -o -name 'stock_data_20*.dump.manifest' -o -name 'stock_data_20*.dump' -o -name 'stock_data_20*.dump.sha256' \) -mtime +"$RETENTION_DAYS" -delete
find /tmp -maxdepth 1 -name 'stock_backup_20*.dump*' -mtime +3 -delete 2>/dev/null || true

echo "[$(date '+%F %T')] dump start: $STOCK_DB_NAME @ $STOCK_DB_HOST → 本地暂存"
PGPASSWORD="$STOCK_DB_PASSWORD" pg_dump \
  -h "$STOCK_DB_HOST" -p "$STOCK_DB_PORT" -U "$STOCK_DB_USER" \
  -d "$STOCK_DB_NAME" -Fc --compress=6 -f "$STAGE"

PGPASSWORD="$STOCK_DB_PASSWORD" pg_restore --list "$STAGE" > /dev/null \
  && echo "[$(date '+%F %T')] pg_restore --list 校验通过"
FULL_SHA=$(sha256sum "$STAGE" | awk '{print $1}')
FULL_SIZE=$(stat -c %s "$STAGE")
echo "[$(date '+%F %T')] 整包 sha256: $FULL_SHA ($FULL_SIZE bytes)"

# 切片上传；失败保留暂存并告警
if ! split -b "$PART_SIZE" "$STAGE" "$BACKUP_DIR/$DUMP_NAME.part-"; then
  echo "ERROR: COS 分片上传失败，暂存保留在 $STAGE" >&2
  exit 1
fi

# 分片清单写入 manifest
{
  echo "full_sha256: $FULL_SHA"
  echo "full_size_bytes: $FULL_SIZE"
  echo "parts:"
  for p in "$BACKUP_DIR/$DUMP_NAME".part-*; do
    s=$(stat -c %s "$p")
    h=$(sha256sum "$p" | awk '{print $1}')
    echo "  $(basename "$p") $s $h"
  done
  echo "assemble: cat $DUMP_NAME.part-* > $DUMP_NAME"
  echo "restore: pg_restore -h <host> -U $STOCK_DB_USER -d $STOCK_DB_NAME --clean --if-exists $DUMP_NAME"
} > "$BACKUP_DIR/$DUMP_NAME.manifest"

rm -f "$STAGE"
echo "[$(date '+%F %T')] done: $DUMP_NAME（分片 $(ls "$BACKUP_DIR/$DUMP_NAME".part-* | wc -l) 个 + manifest）"
