# 黄金AI-1评分契约v3修复

## 问题及原始证据

16:23:12、decision `1791804000-34` 的原始回复完整，但 `structure.level=weak` 与 `reservation.cause=shallow_retrace` 冲突；理由是“价格位于区间低位且远离EMA21”。浅回撤只能是轻微意见，远离EMA应按执行限制分析。该轮Claude主路120秒读超时，备用GPT完整返回，错误来自备用回复。原Schema将level和cause独立枚举，允许Python随后拒绝的组合。

测试夹具 `tests/fixtures/ai1_quality_20261012_162312.json` 保存精确的成功POST紧凑行情、证据目录和原始回复，只含市场及响应事实，不含账户或密钥。目录与归档逐项一致。旧坏卡仍拒绝，不补写原因、升级等级或复活历史订单。

## 行为变化

`strategies/quality.py` 是版本、五项分值、原因分类及等级关联的共同来源，供AI-1提示词、rubric、响应Schema和Python校验使用。版本升级 `entry-quality-v3`，仍为strong20/adequate16/weak8/missing0，不是胜率。

- strong只填level/reason/evidence_refs，不含意见数组。
- adequate/weak/missing另外填reservations和observations；weak/missing必须有至少一条对应扣分理由。程度仅由维度level决定，不恢复v1重复填写ID、归属及severity的方式。
- 普通量与浅回撤仅进入observations，无额外扣分、不能独立支撑weak/missing或重大反证观望。真实重大结构问题与轻微观察可以同时存在，维度仍保留其weak等级及8分，不自动抬分。
- 强制所有观察绑定实际成功POST证据；去重包含扣分理由及观察，跨候选、旧快照ID和不存在路径继续拒绝。硬限制、缺数据等不能藏在observations中。
- 原因说明明确浅回撤不包括深度下行或远离EMA；M15暂反向不自动等于硬伤。事实与分类语义仍需要模型正确分析，客户端不按关键词改写原因。
- 有效v1/v2卡按原版本严格处理，不转换成v3；v1也不能把普通量/浅回撤声明material/blocking。错误消息显示维度路径、实际cause和level，通知追加评分版本。
- 契约失败仍为contract_invalid、无质量分、不审核或下单，不增加付费重审。可以独立记录已确认的H1方向或当前EMA距离，不能拿风险诊断替代评分。
- 日志、持仓保存及重启恢复保留原始观察和入场评分版本，AI-2/AI-3分工及已开仓策略契约不变。加载代码签名包含新quality模块。

Schema以一个 `$defs` 复用嵌套anyOf分支，避免if/then、数组maxItems等兼容性问题；强等级直接省略意见字段，不用复杂空数组枚举。[Anthropic结构化输出文档](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)列明anyOf及minItems 0/1等子集限制。这里只验证Schema和模拟协议链路，中转忽略或降级Schema时Python仍严格验证，未发送付费请求证明供应商支持。

## 验证

`tests/test_ai_quality_contract_v3.py` 覆盖真实错误、所有等级/原因/数组组合的Schema与Python一致性、重大与轻微混写、BUY/SELL、旧版兼容、证据隔离、独立风险诊断，以及三盘双向实际AI-1→AI-2→模拟执行与持仓落盘/恢复。主备超时后用成功请求目录；坏卡只有一次请求、不进入AI-2。全部HTTP及MT5订单调用为mock。

将Schema临时退回独立枚举并去掉最低扣分理由条数，针对性测试明确失败；恢复新Schema后通过。测试脚本不修改生产文件。可选离线测试依赖在 `docs/deployment/requirements-test.txt`，运行交易不依赖jsonschema。

- 新增19项回归通过，包含72组等级/原因/数组组合，以及三盘双向与主备切换的完整实际代码链路。
- 根目录完整黄金回归466项通过（49.695秒）；部署按MANIFEST列出的28个黄金测试模块独立加载，466项通过（52.328秒）。
- 根目录和部署的两个主脚本自检及策略CLI均通过。三盘启用项与指纹 `0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89` 不变。
- 根目录及部署实际.env的SHA256前后一致；部署102个清单文件的精确字节SHA256均匹配。
- 原始报错轮的v2卡仍拒绝，并明确定位structure/reservations/0、shallow_retrace、weak；同样错误组合在v3 Schema也拒绝。独立诊断得到实时请求报价偏离2.35ATR，硬门保留。
- 加载源码签名 `341dc430f017a660`，评分版本 `entry-quality-v3`；未调用付费模型、连接交易终端或启动交易。工程验证不证明实际供应商永不产生错误、成交频率或收益。

