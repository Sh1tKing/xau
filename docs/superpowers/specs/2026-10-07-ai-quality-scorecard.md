# AI入场质量评分设计与实施

用户已授权修复反复观望问题。本设计延续H1/M15、本地粗筛、AI-1自主提案、AI-2独立审核、AI-3护盘架构。

## 问题与量尺变化

可见v3快照覆盖18:08-20:20的15次AI-1决策：10次no、5次SELL，交易提案自由分为58/56/54/54/60，均低于75。部分观望是追价或实时RR不足，部分将有效M15假突破与M5零分混淆；后者已在v4补证据。自由confidence没有一致质量尺度，不能解释为胜率。

v5将运行入口的自由confidence替换成AI选等级、Python汇总的质量分，版本为`entry-quality-v1`。75数字保留，**评分含义已经改变，不能声称有效严格程度与旧自由分相同**。这不是用历史收益标定的概率，也没有证明新分数更能预测收益；它首先解决可解释性与扣分一致性。

## 五项判断

| 维度 | adequate（合格）的含义 |
| --- | --- |
| structure | 本时段setup位置及结构符合既有条件；浅回撤不是结构缺失 |
| trigger | 原有M15或M5已收盘规则命中，具有对应时机；不新增双周期同时确认要求 |
| protection | 命名的触发结构有保护，盖住最近3根M5极值及缓冲，距离符合上限 |
| target | 有给定目标依据，重要障碍及报价下RR满足现有1.5要求 |
| counter_evidence | 仅有普通量、浅回撤、旧H1影线、未证实过程极值等轻微意见，或较弱但非重大反向证据 |

每项等级为strong=20、adequate=16、weak=8、missing=0。strong需要额外支持；weak需要对应material证据；missing需要blocking的关键缺失或失效。质量总分为五项之和，不复制本地分，不从旧自由分机械加分。例如四项adequate、一项strong得到84；四项adequate加重大反证weak得到72，会被75挡住。

AI仍选择允许方向、结构价值、重大反证、命名失效与目标，可否决本地单或自主提出新单。仅有轻微意见的已成立候选应形成提案交后续审核，不能无限等待更漂亮形态。代码不把action=no转换成buy/sell。

## 输出与检查

`quality_assessment`含direction、五项dimensions、reservations。每项含level、reason、evidence_refs、reservation_ids；引用使用相对本次输入的JSON Pointer。原因含id、cause、event_group、dimension、severity、reason、evidence_refs；cause由输入枚举约束。相同cause和已知event只归一个维度；未知event按证据引用区分，不能把所有未知项合并。同一原因不能被多个维度重复引用。minor最多把strong降到adequate，不能声称严重不足；声明blocking却提出交易会拒绝。

no另需`wait_basis`，写明hard_limit、missing_structure、missing_trigger、missing_data、target_space或material_counter_evidence及证据。本周期已有触发时不得以missing_trigger等待；若触发位置不符、质量恶化，应说明相应结构或重大反证。既有触发不代表订单必然有效。

`normalize_ai1_quality()`检查引用存在、重复原因、等级与程度、等待依据后计算总分。无评分卡/引用无效时显示明确协议或事实错误，**不回退自由confidence、不额外请求第二轮AI、不自动下单**。原始输出保存在快照，若模型仍返回自由confidence，仅记为model_reported_confidence，不用于新运行入口的交易门槛。直接规则自测的legacy输入路径保留，生产入口必须先通过质量合同。

`local_plan_feasibility`从本地计划和同次快照报价给出方向正确的BUY ask/SELL bid风险与RR。它只评价该本地方案，AI可提出其他有证据的合法方案。旧收盘价与快照报价不同不是事实冲突；最终执行仍用最新报价。20:15的本地方案历史报价RR约1.74，20:20约1.22，后者不能因质量高而放行。

本地策略、M15参考价及已有M5回退、目标1.8R限制、止损8、最少RR1.5、持仓上限、HTTP最多5次重试与接管时间均沿用。没有改.env或启动交易进程。

## 验证与观察

离线回归覆盖BUY/SELL汇总、minor不可选weak、重大反证可低于75、同原因跨项重复、M15触发不能被M5零分抹去、合法硬限制观望、缺失卡/虚构引用不回退、实际entry_task先验证评分卡、止损上限以及实时RR订单拦截。已有本地及AI脚本自测继续执行。

真实接口冻结快照测试用于验证模型能否输出有效合同，数据仅含归档32根M15/8根M5：M15规则按归档覆盖与四舍五入ATR重建，M5分类器输出原样保留。这不是完整历史或成交重放，不可以用其估计收益。离线合成评分卡通过只能证明代码行为，不能证明模型实际输出更好；接口验证状态需依据实际返回报告。

上线由用户自行复制并重启。检查日志同时显示entry-evidence-v5及entry-quality-v1。后续按动作、每项等级、扣分原因、协议错误、AI-2拒绝、最终价格风险分类复盘，避免再把所有没成交统一叫观望。

本轮验证结果：22项离线回归通过，两个原有脚本自测通过，编译检查通过。冻结快照的三次45秒单请求均超时；20:15案例按运行配置60秒再次请求、最多两次也均超时，未得到可验证模型输出。因此不能声称新合同已通过真实接口验证或模型已减少观望；代码无额外修正请求，生产的HTTP最多5次重试和超时配置保持原值。最终方案也向AI-2提供`final_plan_feasibility`，原下单前报价风险检查继续执行。
