# AGENTS.md — XAUUSD 开发说明

当前主系统默认启用asia_adaptive、london_adaptive、us_adaptive三盘自主AI粗筛；仅模拟账户。本地不要求先命中固定形态，H1方向许可、M15风险指标、最近三根M5极值保护和实时价格风控保留。账户按 100 美元评估，0.01 手，最多 1 个仓。

## 每次开工

判断问题或修改策略之前，必须先调用 agentmemory 的 `memory_recall`。项目标识固定用 `myq-xauusd`。至少查询一次架构和一次已知问题，用查到的结论判断，不要把已经否决的方案重新当成新建议。

建议查询：

- `myq-xauusd architecture H1 M15 fib`
- `myq-xauusd strategy known problems stop cap`

做出新的架构决定、策略取舍或修好一个问题后，调用 `memory_save` 写回，`project` 填 `myq-xauusd`。内容要具体到文件、规则和不要再尝试的做法。

## 改完同步 Git

用户要求保留的代码修改，完成后提交到本地 `master`。不要等用户再提醒。

- 提交当前主脚本、`AGENTS.md`、`.env.example` 和确实属于这次改动的文件。
- 不提交 `.env`、日志、`xau_fib_pa_logs/`、备份副本。
- 提交说明用一句中文，说明行为变化，不写空泛的 “update”。
- 不主动推送远程，除非用户明确要求。
- 不启动实盘或模拟盘进程。正在运行的进程要用户自己重启后才会加载新代码。
- `https://github.com/Sh1tKing/xau.git` 使用干净的 `codex/publish-master` 快照推送到远程 `master`。本地旧历史含无关脚本的硬编码密钥，禁止直接推送本地master历史；公开快照只含README、AGENTS、.gitignore、.env.example、三个黄金脚本、strategies、tests和docs，后续发布以公开分支上次提交为父提交。保留本地master历史，不上传.env、日志、server_deploy、.claude和无关脚本。

## 当前架构

黄金主系统新仓护盘（2026-10-13，用户授权构建）：默认`profit-floor-v1`，纯规则在`strategies/profit_guard.py`，主脚本负责MT5现金模型、身份/报价复核、原子状态与真实执行。新仓冻结参数及原SL/TP到原事件metadata，绑定后保存`guardian_profit_state.json`；旧无标记仓继续legacy，不按当前配置重建旧仓。按运行配置轮询（现0.5秒），BUY用Bid、SELL用Ask；扣已知入场费用、预留平仓费用和负swap，点差不重复扣。持续1秒的新有利报价才晋级，初始预计净锁盈至少0.20账户币种，30%/1R40%/2R50%，没有1R启动门；保留max(冻结上升的1×M5 ATR,3×点差,0.50价格单位,broker止损/冻结+1tick)空间。确认SL只能收紧；ATR下降/回撤不推进，未成功提交不算锁盈。AI3 hold保留底线、tighten也过噪声及当前broker门，策略性退出由AI3处理；新仓不运行旧sigmoid盈利前退出、本地结构退出或旧保本触发，原SLTP及原期限继续。此分工依赖三AI入口，纯本地回放不含AI3，不得称收益更好。实际.env、模型/超时/门槛/三盘定义、crypto及local_guard_demo不改；完整部署含profit_guard.py及两个主脚本，用户自行重启。

独立样本 `local_guard_demo/`（2026-10-12）：用户要求纯本地完整入场+单AI异步护仓，独立文件夹、不接飞书，随后要求秒级黄金。默认strategy.py用H1许可/真实30秒采样区间/持续约1秒突破，0.2秒目标轮询；最新授权由AI负责策略性护仓。新仓冻结quote_ai_guardian_v1，回区间/20秒不足0.15R/0.5R锁盈仅提醒，AI自主hold/tighten/close，本地保留原SLTP/现金风险/SL恢复/2分钟期限。绑定立即派发，默认15秒最小间隔/20秒有效决策预算/动作偏移0.15原始R；成功POST快照验证证据，最新报价及最终Broker修改/平仓前再检查身份/契约/时效/价格变化；单线程、每轮主备最多2POST，坏回复不重审。旧无guardian_policy仓及显式local对照仍沿用原短时本地退出/锁盈，不能重建旧仓。LG_独立配置、magic、0.01单仓、SL8/RR1.5/M5保护/EMA追价/真实现金风险门、token落盘和默认observe保留；EMA入口entry_ema.py保留。replay仅local_comparison、冻结策略/次报价成交/延迟成本压力，不能作为AI护仓收益；真实采样不是完整tick。实际.env、runtime、黄金/crypto/server_deploy本次不改；样本不在公开黄金快照白名单内，用户自行复制完整源码并重启，见local_guard_demo/README.md及VALIDATION.md。

主文件：

- `xauusd_fib_volume_pa_trader.py`：本地策略、下单、新仓分级锁盈、旧仓虚拟护盘和单票平仓。
- `xauusd_fib_volume_pa_ai3.py`：AI-1、AI-2、AI-3。它调用本地策略，不复制一套规则。
- `strategies/definitions/*.py`：策略定义唯一入口，集中评分、参数、证据及AI三角色规则；`strategies/framework.py`自动发现并按文件内容指纹重载。2026-10-10默认启用三盘adaptive定义，旧固定定义及session_retest默认关闭，算法保留。评分异常/语法错误/重复id/无启用策略阻止新入场，持仓护盘继续。旧AI提案绑定原指纹，定义改变即停止审核/执行；已开仓的策略契约和证据保留给AI-3。回测开始冻结定义，不运行中换策略。新策略硬条件写Assessment.blockers，Python也执行，不只靠提示词；粗筛软意见不能误写成硬条件。目标倍数可在1.5–6内调整，旧三策略1.8、新三盘3.0，公共SL8/RR1.5/H1/追价/M5极值保护限制不能覆盖。模板和说明见docs/strategies，CLI `python -m strategies.check`不连接MT5。部署必须复制完整strategies包，框架首次部署需用户重启，此后定义文件在下一次评估/刷新自动检查；辅助模块/.env仍需重启。不要宣传自动从自然语言推导策略或已验证收益。
- 日志目录：`xau_fib_pa_logs/`。密钥只放 `.env`。
- `xauusd_fib_pa_backtest.py`：用 MT5 历史 K 线回放本地策略，不调用 AI，不下单。每根已收盘 M5 评估一次，M1 上模拟成交和本地护盘，同一根 M1 同时碰到止损和止盈按止损算。结果写到 `xau_fib_pa_logs/backtest/`。改了本地策略先跑它再上模拟盘。MT5 的 `copy_rates_from_pos` 一次要几十万根会返回 Invalid params，回测按约 2 万根分页，页被拒绝就再缩小。终端图表最大柱数不够时只拿到已有的K线并提示，不把这当成策略拒绝。
- 回测参数：`--balance` 起始资金（默认 100），`--max-sl` 止损上限（默认读 `.env` 的 `FIB_PA_MAX_SL_ABS`）。`--max-sl` 只改这次回测，不改实盘。报告会写止损上限、单笔最多亏起始资金的比例、资金最低点和是否归零。

当前默认三盘模式（2026-10-10用户明确要求放松模拟策略后启用）：

- 三盘定义和各自entry/review/guardian提示词在`strategies/definitions/{asia,london,us}_adaptive.py`；共同事实与真实分析检查点在`strategies/adaptive.py`。亚盘07–15、欧盘15–20、美盘20–次日07为北京时间策略分段，未移动夏令时。亚盘可趋势交易，不假定永远区间；分盘菜单只是AI参考，不是固定路线。
- `analysis_mode=True`时本地只是软分与真实行情上下文，本地no/低分、无Fib/首次回踩/开盘窗口、单根M5反向都不剥夺AI-1自主分析。数据/指纹/时间错、H1反向和消费/冷却仍拒绝。关闭AI时粗筛不会直接交易。
- `analysis_checkpoint`绑定最新已收盘M5/M15和UTC偏移，明确不是已确认交易触发；每检查点最多一次订单尝试，预提交消费和恢复token保留。新M15、空闲的新M5和启动可分析，普通派发同检查点去重；不增加付费重审。
- AI方案参考成功请求买Ask/卖Bid，旧模式参考价格不变。新模式参考目标上限3R、最长持仓90分钟、平仓冷却60秒，AI-2核固定SL/TP与当前报价，AI-3用原持仓契约。H1、SL8、RR1.5、三根M5极值+缓冲、EMA1.5ATR、实时触发侧2倍stop_buffer均保留。
- 新默认模式依据用户授权用于模拟验证，不要求历史盈利作为启用门；旧区域/三固定策略的亏损回放结论保留，不能改写成盈利证明。完整离线AI链路测试证明代码通路，实际频率/成本/收益须看新的服务器日志。设计见`docs/superpowers/specs/2026-10-10-adaptive-session-demo.md`。

