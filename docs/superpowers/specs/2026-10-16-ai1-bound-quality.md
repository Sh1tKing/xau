# AI-1 意见与评分同源，保留阶梯护盘

用户授权：2026-10-16「修复，用回那个没有平滑过渡的版本来修复」。

## 基线与范围

从本地 master `1f5f8a4` 开始，交易源码与 `4de77b7` 的阶梯护盘一致。暂停的连续护盘草稿、验证和原部署清单已单独归档，不纳入本次交付。新仓仍为 `profit-floor-v3`，现金档位 2/4/6/10/15 对应 0.20/1/2/4/6；普通 M5 跟随、已有仓冻结规则和 AI-3 的 SL/TP 复核均保留。实际 .env、日志、持仓状态、模型、思考档位、上限和超时不改。

## 问题

16:10 原始 v4 回复把 structure 和 target 评 weak，但这两项没有归属自己的 reservation；12:11 同样遗漏 target。两轮原动作均为 no，原卡应继续拒绝。v4 的两个独立声明（issues 与 dimensions.level）要求模型保持跨字段一致；现有先意见后评分提示词与 Schema 外形检查不能消除这个生成错误。

## 新请求契约 entry-quality-v5

保留唯一全局 issues 清单，每条仍只有一个 dimension、kind、cause、event_group、reason 与 evidence_refs，新增 severity。observation 只能 minor；reservation 可为 minor/material/blocking，普通量与浅回撤只允许 observation，真正的 hard_limit/missing_data 必须 blocking reservation。同 cause/event 仍全卡唯一；同 cause 的独立事件保留，不能改名掩饰同一事件。

五个 dimensions 改为 support/reason/evidence_refs。support 仅 adequate 或 strong，声明正向证据的支持程度，**不再独立生成 weak/missing 或最终分数**。必要证据不存在、触发未成立或保护不可行时，必须在对应唯一意见中声明 blocking，不能用 adequate 掩盖。模型仍自主决定 buy/sell/no。

Python 从一个已声明清单生成等级：本项任一 blocking reservation -> missing/0；否则任一 material reservation -> weak/8；否则有任意轻微意见 -> adequate/16；无意见时按 support adequate/16 或 strong/20。每项只计一次最严重程度，轻微观察不叠加扣分；保留原五项、20/16/8/0 和实际 AI 质量阈值。新增字段只由 v5 处理，不把旧 v4 坏卡换版本、补意见、改等级或抬分。有效 v1-v4 按各自原规则兼容。

v5 暂时投影到既有内部校验结构，保留每条意见的真实 severity，不把同维度所有意见升到最高程度；现有真实引用、候选隔离、方向、重大观望和阻断交易检查继续执行。保存原 v5 声明一次，派生等级单独用于日志；反复校验与重启不改写声明。AI-2/AI-3 不新增评分或请求。

## 执行语义说明

16:10 本地固定 SL=4179.70，Bid=4181.02，remaining_stop_space=1.32；这是 Bid-SL，并不是 Ask 到 M5 低点的距离。提示词明确这几个价格的含义，但 local_plan_feasibility 仍只约束该固定方案，不取消 AI-1 提出另一个合法方案的权限。

## 验证与发布

使用真实两张 v4 坏卡验证原样拒绝；独立编写 v5 回复验证各等级、混合程度、BUY/SELL 镜像、去重、候选/成功 POST 引用、观望/阻断、持久保存及主备 Messages 链路。隔离所有日志与状态，禁止实际 HTTP、MT5、订单和飞书。核对三个策略定义、profit_guard、trader 与回测脚本仍是稳定阶梯基线。完整根目录和部署离线回归通过后提交本地 master，不推送；部署目录以新 MANIFEST 为准，由用户复制源码并重启，保留服务器配置和状态。

本次消除的是 v5 独立 weak/missing 声明与意见清单脱节这一种输出结构；不能保证模型永不漏事实、捏造引用或输出坏 JSON，不声称增加交易或收益。
