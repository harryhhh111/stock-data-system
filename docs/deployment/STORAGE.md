# 存储三层规划与挂载事实

> 初始记录：2026-10-19；备份故障处置更新：2026-10-28；历史索引迁移完成：2026-10-29。历史挂载事实按各段日期理解，最新故障与修复状态见文末。

## 三层规划

| 层 | 介质 | 用途 | 现状 |
|---|---|---|---|
| 热 | 系统盘 `/`（vda1，60G，xfs） | web 服务读取路径：current 投影表、mv_* 视图、daily_quote、watchlist/paper/backtest | 10-29 索引迁移后实测 77% 占用、可用约 13.92 GiB |
| 温 | 挂载云盘（vdb，30G，ext4） | US 历史表、TOAST 及对应的 35 个普通索引 | 10-29 扩容及索引迁移完成；实测可用约 14.71 GiB、使用率 48% |
| 冷 | 对象存储 COS（bucket `stock-data-1253228291`，前缀 `/stock-data-backups`，东京区） | pg_dump 备份归档 | 10-28 用户重新挂载成功；最新 dump 与到期 SEC 缓存已归档并校验，见文末 |

## 挂载事实（实测）

### COS（cosfs FUSE 挂载）

- 挂载点：`/lhcos-data`
- 底层：bucket `stock-data-1253228291`，挂载前缀 `/stock-data-backups`，region `ap-tokyo`。
- 历史运行进程（root 启动；10-28 已卸载，以下匿名参数不可作为当前恢复命令）：
  `cosfs stock-data-1253228291:/stock-data-backups /lhcos-data -ourl=http://cos.ap-tokyo.myqcloud.com -oallow_other -opublic_bucket=1 -oensure_diskfree=10240`
- ⚠️ **不在 /etc/fstab，也无 systemd 服务**（2026-10-19 核实 rc.local 为空）。**重启后挂载会丢失**。下方仅保留历史命令用于追溯，当前恢复须使用凭据认证，见文末：
  ```bash
  cosfs stock-data-1253228291:/stock-data-backups /lhcos-data \
    -ourl=http://cos.ap-tokyo.myqcloud.com -oallow_other -opublic_bucket=1 -oensure_diskfree=10240
  ```
  10-28 核查 `/etc/passwd-cosfs`、`/root/.passwd-cosfs`、`/home/vinci/.passwd-cosfs` 均不存在；此前“root 已有凭据”的记录没有事实支持。`public_bucket=1` 为匿名访问，不能代替认证。
- vinci 可写（小文件）。已有内容：`database/`（历史 dump）、`manifests/`、`datasets/`、`e0_20260814/`（E-1 归档）。
- ⚠️ **`ensure_diskfree=10240`（MB）**：本机磁盘余量 <10G 时，大文件上传在 close 阶段报 `Input/output error`（2026-10-19 实测 64MB 即失败）。两种解法：① 磁盘余量回到 10G 以上自动恢复；② root 重挂时调小，如 `-oensure_diskfree=512`。
  备份脚本对此的容错：先落本地 `/tmp` 暂存并校验，上传失败保留暂存文件并以 exit 1 告警。

### 云盘（vdb）

- 挂载点：`/root/data/disk`（ext4，20G），**已在 fstab**（UUID `5f68c94a-…`），重启自动挂载。
- 2026-10-29 更新：用户以 root 执行 `/usr/sbin/resize2fs /dev/vdb`，在线扩容至 7,864,320 个 4 KiB 块（30 GiB）。原 20G 为扩容前记录。35 个遗留索引已迁入 `history_ts`；收尾实测已用约 13.2 GiB、可用约 14.71 GiB、使用率 48%，挂载点保持不变。
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

### 2026-10-29 索引迁移与物理归属复核

7 张父表、其 TOAST 和下表普通索引均位于 `history_ts`。大小按对象自身的 `pg_relation_size` / `pg_total_relation_size` 分类，不再用父表总大小推断索引位置；以下为迁移后实测值，会随业务写入变化。