历史候选session_retest（当前默认关闭）：

- 伦敦/纽约当地08:00–08:30连续六根M5建箱，宽1–6个M5 ATR；08:30–11:00首个收盘突破0.05ATR，后六根/30分钟内首次回踩0.25ATR并收盘守在外0.05ATR。箱内0.30ATR失效，只取最新确认，按IANA时区处理DST。
- 确认M5收盘参考价、最近三根M5极值止损，仍使用生产M15ATR缓冲/下限，参考目标3R上限。保留H1同向、SL8、RR1.5、实时触发侧2倍stop_buffer、EMA21追价1.5ATR、0.01手和最多一仓。不能把此局部参考价选择全局套到旧三策略。
- `strategies/lifecycle.py` 在MT5 order_send前原子保留事件，每当地日期窗口最多一次订单尝试（失败仍消耗），平仓后冷却600秒、最长持仓180分钟。期限及策略契约冻结，重启恢复，账户非模拟/未知或状态写入失败不入场。时段外或无合法事件不付费请求AI。默认状态 `xau_fib_pa_logs/strategy_event_state.json`，迁移保留；只运行一个实例。
- 实时取80根闭合M5以覆盖完整窗口及ATR预热；新候选不受旧Fib38–62%/旧箱体评分规则约束，AI三角色由同一策略文件声明驱动。研究实验不含生产H1/缓冲/护盘，接入回测必须重新做，不能复用实验收益。
- 设计见docs/superpowers/specs/2026-10-09-session-retest-integration.md。实际.env门槛、模型、档位、超时和Messages保持用户配置，代理不启动交易。

历史三策略周期分工（默认关闭，公共交易框架继续适用）：

- H1 决定方向。M15 负责斐波那契、tick volume 和价格行为。M5 不单独决定方向，只负责进场时机：每根已收盘 M5 再检查一次，只有最近 3 根 M5 的拒绝极值加缓冲后仍不超过 8 美元，才用该极值做止损。止损必须盖住这个极值，不能用更近的斐波那契或 M15 高点把止损放进影线里面。
- H4 只作为背景，不是否决条件。
- 新的已收盘 M15 先评估开仓，AI-1 和 AI-2 会收到 `session` 和 `setup`，之后每根已收盘 M5 再检查一次时机。本地先评分，AI-1 再给方向，AI-2 只审核硬伤，AI-3 只持有、收紧或平仓。
- Python 是最后一道门。AI 不能放宽止损，不能突破下面的硬限制。
- `call_chat()` 所有HTTP请求合计最多5次，配置 `FIB_PA_AI_HTTP_RETRIES`，主备/low恢复/参数兼容共用预算。2026-10-08成本优化后，同通道读超时额外补发默认0（`FIB_PA_AI_READ_TIMEOUT_RETRIES`），因为上游可能仍在计费生成；独立备用可接手，两边都读超时就结束本轮。连接失败/连接超时仍可在总预算内重试。HTTP错误、非JSON或空输出可切备用；无备用时保留一次low恢复。Chat Completions用reasoning_effort，400拒绝可去掉并记住兼容性，token上限字段不兼容时只换字段不取消上限。正常no/REJECT及截断回复不换模型重审，截断明确失败。任务过期/停止不再请求，不把超时说成“没有可见JSON”。开仓接管360秒、AI-3接管220秒保留。

旧三策略按北京时间分时段，每段都能开仓，但用不同的进场方式（`trading_session()` 和 `SESSION_SETUPS`）：

- 亚盘 07-15、美盘尾段 01-07：`pullback` 趋势回撤，`range_edge` 区间边缘。
- 欧盘 15-20、欧美重叠 20-01：`breakout` 突破回踩，`pullback` 趋势回撤。
- 每个方向在本时段允许的进场方式里取最高分。三种方式都要求 H1 同向，止损、盈亏比、追价、M5 影线规则完全一样。
- `breakout`：突破前 16 根 M15 箱体不超过 3.5 ATR，收盘突破，突破之后的 K 线回踩到箱体边缘 0.35 ATR 内并守住。没有回踩就封顶 45 分，不追突破。
- `range_edge`：24 根 M15 区间在 2 到 7 ATR 之间，价格在边缘 30% 内，要有拒绝 K 线，否则封顶 50 分。
- RSI 和 MACD 只加减分，不单独出信号：RSI 到极端区扣分，突破时 MACD 没跟上扣分。
- 时段边界没按夏令时调整。

市场状态只约束 `pullback`：

- `trend`：按 H1 方向做回撤。M15 暂时反向只算回撤，不直接否决。
- `wide_range`：只做区间边缘，并且要有拒绝 K 线。中轴不追。
- `tight_range`：回撤直接观望。突破回踩和区间边缘有各自的条件，不受这条限制。

入场斐波那契使用 `entry_fib_structure()`。H1 方向和已确认波段不一致，或旧波段已经被价格突破时，改用当前这一段上涨或下跌。`detect_swing_structure()` 仍留给持仓后的结构失效判断，不要拿它代替入场结构。

## 不能改掉的策略限制

- 当前本地阈值以用户本地 `.env` 为准，不用 `.env.example` 判断运行配置。它控制纯本地信号及旧固定定义的额外M5派发；新三盘analysis_mode的启动/M15/M5分析均不要求本地分过线，AI质量门槛独立。
- AI 置信度以运行机器实际 `.env` 为准，独立于本地分；代码默认75。2026-10-07曾结束65流程验证并恢复75，此后用户自行再次调门槛，不得将那次历史决定当成现在改写配置的授权。本次三盘更新没有改实际门槛、模型、档位或超时。最低盈亏比1.5。
- 止损绝对值上限 8 美元（`FIB_PA_MAX_SL_ABS=8`，2026-10-06 用户决定，100 美元本金下单笔最多亏 8%）。结构止损放不下时拒绝，禁止把远处止损硬拉进 8 美元里面。最近 3 根 M5 极值加缓冲超过 8 美元时拒绝，也不能把止损放进这 3 根极值的影线里面。最低盈亏比仍是 1.5，不因为止损变宽而降低。
- 偏离 M15 EMA21 超过 1.5 ATR 不追。
- 旧pullback策略价格贴着极值、没有回到 38.2% 到 61.8% 回撤区，斐波那契结构分为0；session_retest按自身建箱回踩。新analysis_mode的Fib位置只是分析参考，不是请求或自主提案硬门。
- 不因为某一天多单或空单亏损而给买卖设置不同阈值。
- 不把仓位改成账户的 1.5%，不把方向过滤抬回 D1/H4。

## 已知问题

- 2026-10-13 15:12/15:16 请求与意见重复后续修复：服务器已加载3a0be9be65927595，AI2 Grok4.7配置low但disabled使effort实际省略，103.36秒end_turn报告9847/4096、正文198字符且无思考token分项。官方Grok4.5/4.6/4.7不能关闭思考且默认high；Responses上限只约束正文，不能未经供应商证明就称中转漏传上限或确认9847全为思考。AI脚本新增ai-reasoning-capability-v1，仅对明确三个ID在HTTP/刷新/消费检查点前拒绝disabled/between_tools并转已有独立备用；不能自动删除开关，档位被拒或兼容缓存会省略时也不回落默认high，格式单独降级保留档位与5POST预算。超预算仍拒绝/暂停，通知区分客户端总生成预算与上游参数语义。AI1主路Claude完整BUY中同一M15阴线cause=opposing_structure、null组、/m15_bars/31同时放structure/reservations/0与counter_evidence/observations/0，是真重复，原卡仍拒绝；quality.py补意见唯一归属例子，Schema/提示词同源，报错指出两处。不得自动删条目/改等级/抬分/付费修卡，v3分值及所有风险门不变。原POST目录和正文见tests/fixtures/ai_followup_20261013_151602.json；新增15离线回归及验证见docs/experiments/2026-10-13-ai-contract-followup.md。实际.env和策略/护盘保持，继续Grok需用户把该路线THINKING留空并显式EFFORT=low，备用开关独立保留；不能承诺思考费用已由4096封顶。完整部署同步后用户自行重启，不启动交易、不推送远程。

