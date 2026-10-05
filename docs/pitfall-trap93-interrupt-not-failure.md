# 陷阱93：熔断中断≠失败（2026-10-05 复盘）

> 本文件为陷阱93的权威记录（AGENTS.md 写入需用户批准，暂存于此；后续批准后合并回 AGENTS.md）。

## 现象
大回填接力（12h 熔断 + backfill_relay.sh）下，13:10 熔断触发一次，catchup 复核时把整批 174 份未完成文档全部 attempts+1，attempts 达 2 触发 `--max-attempts 2` 上限 → 174 份全部进 abandoned → 下一轮 pending=0 → 接力器三轮空转后误判"全部完成"退出，回填静默中断 4 分钟（13:10:39 接力器退出 → 13:15:32 我发现并修复重启）。

## 根因
`except subprocess.TimeoutExpired` 后的复核逻辑把"被熔断掐断的批"与"真处理失败的批"同等对待——**中断≠失败**。cron 周期模式下无感（下周期重新排队，attempts 也会因周期长而稀释），接力模式下致命（熔断每触发一次整批 +1，两轮打满上限）。

次生根因（同日连环）：接力器的 dry-run 探测没带 `--max-total 0`——catchup 的 `--max-total` 默认 25，`processed_total=158 ≥ 25` 时静默返回空输出，接力器按"无输出=无事可做"误判完成。

## 修复（2026-10-05 已生效）
1. `reits_rag_catchup.py`：TimeoutExpired 时置 `interrupted=True`，复核后 `fail` 批清空、不 +1 attempts；仅"真跑完仍 ES=False"才计失败。AST 检查过。
2. `backfill_relay.sh`：dry-run 探测统一 `--max-total 0` + 按 `DRY-RUN 待处理 0 份` 显式语义判定，不再按空输出默认。
3. 数据修复：175 份被错杀的 attempts 2→1、abandoned 清空（备份 rag_catchup_state.json.bak_trap93）。

## 铁律
- 中断/熔断/掉电类"未跑完"一律不计失败重试；重试计数只属于"跑了但没成"。
- 任何"静默返回空"的探测接口（dry-run/健康检查）在自动化场景必须显式输出状态，调用方按显式语义判定。
- 接力器/看门狗判活禁用 pgrep 模式字符串（陷阱92 自匹配），用锁文件或 PID。

## 关联
- 陷阱92（同日）：看门狗 pgrep 自匹配导致 catchup 秒退——已改锁文件（catchup_oneshot.sh v2）。
- 接力式大回填部署：catchup_oneshot.sh（单轮）+ backfill_relay.sh（跨窗口接力 + text 标志同步 + 总对账）。
- 周期 catchup cron 已停用（jobs.json 11f1f4830725 enabled=false）。