| 表 | heap | TOAST heap | TOAST 索引 | 普通索引 |
|---|---:|---:|---:|---:|
| `us_financial_fact_version` | 2636 MB | 0 | 8192 bytes | 6 个，2377 MB |
| `us_financial_fact_source` | 1581 MB | 0 | 0 | 5 个，1170 MB |
| `us_fact_version_relation` | 775 MB | 0 | 8192 bytes | 6 个，763 MB |
| `us_fact_selection_audit` | 707 MB | 0 | 8192 bytes | 5 个，561 MB |
| `us_financial_fact_staging` | 49 MB | 0 | 8192 bytes | 5 个，22 MB |
| `raw_snapshot_version` | 768 kB | 1785 MB | 20 MB | 3 个，840 kB |
| `raw_snapshot` | 200 kB | 1064 MB | 17 MB | 5 个，488 kB |

35 个普通索引合计 5,132,173,312 字节，范围内遗留于 `pg_default` 的索引为 0；全部 `valid/ready/live`，约束关联、代表性历史读取和 Web 健康检查通过。执行记录保存在 `build/us_history_index_tablespace/20261029_020225_e78b2c6a/`。

## 备份规范

- 入口：`scripts/backup_db_to_cos.sh`，读取配置后调用标准库实现 `scripts/backup_db_to_cos.py`。优先重传已有完整 dump，本轮有积压就不生成新 dump；失败返回非零并保留未归档文件。
- 新导出写入 `/tmp/stock_backup_<timestamp>.dump.partial`，完整解压读取通过才改为 `.dump`。未完成的 `.partial` 保留待检查，并阻止再次导出；不按年龄静默删除。
- 校验：`pg_restore --file=/dev/null` 完整读取压缩数据块 + 整包 SHA-256；分片上传后逐片回读、核对分片与整包校验和，最后发布 `.manifest`。只有上传校验完成才删除该份本地 dump。完整读取不等同于隔离库恢复演练。
- 分片：10-28 起默认 64 MiB，可用 `STOCK_BACKUP_PART_MIB` 显式覆盖；此前为 700 MiB。cosfs 会用本地缓存，10 GiB 门槛下即使减小分片，也要求实际剩余空间始终高于门槛和上传缓冲。不得因挂载成功就认为大文件上传必然成功。
- 命名：`/lhcos-data/database/stock_data_YYYYMMDD_HHMMSS.dump.part-aa/ab/…` + `.manifest`（含整包 sha256/大小/分片清单）。
- 频率：每日 03:17（原 crontab 不变）；脚本内另有共享锁，手工运行也受保护。COS 完整备份保留 14 天，仅在本轮有校验成功的备份后清理过期完整组；不自动删除无 manifest 的残片或其他用途快照。
- 空间控制：导出前预留系统盘 2 GiB；已有完整远端清单时按最近一份大小加 64 MiB 估算，没有时用 3 GiB。COS 缓存盘按真实进程参数预留至少 1 GiB 和两片缓冲。共享磁盘时叠加导出预算，并把这个预算落实到导出文件硬上限；不能只保护 2 GiB 而在导出后突破 COS 门槛。单次文件上限默认 8 GiB，超限保留 partial、停止后续导出。可显式调整 `STOCK_BACKUP_ESTIMATED_DUMP_MIB` / `STOCK_BACKUP_MAX_LOCAL_MIB`。
- 重传入口：`scripts/backup_db_to_cos.sh --retry-only`，不会执行 pg_dump。操作应在真实宿主机进行；无法核验 COS 挂载/缓存位置时拒绝继续，不能把普通目录当作 COS。
- 状态：追加输出到原 cron 日志，另写 `logs/backup_db_to_cos_status.json`（最近一次运行结果）。未新增外部消息告警；需由现有运维监控消费失败状态。
- 恢复：只选择**一个时间戳**的 manifest，按其中有序分片清单合并；禁止通配多个日期。先核对整包 SHA-256 和大小，再在隔离数据库演练恢复，避免直接用 `--clean` 覆盖生产库。
- 状态：首个备份已完成（`stock_data_20261019_135651`，4 分片，整包 sha256 `03d84231…`，COS 端 cat 重算一致）。

## 数据生命周期