- 2026-10-13 同步黄金输出检查 `ai-output-reliability-v2`：日志到14:06:29，全日90个AI1结果有5次坏契约；10:13备用GPT adequate遗漏意见数组，10:17/10:22/10:37/10:42备用GPT缺结束括号，均是完整HTTP/end_turn/Schema已发送但未兑现，不能补括号、补数组或改no为交易。12:51加载dae6854a989295fc后15个完整结果均为有效watch（后段EMA1.51–1.66ATR/目标空间拒绝），另14:06 Claude达到4096，usage.output_tokens_details.thinking_tokens=3623，原归一化漏读。AI脚本现读取原数字思考计量及来源、非思考差额仅在不冲突时计算；缺报仍未知，不按正文长度猜token，不保存私有思考。增加仅诊断的本次Schema子集检查：schema_enforcement=violated/no_violation_observed/unverified/not_requested，形状合法不等于证据/策略/风控通过，也不证明上游确实约束；Python原旧v1/v2/v3处理保持。quality.py Schema分支明示adequate/weak/missing必须写两数组，模板先放顶层wait_basis并明示闭合，不改变等级/原因/评分。AI1新增FIB_PA_AI1_THINKING及BACKUP_THINKING，空白保留原供应商行为、备用空白继承、disabled显式关闭并省略effort；400不偷偷删除开关，AI1另支持显式between_tools，仅Messages、effort为low/medium/high或默认，不传tools；原生Sonnet5.5拒绝disabled，不能按中转别名猜能力或自动替换，备用空白仍继承所以必须按接口支持独立配置。AI2/3仍只接受原开关且互不影响。实际.env不改，medium/low是软档位，不是独立思考token硬限。10:07 AI2 Grok已实发4096及disabled仍报告6672、正文169字符及1思考块，原费用隔离确实在10:46跳过HTTP；不能再次误判客户端没发上限、按块数猜计费、提高上限或付费重审。15项新增原失败回归、根目录及部署各563项通过；最终全量结果见部署MANIFEST及验证报告，部署必须更新AI脚本和完整strategies包并由用户重启；详见docs/experiments/2026-10-13-output-reliability-review.md。工程检查不能保证中转兑现Schema/成本或思考开关，也未验证真实模型回复质量。

- 2026-10-13 秒级锁盈跳过修复 `guardian-quote-clock-v2`：同步11:11新仓603681669的实际POST报价扣固定UTC+3后领先本机2.310/2.634秒，旧`fresh_guardian_quote`只允许领先1秒；11:11:31至12:01:17共5023次跳过，没有该票程序SL成功回执，AI3文字“4184.12已锁定”不能证明broker已修改。`xauusd_fib_volume_pa_trader.py`统一轮询/最终改SL/最终平仓的`guardian_quote_status/guardian_quote_ready`：允许最多5秒有限时钟领先，报价最老仍5秒，已保存UTC偏移不重算，整小时旧tick、超限未来、交叉、时间字段矛盾/无效、身份改变仍拒绝；不移动入场收盘钟。无效报价清除未完成的1秒确认，原确认SL/水位/风险/规则不改。相同ticket/操作/原因日志最多30秒一次，检查仍每轮执行，恢复立即记录；日志给出reason/变化身份字段/原秒毫秒/offset/age及实际SL/确认线/意图，不能再把所有原因归“身份或报价过期”。AI3快照及保护证据日志补broker_sl与quote_status，提示词禁止把目标说成实际锁盈，仍不能保证模型文字绝无错误。真实报价脱敏夹具`tests/fixtures/profit_guard_clock_20261013.json`；新增10项回归，原代码5项6处失败，修复后通过；完整验证见`docs/experiments/2026-10-13-profit-guard-clock-review.md`。实际.env、模型/门槛/策略定义/冻结利润参数和三个状态文件保持，旧legacy仓不迁移；根目录/现有server_deploy同步代码，用户保留服务器最新状态并自行重启，原profit-floor持仓可按原规则继续，不能删除状态重建水位。无交易或付费HTTP启动。

- 2026-10-13 黄金先锁利润护盘`profit-floor-v1`：实现用户已批准的“本地快保护+AI3策略判断”。只对新提交且冻结标记的仓生效，保存原风险/规则、最优确认价、ATR上限、desired意图与broker确认底线；缺失/放宽SL恢复已确认线，价格越线按最新身份/报价退出。每次修改读取最新SL/TP及broker距离，不回退或夹价；MT5 RLock覆盖本地护盘和AI3执行。原事件token可恢复成交后绑定前崩溃的新仓规则/期限；错身份/不完整回执不猜仓，坏锁盈状态不覆盖、不恢复成legacy；每次报价使用已保存服务器UTC偏移，整小时旧tick不能通过重新推断偏移变成新报价。未盈利不运行旧追损虚拟退出；费用/模型未知时不制造确定净利润承诺。初始参数是模拟假设，不是最优值。538项离线回归、两自检、策略CLI及回放帮助验证；缓存最新100个旧本地固定入场逐票M1对照（共同90分钟期限/0.10费用，无AI1/2/3），17改善/53变差/30相同，新纯本地退出整体更差，不能作为含AI3收益或单仓账户业绩，也不据此调参。设计与验证见`docs/superpowers/specs/2026-10-13-profit-floor-guardian.md`及`docs/experiments/2026-10-13-profit-floor-validation.md`；安全回归入口`verify_gold_profit_guard.py`隔离真实状态并禁止未mock的MT5/HTTP，缓存控制入口`replay_gold_profit_guard.py`只读缓存/旧逐笔。已有server_deploy原文件同步，无修复包；保留.env及最新三个状态文件，由用户重启，未启动交易/付费AI或推送远程。

- 2026-10-13 黄金止损执行复核：AI2 Grok Messages实际发送max_tokens4096及thinking disabled，上游报告7814且含思考块；客户端已有超限拒绝/配置暂停，参数不是漏填，不增加非Messages字段或抬上限。本轮新增实际路由/7814离线回归，未改实际.env/模型/档位/主备。`xauusd_fib_volume_pa_trader.py:modify_position_sl`原来按调用者旧持仓对象比较SL，离线复现BUY95→100.5后旧对象仍能提交99，SELL同理；现每次按ticket读取唯一当前持仓并验symbol/magic/方向/已知identifier，使用最新服务器SL/TP，在AI主系统共享MT5锁内比较/提交。按实际tick网格量化后重核单向收紧及Bid/Ask触发侧stops/freeze距离、现SL/TP冻结和TP距离；非法/未知/距离不足不提交，不向外改松建议、不放弃已有SL，失败日志保留目标/当前SLTP和broker参数。18项新执行测试及3项旧函数失败→新函数通过验证，完整验证见部署MANIFEST及docs/experiments/2026-10-13-ai2-cap-and-guardian-stop-review.md。实际本地已经0.5秒跟踪，AI3按M1异步；盈利后仅按新高水位锁盈、AI3主管策略性退出是后续建议，尚未替换当前虚拟线/结构退出/触发/缓冲/期限，不能把本次执行修复宣传成该新策略已启用或AI3比本地收益更好。只允许单实例，同进程锁不能控制其它终端；无真实HTTP/MT5操作，不保证报价跳变时永无Invalid stops，用户更新原文件并自行重启。

- 2026-10-12 17:36 AI-1评分v3轻微反向观察修正：服务器已加载341dc430f017a660，主路502后备用GPT完整返回，counter_evidence adequate下将“单根M5收跌、尚非反转确认”放observations，原v3只允许volume/shallow_retrace导致错误。strategies/quality.py现按cause_rules.observation_allowed统一Schema/校验/提示词：原因是主题，不自动决定程度；未构成重大风险的触发/反向结构/保护/目标/报价顾虑可作minor观察，hard_limit和必要missing_data仍只能放reservations，weak/missing仍须独立扣分原因，普通量/浅回撤不能撑重大扣分。重大反证不得隐藏为观察，事实证据/候选隔离/去重/材料观望及Python硬风险门保留，不自动搬数组、改等级或no为交易。原17:36完整卡现按原等级72分、watch，实际买价距EMA2.06ATR仍超1.5，不能说修复后这轮应开单。完整原始POST及回复夹具tests/fixtures/ai1_quality_20261012_173643.json；验证见docs/experiments/2026-10-12-ai1-quality-contract-v3.md的17:36修订。兼容扩展仍标entry-quality-v3，须按新代码签名/MANIFEST确认构建，更新AI脚本及完整strategies后用户自行重启；不改实际.env/策略定义、不开交易、不付费重审。

- 2026-10-12 AI-1评分契约v3：16:23备用GPT完整回复将structure weak理由“区间低位/远离EMA”归shallow_retrace，旧Schema允许、Python拒绝。新增strategies/quality.py统一等级/原因/提示词/rubric/Schema及Python关联校验；entry-quality-v3 strong只含level/reason/evidence_refs，其余等级含reservations与observations，weak/missing必须有扣分理由，普通量/浅回撤只放轻微observations，重大原因可与轻微观察并存，不改维度分、不重复扣分。所有观察继续引用成功POST目录并隔离/去重；有效v1/v2保持原版严格处理，旧坏卡不改写、不付费重审、不将no变交易，v1也不得把轻微原因标material/blocking。错误明确路径/cause/level，契约失败可附独立H1/实时EMA诊断但仍无质量分。评分/观察持仓落盘恢复，加载签名包含quality.py。根目录及既有server_deploy同步，用户更新AI脚本+完整strategies并自行重启；实际.env/门槛/模型/档位/超时/策略定义与风险门不变，不启动交易。原轮实时买价距EMA2.35ATR>1.5仍应拒绝。验证与具体限制见docs/experiments/2026-10-12-ai1-quality-contract-v3.md；不可宣称Schema兼容已获付费供应商验证或永不再错。