## 部署及限制

直接修改根目录原文件并同步既有 `server_deploy/`，必须更新AI脚本与完整strategies包（新增quality.py）。保留服务器自己的.env、日志及状态，由用户自行重启；启动评分版本应为entry-quality-v3，具体源码签名和文件哈希见MANIFEST.json。

本轮未改变实际.env、门槛、模型、档位、超时、本地策略、H1、SL8、RR1.5、M5保护及EMA1.5ATR。原报错轮实时BUY Ask距EMA约2.35ATR仍超1.5，修复协议不能使该轮变成合法订单。币种与独立本地样本未修改，也未启动交易。

## 17:36同步日志后的兼容修订

服务器17:22:50启动签名为`341dc430f017a660`，已正确加载上面v3构建。此后可见三次AI-1返回，两次正常watch、一次contract_invalid；不能把以前不同版本的错误算作本版新增。

decision `1791808500-4`在17:35:25主路Claude返回HTTP502，随后刷新行情切备用GPT。备用76.625秒、HTTP200/end_turn、输出1183 token、完整JSON，未达到4096上限。usage记录Schema已发送，但回复仍违反旧观察原因枚举，说明“发送Schema”不等于中转实际保证该约束。不是此轮JSON语法、截断或读取超时。

完整卡的counter_evidence是adequate，observations包含opposing_structure，理由为“最新M5收跌，但尚非独立反转确认”，还有一条volume。这符合已有三盘设计“不以单根反向M5剥夺自主提案”，而原v3机械地把轻微观察限制在volume/shallow_retrace两类，拒绝其它主题的轻微观察，分类规则过窄。

本次在同一共享cause_rules中声明observation_allowed：普通量/浅回撤只能轻微；触发、反向结构、保护、目标、执行波动可以有轻微顾虑，也可以有独立重大风险，原因主题不自动决定程度。轻微观察不继承所在维度的weak/missing程度。重大问题仍须reservations，weak/missing必须有独立扣分原因；hard_limit/必要missing_data禁止放观察。模型必须按真实证据判断，Python不按文字关键词改类别、搬数组、改变等级或提高分数；实际风险仍由Python执行门核验。

这是v3允许原因集合的兼容扩展，原有效v3卡仍有效，v1/v2原版处理及分值不变。提示词、rubric、Schema、Python继续共享规则；新构建须核代码签名和MANIFEST，不能仅看相同v3标签。原17:36卡按原始五个等级得到72分，仍是watch，无AI-2、无订单。模型引用的1.55ATR是闭合位置事实，成功POST真实Ask4175.11距EMA4184.335044、ATR4.48约2.06ATR，仍违反1.5ATR；不能据契约修复声称这轮应开仓。

新夹具`tests/fixtures/ai1_quality_20261012_173643.json`保存最后一次成功请求的精确紧凑输入、引用目录和原始回复，没有改写旧回复，也不包含凭证。修复前真实夹具回归失败，修复后通过。新增回归覆盖这份真实no卡及独立追价门、五类轻微主题的双向评分、重大结构与不同事件轻微反向并存、观察不能替代weak/missing原因或重大反证观望、重复事件/跨候选/未知证据拒绝、三盘双向实际代码链路保存恢复，以及原no只请求AI-1且不下单。测试仅模拟HTTP/订单；未做付费兼容探测。

本次新增7项回归，v3专题合计26项通过；根目录完整黄金473项通过（168.556秒），最后提示词明确量能/浅回撤各自原因主题后专题再验证26项通过（26.835秒）。部署副本按28个黄金测试模块独立运行473项通过（161.216秒），两个主脚本自检、策略CLI及三盘指纹核对通过。夹具与最新成功POST及原始回复逐项一致，根目录/部署精确副本一致，103个部署清单文件SHA256匹配，私有.env原始字节哈希保持。

本次加载签名`1311e75bf7a45272`，完整回归、自检及哈希证据记录在MANIFEST的quality_minor_observation_revision中。实际.env、启用策略、本地算法、风险限制及运行进程保持，根目录原文件和现有部署目录同步，用户自行重启。