- **唯一证据永不删**：US 版本层 + raw_snapshot 原始响应，永久保留（已迁 `history_ts` 云盘表空间）。
- **超龄可再获取数据 → COS 归档**：`data/sec_cache`（SEC 抓取缓存，TTL 7 天）由 `scripts/archive_sec_cache.sh` 每日 04:17 归档到 `/lhcos-data/sec_cache_archive/YYYY-MM/`，复制成功且远端大小、SHA-256 与未变化的源文件一致才删本地，失败保留并非零退出；日志 `logs/sec_cache_archive.log`。2026-10-19 首次归档 512 个（1.78G，其余 534 个在 7 天 TTL 内正常保留），本地 3.6G→1.9G。
- **可再生产物直接清理**：`build/` 构建产物（203M，已清 2026-10-19）；备份暂存不再按 3 天滚动删除，改为上传校验成功后删除对应文件。
- 系统日志（/var/log 1.7G：cron/secure/messages 未轮转 + journal）需 root 处理 logrotate，未列入自动化。

## 增长与告警

- 版本层约 25 万行/月、300-500MB/月，迁移后写入云盘（20G，约 3 年量级）。
- 磁盘余量监控建议阈值：系统盘 80% 告警。

## 2026-10-28 备份上传失败处置

- 宿主机核验：US 环境；系统盘约 60 GiB，空闲约 424 MiB；云盘空闲约 10 GiB。原 cosfs 参数仍为 `ensure_diskfree=10240`，缓存默认使用 `/tmp`。
- 日志显示最后一次成功为 10-21；10-22 起分片上传持续 `Input/output error`。10-24～10-28 五份本地完整 dump 共约 11.46 GiB；旧脚本仍按年龄删除过更早失败文件，因此“失败保留”并不代表永久保留。
- 10-28 dump 完整解压读取通过；SHA-256 与导出日志一致：`a7e491c50bb5b8f85bbf554572d05d12e27929c892be626dac615265c1b2fa4a`。未据此声称数据库恢复演练已完成。
- 已修复脚本的积压、磁盘预检、校验和清理顺序；17 项专项离线测试通过，涵盖上传失败、内容损坏、崩溃重试、并发互斥、缺失挂载、空间不足及 partial 保留。
- 运行环境修复需 root：将 cosfs `tmpdir` 放到云盘专用目录 `/root/data/disk/cosfs-tmp`，将 `ensure_diskfree` 改为 1024 MiB，保留原 bucket、挂载点及其余配置。用户已同意协助执行；重挂及真实上传结果待核验后补记。
- 本轮收尾核验时 `/lhcos-data` 尚未重新挂载；真实 `--retry-only` 入口已验证会报错退出，不写普通目录、不创建新 dump。root 修复脚本在 `/tmp/stock-backup-cos-repair.sh`，已使用 `/usr/local/bin/cosfs` 绝对路径并支持未挂载时重试。当前不能标记为“上传恢复”或“磁盘已释放”。
- 本轮不删除未成功归档的五份 dump，不修改数据库，不运行 SEC 缓存归档；其已有失败记录属于同一挂载问题，待挂载恢复后由独立任务处理。

### 10-28 后续授权清理与挂载检查修正

- 用户随后明确要求删除部分旧备份腾出空间，替代上文“保留五份”的临时决定。再次核对 10-28 dump 的 SHA-256 与日志一致后，持有备份锁删除了 10-24、10-25、10-26、10-27 四份本地快照，共 9,841,762,377 字节（约 9.17 GiB）。10-28 最新备份保留，数据库及其他应用数据未动；操作明细在 `logs/backup_cleanup_20261028.json`。
- 清理后系统盘约 9.58 GiB 可用，仍略低于控制台默认 10 GiB 门槛，但超过修复脚本的 1 GiB 门槛。旧四天的本地恢复点已移除，不能再声称这些日期有已验证的远端副本。
- 本机 cosfs 1.0.21 二进制核查：`FdManager::GetFreeDiskSpace` 在未配置 `use_cache` 时使用字面量 `/tmp/`，不随 `tmpdir` 改变。这解释了云盘有空间但前一轮启动仍报空间不足。备份预检已同时检查实际 tmpdir 与 `/tmp`；root 修复脚本也在卸载前检查 `/tmp` 至少有 1 GiB。
- 修正后专项测试 19 项通过；尚需 root 重新挂载成功，才能执行真实上传和回读验收。不得把“已释放本地空间”记作“远端备份已恢复”。

### 10-28 认证阻塞与未挂载保护

