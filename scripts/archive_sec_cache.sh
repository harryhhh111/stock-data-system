#!/usr/bin/env bash
# SEC 抓取缓存生命周期：超龄文件先归档到 COS，成功后才删本地。
# 原则（docs/deployment/STORAGE.md 数据生命周期）：唯一证据永不删（已在 raw_snapshot_version），
# 超龄可再获取数据归档 COS，可再生产物直接清理。
# cron 每日：17 4 * * *（见 crontab）
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="$PROJECT_ROOT/data/sec_cache"
COS_ROOT="/lhcos-data/sec_cache_archive"
TTL_DAYS="${STOCK_SEC_CACHE_TTL_DAYS:-7}"
MONTH_DIR="$COS_ROOT/$(date +%Y-%m)"

if [ ! -d "/lhcos-data" ]; then
  echo "ERROR: COS 未挂载（/lhcos-data 不存在）" >&2
  exit 1
fi
mkdir -p "$MONTH_DIR"

archived=0; archived_bytes=0; failed=0; failed_files=""
while IFS= read -r f; do
  base="$(basename "$f")"
  src_size=$(stat -c %s "$f")
  if cp "$f" "$MONTH_DIR/$base" 2>>/dev/null; then
    dst_size=$(stat -c %s "$MONTH_DIR/$base" 2>/dev/null || echo -1)
    if [ "$dst_size" = "$src_size" ]; then
      rm -f "$f"
      archived=$((archived + 1)); archived_bytes=$((archived_bytes + src_size))
    else
      rm -f "$MONTH_DIR/$base"
      failed=$((failed + 1)); failed_files="$failed_files $base(size $src_size->$dst_size)"
    fi
  else
    failed=$((failed + 1)); failed_files="$failed_files $base(cp失败)"
  fi
done < <(find "$CACHE_DIR" -maxdepth 1 -name '*.json' -mtime +"$TTL_DAYS" -print)

echo "[$(date '+%F %T')] TTL>${TTL_DAYS}d 归档 $archived 个（$((archived_bytes / 1024 / 1024)) MB）到 $MONTH_DIR，失败 $failed 个$failed_files"
