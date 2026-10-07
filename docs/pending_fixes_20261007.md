# 待修 Bug 台账（2026-10-07 00:25 立；11:05 全部处置完毕）

> 背景：陷阱95修复夜（内核panic→Docker降容→OCR泄漏三道防线→API通道裁定）过程中发现。
> 触发约定：RAG 管道进程退出后立即逐项修复——已于 10:52 管道完工后执行。

## BUG-1 CC CLI 403 鉴权失效 ✅ 已修复（11:00）
- 根因：cc-switch 代理活跃上游 = GLM(zenmux.ai)，key 无权限（payg 403）；代理路由真源在 `~/.cc-switch/settings.json` 的 `currentProviderClaude`（providers.is_current 与 proxy_live_backup 均非决定项）
- 修复：核证 DeepSeek 官方端点真实可用（/messages 200）后，currentProviderClaude 切至 DeepSeek-V4-Flash（4e39eba2），重启 CC Switch
- 验证：`claude -p` 端到端实测返回「正常」；代理日志 provider=DeepSeek-V4-Flash 状态 200
- 遗留：GLM(zenmux) 与 Claude(zenmux 同上游) 两个供应商 key 均 403，要用需充值/换 key；DB 已备份 .bak-20261007

## BUG-2 memory 看门狗每日假警报 ✅ 已修复（11:05）
- 根因：watchdog 自愈的 `cp -R` 在目标目录已存在时产生嵌套（memory_tencentdb/memory_tencentdb/），plugin.yaml 永远不在检查层级 → 每天 07:00 报「已自愈+需重启网关」（连续 7 天）
- 修复：①手动重建平铺布局（plugin.yaml/__init__.py 就位）②脚本改「先 rm 再 cp」根除嵌套
- 验证：看门狗 exit 0 静默；L0 最后写入 2026-10-07T10:38（本会话活跃，记忆正常写）

## BUG-3 AGENTS.md 陷阱95 写入 ✅ 已修复（00:35 用户批准）

## BUG-4 技能 reits-rag-pipeline 更新 ✅ 已修复（11:02）
- 定位：真实落点在 hermes-shared/skills/reits/（运行时）+ projects/hermes-agent-config（git 源仓），非 active profile
- 修复：v1.11.9 更新日志双副本同步，git 提交 8e6d175

## BUG-5 Docker Desktop AppleScript quit 无效 — 仅记录，不阻塞

## 🆕 BUG-6 管道完工对账：8 份评估报告 step4 失败（待重试）
- 完工会计：119 份过手 = 35 跳过(此前已完成) + 76 成功入 RAG + 8 失败
- 失败原因：step4.1 表格图片描述「部分图片处理失败」（API 通道层，非 step2/OCR——陷阱95 修复未涉及的新事项）
- 已知涉及：508000 张江光大园评估报告、508069 华夏南京交通 2024 年度评估报告、508601 首农招募书(更新)等 8 份
- 下一步：单文档模式重试（`--doc`），若 API 侧连续失败需查 DashScope 限额/配额

## 其他遗留（用户侧）
- 机器方便时重启一次：清掉 14G 旧 swap 残留