- 2026-10-12 黄金集中请求修复 `ai-review-reliability-v1`：`xauusd_fib_volume_pa_ai3.py`增加独立AI-2 THINKING/BACKUP_THINKING，空白保留接口默认、备用空白继承主路，disabled显式关闭思考并省略effort；不能把low等同关闭思考，不因400拒绝而偷偷删除disabled。用户11:24/11:43反馈主备超时/输出6045超过4096；同步原始日志只到10:50，无对应完整usage，不能推断具体超限通道/计费组成。部署AI2主路120秒而备用40秒，现本地/部署仅AI2主备thinking=disabled、备用timeout空白继承，模型/档位/门槛/上限和其它私有配置不变。上游报告超发送上限立即结束本轮，不付费换路重审；按角色/URL/模型/凭证摘要/实际token字段与上限/实际effort/thinking暂停同配置后续付费请求，跳过发生在行情刷新/检查点消费之前且不消耗实际5次POST预算，下一轮可用已有独立健康备用，两路违规则停止HTTP。截断/过期超限也保留费用保护；恢复需供应商修正后调整明确配置或用户重启，不定时自动试探、不提高上限、不放行残缺JSON。usage记录原数值token计量及加载时代码签名，启动/API失败/契约错误携带版本，主备超时汇总各自host/model/实际等待，不把主路+备用说成备用重复两次。`/m15_ema21_distance_atr`已在56a9a43引用修复中允许，当前报告须核加载版本，不能再次扩大证据白名单或猜测补路径。新增20项离线回归见`tests/test_ai_review_reliability.py`，验证报告见`docs/experiments/2026-10-12-ai2-review-reliability.md`；保留严格JSON、实时风险及护盘，未启动交易或付费探测，部署须用户重启。

- 2026-10-12 黄金AI-1引用契约修复 `ai1-citations-v1`：12日日志46次返回中30次契约失败，静态rubric仍示范池模式已删除的赢家别名，共享EMA距离/风险规则被误拒绝，模型另会错写position、volume和候选目标参数层级。`strategies/citations.py`只在实际POST压缩后的输入生成带快照摘要、候选/方向归属的证据目录；`xauusd_fib_volume_pa_ai3.py`提示词要求复制短ID、逐POST同时刷新目录及Schema，Python仅按该次成功请求声明映射回真实Pointer，再执行原隔离/质量/风险验证。Schema用单份$defs枚举约束维度/保留意见/等待依据，避免重复枚举撑大请求；共享规则仅允许明确的公共风险参数，目标倍数仍引用所属候选contract，旧赢家别名不恢复。真实旧Pointer兼容但错误层级、旧快照ID、跨候选/反向/隐藏来源仍拒绝，严格拒绝非法~转义；不得模糊猜测修补旧回复或付费重审。历史离线复核解除11次误拒，剩19次原错误不修成交易，原action无变化；新11项回归及黄金427项通过，覆盖Messages正常/主备超时/格式降级刷新和AI2稀疏索引。目录增加请求字符量（历史46次均值+21.31%，含系统与Schema约+17.06%），不是零成本，不保证模型永不违约或提高成交/收益。定义、实际.env及币种/独立样本不改；部署须同步AI脚本和完整strategies包并由用户重启。诊断与验证见docs/experiments/2026-10-12-ai1-citation-contract-validation.md。

- 2026-10-12 黄金同步`xau-ai3-thinking-v1`：`xauusd_fib_volume_pa_ai3.py:request_thinking_mode/call_chat`新增仅AI-3的`FIB_PA_AI3_THINKING`及`FIB_PA_AI3_BACKUP_THINKING`，只接受空白/disabled；空白保留供应商默认，备用空白继承主路，不能按模型名或low档位猜测关闭。显式disabled发顶层thinking并省略effort，保留原token上限、Schema/严格Python契约、每POST新快照、主备总预算5及截断不重审；400明确拒绝thinking只切已有独立备用或结束，不偷偷移除开关。请求准备及usage区分配置/实发档位、开关、刷新耗时、可见正文字符与思考块数量，不记录私有思考正文，未报告reasoning仍为未知。实际根目录/部署.env保留（现有medium/2048不能描述成已关闭思考），用户填写开关并重启后生效。离线416项、两自检、策略CLI及回放help通过；原策略/风险门/本地护盘未改，未连接交易终端或请求真实AI。server_deploy此前混入旧crypto说明/公共模块，黄金清单重新同步，以新的MANIFEST为准；私人配置、日志状态缓存和crypto_demo保留，独立币种仍用crypto_demo。禁止复制整个crypto_ai3覆盖黄金或借此改变黄金止损、手数、单仓和策略。详见docs/experiments/2026-10-12-gold-ai3-thinking-validation.md。

- 2026-10-11 三盘自主模式最终复审修补：`xauusd_fib_volume_pa_ai3.py:role_instructions`的AI-3公共说明曾写死90分钟，会与可编辑且冻结的原持仓期限冲突；现只引用原入场保存的Python持仓期限，数值来自原策略契约，不能用当前定义或默认值重建旧仓。`tests/test_adaptive_ai_integration.py`真实三盘Registry120分钟→完整入场链路→定义重载180分钟→完整AI-3快照，验证原120规则/上下文及当前和恢复ledger期限均不变，默认90测试保留。最终运行代码`7f3df37`，独立403项/两自检/策略CLI/回放帮助及缓存粗筛回放通过，整体规格/质量复审通过；此前01c6559/402项记录是修补前历史证据。新默认启用、实际.env、所有风险门和原451个配置/日志/状态/缓存不变，部署版本及逐文件SHA以`server_deploy/MANIFEST.json`为准。禁止在公共角色提示词再写死可编辑策略参数；工程通过不证明实际成交频率、费用或收益。

- 2026-10-10 `adaptive-session-v1`三盘自主模拟模式完成：默认asia/london/us_adaptive，原固定/首次开盘/区域定义默认关闭保留。`strategies/adaptive.py`真实analysis_checkpoint仅分析/去重，不冒充触发；宿主冻结M5/M15/H1来源、方向/偏移/期限，成功AI1绑定live Ask/Bid和所选分盘规则。local no/低分/无固定形态/反向单M5均不挡合法AI候选，关闭AI不能让粗筛自动交易。启动/新M15/空闲新M5可分析；普通scope在首次请求行情/规则成功刷新后才claim，失败源恢复后可用同检查点，兼容/主备重试仅更新scope不重复claim。AI1/AI2每POST前检查DEMO账户与全部symbol持仓，未知/已有仓不继续付费；AI2先验旧提案指纹再读取提案和查仓，不能破坏原拒绝顺序。AI2固定SLTP+新报价，AI3保存原入场打法/契约；检查点过期不是开仓后理由失效。参考目标3R/90分钟持仓/60秒冷却，每checkpoint一次订单尝试和不确定回执消费沿用ledger/token；H1/SL8/exactRR1.5/三M5极值+缓冲/EMA1.5ATR/触发侧2buffer/.01单仓、质量JSON与成本预算保留。任务复审与最终独立402项/两自检/CLI通过；缓存一天275评估264合法分析候选，纯本地零成交说明AI边界，不是实际AI观望/收益。运行代码01c6559，设计/验证见docs/superpowers/specs/2026-10-10-adaptive-session-demo.md和docs/experiments/2026-10-10-adaptive-session-validation.md。用户明确授权先放松DEMO策略，不能重新以历史盈利作为此轮启用前置门，也不能只改提示词却留首次事件调用门。实际.env/模型/档位/超时/门槛与日志状态缓存保持；部署必须完整替换主脚本+strategies并由用户重启，不宣称保证成交频率/供应商时延/收益。

