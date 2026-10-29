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

if ! mountpoint -q /lhcos-data || [ "$(findmnt -n -o FSTYPE -T /lhcos-data)" != "fuse.cosfs" ]; then
  echo "ERROR: COS 未挂载为 fuse.cosfs，保留全部本地 SEC 缓存，停止归档" >&2
  exit 1
fi
mkdir -p "$MONTH_DIR" || exit 1

archived=0; archived_bytes=0; failed=0; failed_files=""

# 指数成分名单缓存不是"可再生产物"：Wikipedia live 源失效时它不可再生，
# 是 fetcher 30 天 stale 兜底链的一环（2026-10-20 事故：缓存被归档删除后
# 旧进程无兜底，整轮同步阻塞）。排除在归档/清理之外，生命周期由 fetcher 管理。
while IFS= read -r f; do
  base="$(basename "$f")"
  src_size=$(stat -c %s "$f")
  if ! src_sha=$(sha256sum "$f" | awk '{print $1}'); then
    failed=$((failed + 1)); failed_files="$failed_files $base(源文件校验失败)"
    continue
  fi
  if cp "$f" "$MONTH_DIR/$base" 2>>/dev/null; then
    dst_size=$(stat -c %s "$MONTH_DIR/$base" 2>/dev/null || echo -1)
    dst_sha=$(sha256sum "$MONTH_DIR/$base" 2>/dev/null | awk '{print $1}')
    current_src_sha=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
    if [ "$dst_size" = "$src_size" ] && [ "$dst_sha" = "$src_sha" ] && [ "$current_src_sha" = "$src_sha" ]; then
      if ! rm -f "$f"; then
        failed=$((failed + 1)); failed_files="$failed_files $base(本地删除失败)"
        continue
      fi
      archived=$((archived + 1)); archived_bytes=$((archived_bytes + src_size))
    else
      rm -f "$MONTH_DIR/$base"
      failed=$((failed + 1)); failed_files="$failed_files $base(大小/内容校验不一致，保留源文件)"
    fi
  else
    failed=$((failed + 1)); failed_files="$failed_files $base(cp失败)"
  fi
done < <(find "$CACHE_DIR" -maxdepth 1 -name '*.json' -mtime +"$TTL_DAYS" \
    ! -name 'russell1000_tickers.json' -print)

echo "[$(date '+%F %T')] TTL>${TTL_DAYS}d 归档 $archived 个（$((archived_bytes / 1024 / 1024)) MB）到 $MONTH_DIR，失败 $failed 个$failed_files"
if [ "$failed" -gt 0 ]; then exit 1; fi