- 清理后重挂已通过空间检查，COS 返回 `AccessDenied`（服务端时间 `2026-10-28T14:19:53Z`）。原挂载参数 `public_bucket=1` 明确启用匿名访问；该错误不能据此判定为某个密钥失效，因为这条请求并未使用密钥认证。
- 用户确认暂时没有访问凭据。停止重挂尝试；未修改存储桶权限，未生成或读取密钥，也未要求将密钥发到聊天。
- `/tmp/stock-backup-cos-repair.sh` 已改为先检查凭据文件，再做任何挂载变更。默认读取 `/etc/passwd-cosfs`，或已有 `/root/.passwd-cosfs`；可用 `COS_PASSWD_FILE` 指定其他本地文件。不再使用匿名参数和匿名回退；后续认证连接用 HTTPS，新写入对象的默认 ACL 为 private。
- 数据库备份入口继续在 COS 未挂载时停止导出；另修复 `scripts/archive_sec_cache.sh`：必须是实际 `fuse.cosfs` 挂载才允许归档，不能仅检查目录存在。真实宿主机验证已在复制/删除前返回非零，保留本地缓存。
- 专项测试现为 21 项通过。10-28 最新完整 dump 仍保留；远端恢复点尚未重新核验，真实上传及恢复演练仍未完成。

### 10-28 重新挂载后的真实归档结果（22:55 北京时间）

- 用户再次明确授权把应归档数据传入存储桶。宿主机核验 `/lhcos-data` 为实际读写 `fuse.cosfs`，对应 `stock-data-1253228291:/stock-data-backups`；进程仍为 `public_bucket=1`、`ensure_diskfree=10240`、默认 `/tmp` 缓存。本轮未重挂、未改变存储桶权限。
- 小文件经挂载写入/回读/删除成功，同一前缀直接 HTTPS PUT/GET/DELETE 也成功。此前 AccessDenied 在当前实测中已不再出现；不再将“凭据缺失”视为本轮传输阻塞。
- **SEC 缓存完成**：按原 TTL>7 天规则归档 19 个文件，共 56,181,702 字节，目标为 `/lhcos-data/sec_cache_archive/2026-10/`（桶内 `/stock-data-backups/sec_cache_archive/2026-10/`）。补强为 SHA-256 回读校验后删除本地；0 失败，收尾时剩余到期文件 0 个，指数名单缓存继续保留。
- **数据库备份完成**：`stock_data_20261028_031701.dump`，2,461,135,797 字节，37 个 64 MiB 分片（末片不足 64 MiB）；目标 `/lhcos-data/database/`（桶内 `/stock-data-backups/database/`）。逐片从 COS 直接 GET 回读，整包 SHA-256 为 `a7e491c50bb5b8f85bbf554572d05d12e27929c892be626dac615265c1b2fa4a`，与原完整备份一致。最后发布并回读 `.manifest`，再删除本地暂存。
- 首次经挂载重传因其他磁盘增长、空闲降到门槛以下而安全停止；成功传输使用一次性 HTTPS 流式脚本 `/tmp/stock_cos_direct_archive_20261028.py`，仅处理这一份已验证备份，不生成新 dump，不占用 FUSE 分片缓存。该脚本不替代现有 cron 入口，也不据此宣称后续每日全量任务已恢复。
- 合计移除本地已归档文件约 2.34 GiB。记录：`logs/cos_archive_20261028.log`（缓存成功及挂载重传中止）、`logs/cos_direct_20261028.log`、`logs/cos_direct_20261028.status.json`、`logs/cos_direct_20261028.manifest`；最近备份状态也已记录为本次 HTTPS 归档成功。
- 验证：项目专项测试 25 项通过；一次性 HTTPS 脚本另有 4 项离线测试通过，覆盖上传中断、远端损坏、源校验失败和完成后重试。没有执行生产数据库恢复、迁移、服务重启、commit 或 push。
- 限制：挂载仍保留 10 GiB 门槛，下一轮新全量导出还需同时满足“已有 dump 大小 + 64 MiB 余量 + 挂载预留 + 分片缓冲”；当前磁盘仍有其他写入增长，空间不够时常规任务会停止。当前 HTTPS 无凭据读写已实测可行，意味着该前缀匿名可访问，访问权限尚未收紧。