- 2026-10-10用户再次明确AI-1用于自主选择交易思路，不固定死守某策略；时段/策略是分析参考，不能用固定分盘路由替代自主判断。同步日志最新到10日04:50，9日21:55重启后至此30轮派发均在HTTP前停止，快照全entry_stopped：24窗口外、5较早闭合证据问题、1首次突破反向；10日00:14带closed-entry-clock-v1重启后20轮全窗口外。服务器只加载session_retest，指纹8cb35d与本机bc222d不同，不能仅凭版本标签认为完整新构建已加载。当前宿主仅给无structural_blockers候选/空池不请求，AI只能在本地合法事件内自主精筛，范围比用户期待窄；提示词写自主不解除调用门。后续应设计可验证自主提案契约，把单一入场形态与数据/硬风险门区分，保留H1/SL8/RR1.5/M5保护/追价/真实时效/去重/单仓；不能直接删验证或制造事件。纯本地回测不是AI链路损益。报告docs/experiments/2026-10-10-ai1-not-called-log-review.md；本轮仅分析澄清，未改运行代码/.env/启用/部署或启动交易。

- 2026-10-10原三策略公平对照也未达标：按0533c4e运行前登记，用补齐实际成交SL8后的361f59b回放，同一100136根M1/89个日期/100美元/.01手/本地55/单仓，离线克隆启用pullback/range_edge/breakout，不改运行启用项。基准1958笔/每日中位23/净−75.37，双点差1922笔/−179.91，120秒加双点差1591笔/中位19/−248.11；三组均只有2个零成交日，但已平仓最大回撤293.17/337.47/311.53美元，资金最低−144.08/−198.87/−201.52，全部在7月已跌穿零。回放归零后仍固定手数继续，仅用于诊断，不能把后续成交数或期末余额说成真实100美元账户可持续能力。CSV净盈亏/SL8/成交延迟0、0、120/单仓无重叠均核对；369项工程回归与独立修复审查通过不等于策略成功。区域方案稀疏且拒绝类亏损，旧三策略有频率但成本与资本失败，本轮没有合格中频盈利方案，不能直接恢复旧策略、放宽AI或关护盘冒充解决。组合内range_edge仍正不构成独立回放/OOS证据，不据此按方向/时段/类别挑赢家；本轮没有AI质量/AI2/AI3及模型费用，不能推出AI筛选后的盈利或亏损。旧14天报告不能替代这次同配置长窗口复核。详见docs/experiments/2026-10-10-legacy-control-results.md；本次仅修回放成交门和诊断文档，实际策略/.env/模型/档位/护盘不变，未启动交易或推送远程。

- 2026-10-10旧策略回放成交风控补齐：`xauusd_fib_pa_backtest.py:open_position`此前旧契约绕过demo专用执行函数后仅检查成交RR，参考价SL8可能因点差/价格变化在实际成交超过8；真实`place_order`原本会拒绝。回放现对所有定义再用实际Bid/Ask成交价检查止损距离、方向和非有限值，沿用真实下单0.001止损舍入尺度，不改SL/TP/参考价/评分/护盘/RR。BUY/SELL即时及延迟双点差、恰好8、零距/反侧/非有限回归通过，原旧语义样例11.8风险改为合法7.8仍检查同根SL优先。旧本地回放利润不能未经此成交门重跑就认为完全符合生产SL8，默认session及两区域已有demo执行门不受新增公共门改变。本轮原三策略对照运行前中断无结果，按docs/experiments/2026-10-10-legacy-control-plan.md三场景重跑，不改实际启用项或启动交易。

- 2026-10-10用户否定中频结果后重新归因：本轮策略目标判为失败，不用366项代码通过代替收益/频率。三定义组合内region_rejection35笔−39.97、region_retest35笔+18.92、session10笔+0.30；不能把独立42笔回踩直接相加。区域拒绝及三策略组合淘汰为运行候选，仍默认关闭；回踩独立后段亏且57/89零成交日，也不能发布为合格中频策略。80笔只有1笔原TP，计划2.5/3R不等于实际盈亏比。但固定原入口/SL/TP/期限、不用动态护盘的独立单票诊断中，回踩仅3亏损单获救却14盈利单转亏，拒绝2获救/6转亏，不支持直接关护盘；该诊断持仓可重叠，不能相加当单仓收益。新区域合法事件基础分85、无触发0分，降本地55门槛不是增频解法；冷却最终拒绝仅3次。后续先轻量检验新交易假设与冻结频率/成本/延迟/回撤门，再决定生产接入，不拟合已用缓存、不增加付费请求硬凑成交。脚本docs/experiments/intraday_failure_diagnosis.py只读CSV/JSON、无生产/MT5/HTTP/.env导入，报告docs/experiments/2026-10-10-intraday-failure-postmortem.md；未改运行策略、配置、护盘、部署或启动交易。

- 2026-10-10 日内区域候选池构建：`strategies/events.py`提供通用已收盘事件及来源/偏移/TTL校验，session_retest保留显式历史适配；`regions.py`复算48根M15的左右2根确认拐点、冻结ATR边界、16根M15期限及首次M5触碰周期，`patterns.py`只输出实体/影线/出现顺序事实，不强制吞没。`region_retest.py`与`region_rejection.py`默认关闭，2.5R上限/120分钟/600秒冷却/模拟账户，参数冻结，不能按历史结果反复挑赢家。宿主完成所有启用策略的计划后选可执行最高分；结构合法的低分或本地参考计划失败仍留给AI自主提案。region_event_kind声明算法，不按ID猜；宿主从原始K线复算区域/保护锚/完整反向障碍，止损合并区域失效+近3根M5+原M15缓冲/下限，不伪造Fib或向内裁8美元。共享函数拒绝非有限/非正价格与无效方向。AI-1单请求候选池、必返成功POST的opportunity_id，质量引用绑定实际发送快照，只允许所属方向候选或共享事实；旧本地赢家别名、跨候选/嵌套其它事件/内部host标记引用拒绝，不修卡重审。AI-2保留选中项原稀疏索引，逐POST验原事件与新报价；AI-3保存原候选契约。空池初始及重试正常写本地快照，无POST/API故障通知。执行token写入预留metadata/comment，失败不释放；恢复须核对原event、symbol、magic、方向及唯一token，不猜最近持仓，旧_SR_歧义未知。最终代码复审PASS（HEAD7981642），独立366项回归、两内置自检和策略CLI通过；AI数值reference_price与完整reference_price_metadata分离并覆盖真实BUY/SELL组装到执行门，额外M5仅本地可执行且过实际门槛派发，新M15仍保留自主提案。九组冻结缓存使用100136根M1、89个可评估日期；默认10笔/+0.30与030ffcf逐笔一致，三定义80笔/34个零成交日/净−20.75，region_retest42笔/+25.64但后段10笔−0.77，region_rejection35笔/净−39.97；120秒及双点差联合50笔/净−1.21。频率增加但不支持稳定每日多次或收益结论，不按已用样本挑赢家、主动延时或放松覆盖/风险门；完整server_deploy包与哈希同步；未启用新定义、未改实际.env、未启动交易。最终验收见docs/experiments/2026-10-10-intraday-build-validation.md，不把构建完成称为收益验证。

- 2026-10-10 技术合集9114894代码构思：用户已登录，按最新指示用内置浏览器核对六节页面及作者可见答疑，第一节已显示超过30秒的正文字幕；仍未取得完整字幕列表，不能称完整复现。主题、作者答疑与项目自拟规则必须区分，见docs/experiments/2026-10-10-bilibili-technical-collection-review.md。作者强调实体吞没、位置、首轮/半路区别，第六节否认简单镜像；草案patterns只给几何/位置/顺序/覆盖事实，不把吞没变成硬门或颜色交易指令。实施前代码草案docs/superpowers/specs/2026-10-10-intraday-region-opportunities-draft.md当时尚未实现/启用（构建已由上条完成记录替代，保留以下为历史设计）：通用entry-event、可复核M15区域与触碰周期、逐候选计划、AI1单轮自主选ID、AI2/3绑定所选契约、持仓执行token。已知接口耦合尚未修：validate_strategy_entry和回放research_signal_error要求retest_bar/windows，不能让新策略假装session_retest；当前本地先取最高分再做风险，不自动改审另一个策略。局部方案失败不等于结构没有可执行自主方案。保留H1/SL8/RR1.5/EMA/M5保护/成本预算，不搬1:8/1:19逆势、D1硬过滤或未知插针算法；本轮仅设计，不声称新策略已经能运行。

- 2026-10-10 用户明确希望较快完成单笔、日内中频多次进出，并提供“交易狠人大白”B站账号供参考；暂不依赖补仓。当前仅启用session_retest仍是低频首次开盘事件，3R并不增加频率。研究取舍见docs/experiments/2026-10-10-intraday-medium-frequency-direction.md：只核对近期目录/公开详情，完整视频受登录或充电限制，不能声称已复现作者算法。当时优先研究盘中滚动结构突破回踩及关键区域顺势拒绝，尚未实现/回测/启用；后续构建验收见上条2026-10-10完成记录；不自动恢复旧亏损实验、不搬历史视频价位/固定3美元止损/止损即反手/补仓/1:19目标。保留H1同向、SL8/RR1.5/EMA1.5ATR/M5保护、单仓和AI分工，新事件才能再入场。本轮只改研究说明，实际代码/.env/部署/进程不变。

