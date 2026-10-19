# 存储三层规划与挂载事实

> 记录时间：2026-10-19。本文档是磁盘/备份/表空间迁移的唯一事实来源，改动挂载或备份策略时同步更新。

## 三层规划

| 层 | 介质 | 用途 | 现状 |
|---|---|---|---|
| 热 | 系统盘 `/`（vda1，60G，xfs） | web 服务读取路径：current 投影表、mv_* 视图、daily_quote、watchlist/paper/backtest | 90% 占用，迁移后应降至 ~15% |
| 温 | 挂载云盘（vdb，20G，ext4） | 低频证据链：US 版本层 4 表 + raw_snapshot 两表 | 挂载完成，待建表空间 |
| 冷 | 对象存储 COS（bucket `stock-data-1253228291:/stock-data-backups`，东京区） | pg_dump 备份归档 | 已挂载可用，2026-07 后无新备份 |

## 挂载事实（实测）

### COS（cosfs FUSE 挂载）

- 挂载点：`/lhcos-data`
- 底层：bucket `stock-data-backups`，appid `1253228291`，region `ap-tokyo`，`public_bucket=1`
- 运行进程（root 启动）：
  `cosfs stock-data-1253228291:/stock-data-backups /lhcos-data -ourl=http://cos.ap-tokyo.myqcloud.com -oallow_other -opublic_bucket=1 -oensure_diskfree=10240`
- ⚠️ **不在 /etc/fstab，也无 systemd 服务**（2026-10-19 核实 rc.local 为空）。**重启后挂载会丢失**，需手工重挂：
  ```bash
  cosfs stock-data-1253228291:/stock-data-backups /lhcos-data \
    -ourl=http://cos.ap-tokyo.myqcloud.com -oallow_other -opublic_bucket=1 -oensure_diskfree=10240
  ```
  （凭据文件在 root 的 cosfs 配置里；若重建机器需重新配置 `/etc/passwd-cosfs`。）
- vinci 可写（小文件）。已有内容：`database/`（历史 dump）、`manifests/`、`datasets/`、`e0_20260814/`（E-1 归档）。
- ⚠️ **`ensure_diskfree=10240`（MB）**：本机磁盘余量 <10G 时，大文件上传在 close 阶段报 `Input/output error`（2026-10-19 实测 64MB 即失败）。两种解法：① 磁盘余量回到 10G 以上自动恢复；② root 重挂时调小，如 `-oensure_diskfree=512`。
  备份脚本对此的容错：先落本地 `/tmp` 暂存并校验，上传失败保留暂存文件并以 exit 1 告警。

### 云盘（vdb）

- 挂载点：`/root/data/disk`（ext4，20G），**已在 fstab**（UUID `5f68c94a-…`），重启自动挂载。
- ⚠️ 目录属主 root。PostgreSQL 表空间要求挂载目录本身属于 postgres 且权限 700。

## 表空间迁移（已完成 2026-10-19）

- 表空间：`history_ts` → `/root/data/disk/pg`（postgres 超户密码同日重置为项目默认，见 config.py）。
- 已搬迁 7 表（搬迁前后行数逐一核对一致）：

| 表 | 行数 | 大小 |
|---|---|---|
| us_financial_fact_staging | 233,257（已 TRUNCATE 清空） | 215 MB |
| us_fact_selection_audit | 1,674,737 | 1268 MB |
| raw_snapshot | 1,554 | 1063 MB |
| us_fact_version_relation | 3,536,398 | 1538 MB |
| raw_snapshot_version | 2,955 | 1728 MB |
| us_financial_fact_source | 8,508,149 | 2693 MB |
| us_financial_fact_version | 7,177,804 | 4853 MB |

- 效果：系统盘 94% → 82%（12G 可用）；vdb 用 8.4G/20G。热表（daily_quote、current 投影、mv_*）仍在系统盘。
- 2026-10-19 已对 6 张大表 + daily_quote 做 ANALYZE（此前 pg_stat 统计严重过期）。
- 迁移命令（stock_user 是表 owner）：`ALTER TABLE <t> SET TABLESPACE history_ts;`
- 回滚：`ALTER TABLE <t> SET TABLESPACE pg_default;`

## 备份规范

- 脚本：`scripts/backup_db_to_cos.sh`（pg_dump 自定义格式 `-Fc` → 本地 `/tmp` 暂存 → `pg_restore --list` 校验 + 整包 sha256 → **切片 700M 上传 COS** → 每份附 `.manifest`）。
- 为什么切片：cosfs 上传大文件时会在本地暂存约等量数据，`ensure_diskfree=10240` 要求"空闲−暂存 > 10G"；整文件 2.3G 上传在 12G 空闲时仍 EIO（2026-10-19 实测）。700M 分片峰值约 0.7-1.4G，稳定通过。根治可 root 重挂 `-oensure_diskfree=512`（仍未做，切片方案不依赖它）。
- 命名：`/lhcos-data/database/stock_data_YYYYMMDD_HHMMSS.dump.part-aa/ab/…` + `.manifest`（含整包 sha256/大小/分片清单）。
- 频率：每日 03:17（crontab，flock 防重入）；保留 14 天滚动。
- 恢复：`cat stock_data_*.dump.part-* > /tmp/full.dump`，核对 manifest 中的整包 sha256 后 `pg_restore -h 127.0.0.1 -U stock_user -d stock_data --clean --if-exists /tmp/full.dump`。
- 状态：首个备份已完成（`stock_data_20261019_135651`，4 分片，整包 sha256 `03d84231…`，COS 端 cat 重算一致）。

## 数据生命周期

- **唯一证据永不删**：US 版本层 + raw_snapshot 原始响应，永久保留（已迁 `history_ts` 云盘表空间）。
- **超龄可再获取数据 → COS 归档**：`data/sec_cache`（SEC 抓取缓存，TTL 7 天）由 `scripts/archive_sec_cache.sh` 每日 04:17 归档到 `/lhcos-data/sec_cache_archive/YYYY-MM/`，复制成功（含大小校验）才删本地，失败保留；日志 `logs/sec_cache_archive.log`。2026-10-19 首次归档 512 个（1.78G，其余 534 个在 7 天 TTL 内正常保留），本地 3.6G→1.9G。
- **可再生产物直接清理**：`build/` 构建产物（203M，已清 2026-10-19）；`/tmp` 备份暂存 3 天滚动。
- 系统日志（/var/log 1.7G：cron/secure/messages 未轮转 + journal）需 root 处理 logrotate，未列入自动化。

## 增长与告警

- 版本层约 25 万行/月、300-500MB/月，迁移后写入云盘（20G，约 3 年量级）。
- 磁盘余量监控建议阈值：系统盘 80% 告警。