- 2026-10-09 收盘边界漏检 `closed-entry-clock-v1`：新策略21:55重启后可见69分钟日志有6次线程但无HTTP；5次整点前约1–2秒被unclosed_or_future_m5_evidence挡住。MT5按经纪商换K，本机UTC尚未到边界时严格future检查正确，旧run却提前记为已处理并不重查。两个脚本现共享closed_entry_readiness/entry_clock_ready，未到本机收盘点或tick时差未知时不消费M15/M5标记，后续正常轮询同根重查；护盘/AI3继续，raw latest_bar_time保留给行情新旧判断。M5无动作日志显示双向blockers和原因，等待按bar/原因去重；不加飞书心跳或付费重试。仍严格拒绝未来数据、过期300秒确认、第二次事件和H1逆向，不通过放宽时间/策略去制造成交。日志21:55首个突破与BUY相反、H1=bull是正常阻断，10月9日北京时间窗口15:30–18:00及20:30–23:00（其它日期按DST重新算），23:00后停止新入场。缺少新策略完整原始快照且文件只到23:04，不能说两小时内应成交或证明之后进程状态。回归tests/test_entry_clock_boundary.py，复盘docs/experiments/2026-10-09-session-retest-silence-review.md；实际.env不改，部署替换两脚本并用户自行重启。

- 2026-10-09 报告候选接入strategy-framework-v2：用户要求用研究报告策略，默认仅session_retest、旧三策略关闭。定义集中P参数/评分/完整候选OHLC与ATR证据/三角色规则；确认M5参考仅新候选，旧策略M15参考不全局改动。Windows安装tzdata，80根M5、明确serverUTC偏移、当地DST窗口；模拟账户、首次事件、SL8/RR1.5/触发侧2倍buffer/EMA1.5ATR/M5保护保持。EventLedger预提交原子保留事件，拒单不释放；初次绑定和加载持仓期限/冷却必须与预留metadata一致，重启未绑定回执从SR标记/原时间恢复。180分钟退出独立普通护盘开关，成功平仓立即记录冷却；持仓查询失败不抹掉期限。AI2每次实际POST前再验事件时效/demo/窗口/冷却，K线停滞也拒绝过期，不付费重审。回放缺失确认对应M1丢弃，不延迟成交。call_id与快照时间戳补UUID防Windows精度碰撞。259回归和根目录/部署包各2自检通过；HTTP重试自检隔离实际备用配置，成功或异常后恢复全部替换，避免Messages备用污染虚构Chat回复；缓存100136根M1、本地10笔净+0.30、已平仓DD7.85，后段2笔-3.69、去最好日-10.80；原实验收益不能沿用，未建立稳定优势。未改实际.env模型/档位/门槛/超时/协议，不启动交易，不主动推送。接入设计与验收见docs/superpowers/specs/2026-10-09-session-retest-integration.md和docs/experiments/2026-10-09-session-retest-integration-validation.md。

- 2026-10-09 用户要求三AI主备全面使用Messages：本地和部署 `.env` 的角色URL及共用BASE_URL已改为Messages，空白备用仍继承，模型/密钥/档位/超时/门槛不变。中转站使用 `hapiopen.cc/v1/messages`，DeepSeek官网使用 `api.deepseek.com/anthropic/v1/messages`；官网确实支持Anthropic格式，不要误认为只有Responses/Chat，也不能直接写官网 `/v1/messages`。`xauusd_fib_volume_pa_ai3.py` 默认入口Messages、版本response-contract-v3，增加x-api-key并保留Bearer，所有Messages模型都用max_tokens/output_config.effort。官网兼容文档明确output_config只支持effort，因此该官方完整路径不发format，usage如实标prompt，Python JSON/质量/策略契约严格校验；其它中转仍优先Schema，不按模型名猜协议或能力。保留历史Chat兼容代码但实际主备没有Chat路线，不自动将失败请求改走Chat。6组现有路由小请求均HTTP200/原模型/end_turn/完整JSON；GPT备用首次30秒读超时，另一次40.69秒成功，不能说Messages消除超时或小请求验证了完整生产审核。回归168项与2项自检通过；预算5、每POST刷新、截断/坏JSON拒绝及全部风控未改，不启动/重启交易。记录见docs/superpowers/specs/2026-10-09-messages-route-migration.md；部署需同步代码和私有.env后用户重启。

- 2026-10-09 黄金多策略研究 `gold-strategy-lab-v1`：用户授权短线/中线/激进模拟实验，新增 `docs/experiments/gold_strategy_lab.py`，纯CSV离线、不导入主脚本/.env/MT5/HTTP、不下单。五策略先固定，随后因直接突破延迟敏感追加时段突破回踩，因H1无成交追加$1000/SL48可行性组；共104组，时间分段并非真正未见样本。正式M1共77951根，7月20日至10月6日；各组$100/.01手/一仓，SL8或16及历史/双成本/60秒延迟/组合压力，H1额外组$1000。时段回踩SL8暂列前向模拟首选：前段25笔+56.43、后段11笔+30.16，后段双成本仅+3.20，去掉9月25日最佳日变-13.66，不能称已验证盈利。直接突破后段延迟-27.59；反转前段-61.95后段+61.28不稳定；SL16多组近耗尽；H1宽止损组仍亏，不推广到生产。候选未强制生产H1/EMA门，参考价/止损缓冲也不同，不把实验当三AI收益或自动接入。保留主脚本/真实env/进程；实验模拟Bid/Ask、延迟成交、实际风险/RR/止损余量、跳空、先SL、现金耗尽，无真实保证金/强平/tick路径。报告 `docs/experiments/2026-10-09-gold-strategy-research.md`，14项实验回归、139项全量通过；原始结果仅 `xau_fib_pa_logs/strategy_lab/`，不可提交或为多开单裁短SL。后续先冻结规则采新行情，保留本地/AI1/AI2/AI3分工，不根据这段历史继续挑参数。

- 2026-10-09 止损精度修复 `protective-stop-v1`：13:00原始M5低点4176.97、缓冲0.8325，BUY边界4176.1375被四舍五入到4176.14后进入缓冲，覆盖检查失败却误报超过8美元；真实参考风险约5.73、实时报价风险约6.12。主脚本共用`round_protective_stop()`，保持两位小数，BUY向下/SELL向上，生成、AI定稿与computed_evidence保护价一致，取整后继续检查8美元；覆盖检查容差不放宽。`reject_inside_m5_wick()`区分真实超限、量化后超限、无法生成和未覆盖，不将小风险生成失败一律称超8。启动日志显示版本，回归`tests/test_stop_price_rounding.py`包含归档BUY/SELL镜像、半分/边界与EMA仍拒绝。15轮当前原始提案均no且参考EMA2.05–4.12ATR；此次修复不是这段连续观望的主因，不保证修后开仓，不改EMA1.5/H1/RR/门槛/实际.env或付费重试。

- 2026-10-09 入场可靠性v2：`xauusd_fib_volume_pa_ai3.py`使用entry-quality-v2，模型保留意见直接嵌入所属维度，不再重复声明ID/归属/severity；level一次声明程度（adequate保留为minor、weak为material、missing为blocking），Python确定生成一致内存卡并汇总。仍严格验证证据路径、同cause/event去重、已有触发和blocking/no规则；普通量/浅回撤不得weak/missing。旧v1有效卡保留兼容标签，历史坏卡不修补、不付费重审、不改no为交易。Schema、模板、提示词和rubric同步，错误明确到路径，JSON Pointer不接受负/前导零列表索引。request-freshness-v2将止损生成下限与实时余量分离：0.6ATR/0.8仍用于M15参考价定稿，实时止损触发侧BUY Bid/SELL Ask至少留2*stop_buffer（当前1.2–2美元），区间考虑点差、SL8和RR1.5，AI2及下单前同函数校验；不再将生成下限作为实时最低距离。归档五份有利报价的区间问题解除，00:56仅余1.01美元和历史秒损仍拒绝，不能说成应成交单。提示词说明本地方案不可执行不剥夺自主提案，并公开Python1.8R目标定稿限制；04:16调整后同快照RR1.495575仍不放行，不给RR加容差。ai1_result.status区分契约错/观望/Python拒绝/待审核。设计与发布范围见docs/superpowers/specs/2026-10-09-entry-gate-reliability.md，回归tests/test_ai_entry_gate_v2.py；未改实际.env/模型/档位/超时/门槛/本地策略。部署需用户自行重启，离线测试不保证供应商永不出错、成交或收益。

- 2026-10-08 双协议与JSON修复 `response-contract-v1`：`xauusd_fib_volume_pa_ai3.py` 按各主备URL路径独立选择Chat或Messages，不能按模型名猜协议。Messages使用顶层system、user消息、anthropic-version、max_tokens、output_config.effort/format，解析text块、stop_reason及缓存独立输入usage；Chat保留reasoning_effort和原token字段转换。Claude主路本地/部署.env改到hapiopen.cc/v1/messages，GPT备用仍Chat，模型/档位/密钥/超时/置信度不改（用户现在自行用AI65临时验证流程，不擅自恢复75，服务器以实际配置为准）。三角色优先严格JSON Schema，明确400不支持才记住并降级；Chat可降JSON object/提示词，Messages保留支持的effort或format，整项output_config不支持才去掉两项，日志及usage记录真实发送模式/档位。所有恢复共用原5次预算、每POST刷新、读超时不重复补发。Messages必填max_tokens拒绝时切独立备用或失败，不能换Chat上限字段/取消上限。AI1补完整JSON模板，wait_basis必须顶层；parse_json_object报告语法行列/字段重复/非有限数值，拒绝数组中挑交易和猜补括号。已确认10:25历史回复HTTP200/stop/output2580却少外层括号且wait_basis嵌错，不是超时/触顶，不能修补后下单。正文格式错误只记录原文和具体诊断、不付费重审，不放宽质量卡或AI2/Python门。三角色截断/非最终结束/回执输出超过发送上限均失败且不重复生成；超限检测不能撤回上游费用，第三方忽略参数或晚到账单仍需服务方核实。回归 `tests/test_ai_response_contract.py`，部署需用户重启；不声称离线测试保证中转schema/effort透传或解决所有长尾。

- 2026-10-08 行情刷新 `request-freshness-v1`：昨晚票据595017736实际SELL成交4128.23、止损4128.63，一秒后在4128.63平仓、净亏0.40；不能归因AI-3主动平仓。旧代码重试复用快照，AI-2也沿用AI-1报价，执行仅检查最大止损和RR，贴近止损会出现虚高RR。现 `xauusd_fib_volume_pa_ai3.py` 的 `call_chat(refresh_request=...)` 在每次实际POST前重建请求（含主备、参数兼容），采集失败不重发旧提示词；AI-1结果绑定成功请求的完整行情/质量卡，AI-2每次刷新Bid/Ask，对照原快照并重算固定SL/TP执行区间。区间由既有 `entry_risk_limits(atr)` 和RR1.5推导，止损触发侧还需 `stop_buffer(atr)` 空间，实时EMA21追价不得超过1.5ATR；不是允许AI移动止损或扩大区间。新的已收盘H1/M15/M5出现时旧提案停止审核/执行。下单前用同一提交tick再检查，AI-3重试刷新原持仓/行情，原票消失不接管新仓。基础脚本新增默认空校验钩子，纯本地策略与M15参考价不变；开仓通知区分提交报价/回执成交价。用量日志带snapshot_id/request_quote，启动日志显示刷新版本。离线回归见 `tests/test_ai_request_freshness.py`；没有改.env/模型档位、没有增加付费重试，没有启动进程，不能据此声称提高收益或彻底消除秒损。

- 2026-10-08 `cost-latency-v2`验收：71项离线测试及2项内置自检通过，交易日志/持仓状态哈希不变。修复后真实call_chat完整历史审核：Grok主路仍有90.6秒读超时；自动切DeepSeek low/40秒后，备用17.6秒返回完整REJECT，整轮110.2秒。备用直接验证19.8秒也成功。已恢复的是完整备用审核链路，不保证主路从不超时、服务器时延或历史单必定开仓。没有启动/重启交易，URL/密钥/模型保持用户配置。

- 2026-10-08 AI2完整审核超时修复 `cost-latency-v2`：修正配置后的11:26轮确认主路Grok与官网DeepSeek都读超时，备用切换有效。最新完整快照本机复放：Grok medium约85.7秒返回；DeepSeek V4 Pro medium（官方映射high）0.47秒收到HTTP200头、44.6秒返回length，4096 token全部用于reasoning，最终content空；同输入仅改low约13.8秒返回完整REJECT。不能把“连得上官网”当成完整审核能在40秒内完成，也不能仅延时后放行截断。`AI2_TIMEOUT`默认90秒，本地/部署.env同步；部署已配置的DeepSeek备用effort改low并设`FIB_PA_AI2_BACKUP_TIMEOUT=40`。新增各角色可选`FIB_PA_AI{n}_BACKUP_TIMEOUT`，正整数秒，空/非法/非正值继承主路；单独超时项不形成独立备用，未改URL/密钥/模型。requests响应hook保留响应头状态/ID/耗时，正文超时用量仍未知；usage新增read_timeout_sec/headers_elapsed_sec/body_complete。保留总预算5、读超时不重复补发、AI1/AI2共享360秒、截断拒绝和全部策略硬限制。上次小请求探测不足以验证完整审核的思考额度；以完整快照验收，但单次本机成功不保证服务器时延或收益。不因有效REJECT继续切模型。回归见tests/test_ai_cost_control.py，部署需用户自行重启，代理不启动交易。

- 2026-10-08 成本/延迟优化 `cost-latency-v1`：客户端每轮只有system/user两条消息，不累积聊天历史。用户截图当前输出约3.3k–6k token，早期几百；不要都归因中转或缓存。`xauusd_fib_volume_pa_ai3.py`精简角色重复规则并要求短JSON，保留quality_rubric/归一化和全部交易硬限制；`compact_market_pack()`只去原始K线可由OHLC还原的candle_parts、相等元数据及完全重复时间列表，完整K线窗口、computed_evidence、本地拒绝理由不删。规则/rubric提前利于缓存，不声称必命中。AI2保留最终计划/原始提案理由/完整市场，去重复details和评分卡；AI3仅发送副本去评分卡，持久化证据不删。`reasoning_effort`为标准Chat参数；默认AI1/AI2/AI3生成上限4096/4096/2048（`FIB_PA_AI{n}_MAX_TOKENS`），拒绝上限参数只转换字段，不无界重发；length/max_tokens截断不能执行。每实际POST写 `xau_fib_pa_logs/ai_usage/YYYY-MM-DD.jsonl`，含call_id/decision_id/trigger(startup/M15/M5/M1)/attempt/主备/ID/耗时/参数/usage，未知为null不当免费、不猜费率、不把缓存miss当写入，晚到回复仍记用量。默认读超时停止同通道补发，有备用可切换；这会牺牲无备用慢线路靠重复请求碰成功的机会，不要未经授权改回5份长生成。回归 `tests/test_ai_cost_control.py`，设计 `docs/superpowers/specs/2026-10-08-ai-cost-latency-optimization.md`。离线验证不能证明收益/实际账单减少，代理不启动或重启交易进程。

- 2026-10-08 主备AI接口：用户要求AI-1/2/3各有备用，未填项沿用本角色主配置。`.env`及`.env.example`新增 `AI{1,2,3}_BACKUP_URL` / `_BACKUP_KEY`、`FIB_PA_AI{1,2,3}_BACKUP_MODEL` / `_BACKUP_EFFORT`，逐项空白继承，超时沿用原角色值；完全相同的主备不视为独立通道。`backup_chat_config()`按角色解析，`call_chat()`每轮从主接口开始，首次接口故障后在剩余最多5次总预算内使用备用；不等主接口耗完5次才再发5次。正常观望/审核REJECT及策略协议验证失败不因交易未放行而重新找模型；备用仍使用同角色提示词和Python硬限制。日志标明primary/backup、总尝试序号、备用host/model，只显示key是否配置。API错误耗尽以AIRequestError报真实原因，任务过期/停止不再请求。配置留空时没有新服务可用，用户需填真实备用URL/密钥；代理不猜测服务或密钥，不启动交易进程。

- 2026-10-08 日志跨日修复：旧 `setup_logger()` / `attach_ai_logger()` 在启动时固定日期文件名，导致8号记录继续写7号文件。两者现共用 `DailyDurableFileHandler`，按每条 LogRecord.created 的服务器本地日期切换 `Fib_PA_YYYY-MM-DD.log` / `Fib_PA_AI3_YYYY-MM-DD.log`，追加写入并保留flush/fsync；AI日志重复挂载不再重复写。线程锁保护切换，迟到的旧日期记录仍写其所属日期。跨午夜、延迟记录、跨多天/重启追加和并发分流回归见 `tests/test_daily_logging.py`。日期切换在当天第一条日志到来时发生，运行中的旧进程仍需用户自行重启加载代码。
- 2026-10-08 复盘服务器 `xau_fib_pa_logs/n/Fib_PA_AI3_2026-10-07.log`，实际覆盖7号20:25至8号08:55。以8号完成时间统计AI-1有21轮五次请求后失败（含7号跨日未完的一轮），另有21份有效结果：20次no、仅04:45本地73/AI质量80的SELL进入AI-2，后者至04:49仍连续5次40秒超时，未返回审核结果。04:01日志还明确出现hapiopen.cc域名解析失败11001，不可只归因模型推理耗时。两天可见的三笔双过线提案均因AI-2超时未审核；没有有效AI-2审核结果或成功下单日志。06:00以后成功返回的AI-1均为no，涉及缺少触发、目标空间、宽震荡中轴/窄震荡pullback限制；分数高仍可有硬拒绝。只有两份文本日志，没有服务器决策JSONL，不能独立复核每个AI拒绝细节，更不能为强行出单降低门槛。日志跨日修复不解决网络/API超时，保持角色、止损8、RR1.5和75门槛。

- 2026-10-07 AI-2日志须区分来源：本地普通 `Fib_PA_*.log` 中五次 `ConnectionError: connection refused` 在几十毫秒内完成，并伴随 AI-1 两次假超时后成功、AI-3 `Timeout: slow` 后任务过期，来自 `run_http_retry_self_test()` 模拟请求。该函数现使用独立 NullHandler logger，finally 恢复交易 logger，模拟异常不再写交易日志，历史日志保留。用户随后拉下的服务器日志 `xau_fib_pa_logs/n/Fib_PA_AI3_2026-10-07.log` 确有两轮真实AI-2连续5次40秒ReadTimeout：22:52:21本地100/AI质量80提案后至22:55:52，23:10:46本地80/AI质量80提案后至23:14:18，均未得到审核结果所以拒绝开仓。相同hapiopen.cc上的AI-1也在23:00-23:05连续5次60秒超时。最小连通请求6.83秒成功不能证明完整审核时延；不要将服务器事故误判为自测，也不能从ReadTimeout单独断定服务端排队或网络原因。入场接管360秒是AI-1/AI-2共享时间，不能不核对总耗时就单独延长AI-2超时。

- 2026-10-07 入场证据v5和质量评分entry-quality-v1：18:08-20:20的v3快照中10次no、5次SELL54-60，五次交易提案均被自由置信度75挡在AI-2前。本轮将AI自由分替换为可审计质量总分，75数值保留但量尺已改变：结构/触发/止损保护/目标/反证五项由AI选择strong20、adequate16、weak8、missing0，Python汇总，非校准胜率。每项引用真实输入JSON Pointer；同一原因/事件只归一项，minor不能单独评weak/missing，blocking不能提出交易。观望须返回wait_basis；M15或M5规则已命中时不得声称不存在触发。没有评分卡或证据引用无效则明确拒绝，不回退自由分、不把no自动改成开仓。模型仍自主判断重大反证和提案，AI-2及Python风险检查保留。本地策略、.env、目标规则未改；新增local_plan_feasibility分别显示参考价和历史快照报价RR，价差本身不算事实冲突。质量卡、扣分、等待依据写入快照及入场上下文，通知显示AI-1质量分。该评分尺度属于待验证策略设计，不能声称收益改善或75与旧尺度严格程度相同。

- 2026-10-07 入场证据 v4：修复19:15/19:25将M15假突破与M5零分误当事实冲突的问题。`classify_price_action(..., evidence=...)`直接输出原规则命中、ATR及前6根极值；AI输入按M15/M5与方向分别提供对应K线、时间和分数。sell假突破只要求本周期high超过前6根最高且close收回，不要求阴线或长上影；buy镜像。两周期无须同时触发，不能因M5零分否认M15事实；质量仍由AI判断，保留AI75和全部硬限制。部署副本同步为entry-evidence-v4，需用户自行重启。

- 2026-10-07 用户明确保留本地粗筛、AI-1自主提案精筛、AI-2审核、AI-3护盘结构，要求先brainstorming并由代理决定具体策略、不反复询问。设计已落实到 `xauusd_fib_volume_pa_ai3.py` 和 `xauusd_fib_volume_pa_trader.py`：AI-1仍可自主提案，新增收盘触发/失效/目标证据，AI-2独立复核Python最终价格，AI-3按持久化入场理由护盘，不取消AI自主提案权。
- AI-3的MFE/MAE路径已修复：本地run_guardian现在对BUY/SELL都持续更新最高价和最低价；重启或初始风险缺失时上下文标记为未知并带覆盖起点，不把缺失历史填成零。

- 2026-10-07 AI 策略实现：AI-1 区分 H1 方向许可与入场结构有效性，要求目标依据和反证；AI-2 收到完整 market_evidence，以原始K线/Fib核实明确结构矛盾并审核Python最终方案；AI-3 不可从当前 r_now、持仓时长或单根反向K线推断持续浮亏，未知入场结构不得声称失效。实现保留本地评分和setup规则，尚未证明提高收益。
- 2026-10-07 AI 入场证据契约 v3：`xauusd_fib_volume_pa_ai3.py` 输出 Fib 容差/探入、M5确认与保护极值、突破/区间窗口、K线实体/影线和障碍证据。v2 曾把 Fib 端点直接标成独立障碍、只按价格合并事件、把箱体外收盘误标成回踩守住，本轮已修正。回踩证据与本地评分共用 `breakout_retest_facts()`，量能仍仅加减分。只有极值相同且M5时间落在对应M15内才跨周期合并，同价不同M15触碰分别保留；确认拐点保留，候选强度交AI评估，缺少来源不猜时间。障碍提供来源K线、后续收盘突破和本地反向价格行为，未知归属本身不构成硬否决。AI-1自主提案、Python硬限制、AI-2审核及决策快照保留，不把“双过线”自动改成开仓，尚未证明收益改善。
- 2026-10-07 AI-3 增加已收盘 M15/H1 输入及周期元数据。失效理由指定哪个周期就必须核对那个周期，禁止用M1/M5收盘代替M15/H1收盘；数据或覆盖不足标未知，不得据此声称结构失效。离线回归见 `tests/test_ai_entry_evidence.py`，没有启动或重启交易进程。
- backtest 下六份旧 TXT 均为本地阈值70，不能代表当前55/止损8配置。214459（止损6）净+5.45但最低资金-39.90；214606（止损4）净-25.82且最低-14.85，均记录中途归零。不可用期末余额认定可持续盈利；203048 的-70.06属于已撤回M5入场价实验，不当作当前版本结果。

- “M5时机未到”和“本地评分”日志都会带 BUY/SELL 分和拒绝原因。高分没开单时先看拒绝原因，通常是 8 美元 M5 极值和 1.5 ATR 追价，不是 M5 单独拦截。
- M5 复核的入场价仍用上一根已收盘 M15 的收盘价。2026-10-06 回测过改用最新 M5 收盘价：同一份 14 天数据从 101 笔、-1.65 美元变成 175 笔、-70.06 美元，已撤回，不要再改回去。
- 飞书的 AI-1/Python 未放行、AI-2 未通过会带本地置信度、AI-1 置信度和两个门槛。平仓通知带开仓置信度，只记在内存里，进程重启前开的仓显示“未知”。
- 飞书平仓通知统一显示本次净盈亏（正数带 `+`、负数带 `-`）、当前余额和当前净值；服务器历史成交暂时查不到时标明盈亏未知并保留最后浮动盈亏。AI-3/护盘直接平仓也走这套通知。
- AI-3 可以在价格碰到初始止损前平仓：浮亏持续、M1/M5 收盘朝止损推进且没有有利拒绝时 close，也可以把止损收紧到更近的结构外。止损只能收紧不能放宽。初始止损仍是服务器上的最坏保护。
- AI-1 选择观望时，如果理由里没有“观望”二字，仍会发飞书，通知偏密。
- AI-1结果日志和相关飞书门槛行现在明确显示“AI-1动作：观望/BUY/SELL”；明确观望仍按原规则降噪，不改变开仓判断。
- 2026-10-06 18:00 至 10-07 11:55 复盘：MT5 历史核实 6 笔 BUY 全部平仓，净 +0.62 美元（+9.93、-0.90、-2.16、-0.98、-0.90、-4.37），开仓 AI 分数为 66、68、68、66、72、70。75 会挡住这些原提案，包括盈利单；恢复门槛是结束流程验证，不是证明高分收益更好。高分 action=no 是观望置信度，不是高分开仓信号。详见 `md/overnight_review_20261007.md`。
- 运行中的进程不会自动加载代码或 `.env`。本次 AI 门槛恢复 75 要用户自己重启 `xauusd_fib_volume_pa_ai3.py` 后才生效；启动日志应显示 `confidence=75`，结果日志应显示“本地55 / AI75”。不要由代理启动或重启交易进程。
