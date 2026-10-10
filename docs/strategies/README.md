# 策略文件使用说明

现在修改或新增交易策略，入口是 `strategies/definitions/*.py`。框架自动发现其中的 `STRATEGY`，按当前时段评分，沿用本地粗筛 → AI-1自主提案 → AI-2审核 → AI-3护盘。当前默认只启用研究报告优先候选 `session_retest`（欧美时段突破回踩），仅模拟账户；原三个定义保留但默认关闭。

## 修改现有策略

| 文件 | 内容 |
| --- | --- |
| `session_retest.py` | 默认启用：伦敦/纽约当地建箱、首次突破回踩，3R、180分钟、一窗口一次、10分钟平仓冷却 |
| `region_retest.py` | 默认禁用：盘中确认区域突破后的首次M5回踩，2.5R上限、120分钟、10分钟平仓冷却 |
| `region_rejection.py` | 默认禁用：盘中确认区域的首次触碰拒绝，2.5R上限、120分钟、10分钟平仓冷却 |
| `pullback.py` | 趋势回撤评分、动量加减分、市场状态约束和三角色规则 |
| `range_edge.py` | 区间长度、宽度、边缘范围、拒绝条件、评分和详细证据 |
| `breakout.py` | 箱体长度、突破幅度、回踩容差、量能评分和详细证据 |

每个文件集中声明 `P` 参数、`evaluate(c)` 逻辑和 `STRATEGY`。修改参数时，评分、证据函数和AI规则都应引用同一个 `P`；修改算法时，也要更新同文件的证据与规则，框架不能推导任意Python代码的金融含义。旧三个策略的 `target_r` 为1.8，时段回踩为3.0，区域定义为2.5；本地85分表示规则确认，不是胜率。

`STRATEGY` 的字段：

- `id`：唯一的小写策略标识；`name`：显示名称；`version`：供人工追踪的版本号。
- `enabled`：是否启用。`sessions` 指定允许的时段及并列评分的优先级，数值小者先选；优先级相同按id排序。分数更高者仍优先。
- `parameters`：向AI公开的参数；`evaluate`：实际评分函数；`ai_rules`：`entry/review/guardian` 三段策略规则。
- `target_r`：参考价下的目标倍数上限，允许1.5–6。Python仍可按结构收近目标，并检查实际报价RR。提高它不等于必达目标。
- `m5_boost`：是否沿用框架的M5拒绝确认加分；关闭后M5止损覆盖、时机和实时风控仍执行。
- `momentum`：可选RSI/MACD加减分函数，返回 `(整数加分, 说明)`。
- `regime_filter`：`pullback` 沿用窄区间观望和宽区间边缘约束；`none` 由策略自己在 `blockers` 中声明条件。

`session_retest` 按 `Europe/London` / `America/New_York` 的当地日期及夏令时判断：08:00–08:30六根闭合M5建箱，08:30–11:00首次突破后的首次回踩，完整规则见接入设计。其他策略的宿主时段标签为北京时间：`asia` 07–15、`london` 15–20、`overlap` 20–01、`late_us` 01–07，未按夏令时调整。

## 新增策略

复制 `docs/strategies/strategy_template.py` 到 `strategies/definitions/`，给文件和 `id` 取新名称，再实现并验证策略。模板默认 `enabled=False`，只是接口示例，没有收益结论。无需修改主脚本的策略列表、分支或AI提示词。

`evaluate(c)` 收到已收盘的M15/H1/M5数据、ATR、ADX、方向、时段、显式决策时间 `now`、已核实 `server_utc_offset` 及公共数学函数。实时Bid/Ask由框架在每次AI请求、AI-2审核和下单前刷新；不让插件自行调用交易接口。当前窗口为M15最多160根、H1最多120根、M5最多80根；需要其他数据源或周期时，必须扩展框架。

评分函数返回 `Assessment(score, details, structure, evidence, blockers)`：

- `score` 必须为0–100整数。分数仅是本地评分，不代替AI质量门槛。
- `details` 给出说明，常用 `fib/volume/price_action/trend/fib_score/pa_score`；M5加分只在相应评分字段存在时使用。
- `structure` 可为公共 `FibLevels` 或 `None`，供框架生成保护价格。
- `evidence` 为JSON可序列化的真实事实，禁止NaN。AI候选池中的路径是 `/opportunities/<索引>/strategy_evidence/buy/evidence/...` 或sell。
- `blockers` 为字符串tuple。未完成策略硬条件时必须写入，Python会拦住本地与AI自主方案；不要只在提示词里声明。低分、普通量等软意见应留在评分与证据，不要误写为硬条件。

自定义收盘触发可以声明 `evidence.closed_trigger={confirmed,timeframe,bar_time}`。当confirmed为true，时间必须来自本次输入中真实的已收盘M15/M5；质量契约据此阻止模型把已触发说成完全没有触发。它不自动批准交易。

宿主逐项完成所有启用定义的计划，再为纯本地模式选择可执行的最高分方案。AI收到的是结构合法候选池：本地分低或参考方案失败仍可保留，允许AI在同一公共风险边界内自主提出方案。每轮只发一份共享行情，AI-1返回成功请求中真实的 `opportunity_id`；质量卡引用所属候选或共享OHLC，不能借另一候选的触发，也不能把原本地赢家的别名当所选方案。AI-2保留选中项的原索引供引用核验，AI-3沿用入场保存的契约。无结构合法候选时写本地决策快照，不请求AI。

新M15评估保留上述自主提案能力。额外M5复核先记录全部本地候选，再要求本地BUY/SELL方案可执行且分数达到实际本地门槛才派发AI；合法但低分或本地计划失败的池不会每5分钟追加付费请求。AI定稿的`reference_price`保持数值，原始来源/时间另存`reference_price_metadata`，审核和执行不能把元数据对象当价格。

参考来源由契约声明：`entry_reference=closed_m15/closed_m5`，`stop_reference=structure/m5_extreme/region`；研究定义单独使用确认M5参考，旧三个策略仍使用M15。区域保护还要覆盖有来源的失效锚，目标先考虑真实的反向区域边缘。`max_hold_minutes/cooldown_seconds/one_entry_per_event/demo_only` 声明生命周期，Python保存并执行，已有仓不随定义修改延长。公共边界仍是H1方向、M5保护极值、止损8美元、RR至少1.5、EMA21追价1.5ATR、0.01手和最多1仓。服务器止损、本地锁盈及结构护盘仍是框架的公共保护；AI-3再结合入场保存的策略护盘规则。插件不改变这些限制，不能靠文本覆盖它们。

区域定义用 `parameters.region_event_kind=retest/rejection` 声明已支持的事件算法，宿主从原始行情复算其区域、触发、失效锚、窗口及完整目标障碍；不会根据策略ID猜算法。改名或新增同接口定义不需要给宿主加ID分支。新增其他区域来源或算法仍须扩展辅助模块、测试并自行重启，不能只写一段提示词。

`one_entry_per_event=True` 的新策略须提供 `entry_event`，包括真实执行键、周期起点、原始触发时间、显式UTC收盘与排他有效期、原始K线引用；有效期最多确认后300秒并受原窗口/区域期限约束。缺来源、缺对应K线、错方向、未来或过期均拒绝。原 `session_retest` 支持显式旧证据适配，未保存的历史事实保持未知。下单前原子预留执行键；拒单或回执不确定不会释放事件。新订单comment含短执行token，恢复须匹配原事件、symbol、magic、方向及契约；旧标记有歧义时不猜最近事件。

## 加载和验证

先部署完整的新框架并自行重启交易进程一次。此后保存策略文件，下一次M15评估、符合条件的M5复核或请求刷新时自动检查文件内容SHA256；不是立即追加一次付费分析。读取期间文件变化、语法错、重复id、非法声明或评分异常，会停止本轮新开仓并记录具体原因，已有持仓继续护盘。

正在审核的提案绑定原策略内容指纹。策略文件变化后，该提案在AI-1返回、AI-2请求/返回或提交前停止，等待后续评估；不会把旧信号与新规则混用。已有持仓的入场契约和证据持久保存，AI-3继续用入场版本。回测在开始时冻结整套定义，运行中编辑文件不会改变这次结果。

自动发现只覆盖定义目录下不以下划线开头的 `.py`；依赖其他辅助文件、框架代码或 `.env` 的修改需要自行重启。策略是受信任的本地Python代码，加载器不是安全沙箱，也不是自然语言自动编译器。

```powershell
py -3.12 -m strategies.check
py -3.12 -m unittest discover -s tests -q
py -3.12 xauusd_fib_volume_pa_trader.py --self-test
py -3.12 xauusd_fib_volume_pa_ai3.py --self-test
py -3.12 xauusd_fib_pa_backtest.py --offline --threshold 55 --max-sl 8 --balance 100
```

第一条只加载并验证策略声明，打印指纹、时段和参数，不连接MT5、不请求AI。内置自检固定旧三策略数学样例；事件、区域、形态、候选池、AI绑定与生命周期另由相应 `test_entry_events.py`、`test_intraday_regions.py`、`test_intraday_patterns.py`、`test_strategy_opportunities.py`、`test_ai_strategy_opportunities.py` 及回放测试覆盖。离线回测不含AI，不保证实际成交或收益。

已有本地历史缓存时，可以仅在这次离线回放中启用研究定义，不编辑定义或实际 `.env`：

```powershell
py -3.12 xauusd_fib_pa_backtest.py --offline --strategies region_retest,region_rejection --threshold 55 --max-sl 8 --balance 100 --output-label regions
py -3.12 xauusd_fib_pa_backtest.py --offline --strategies session_retest,region_retest,region_rejection --threshold 55 --max-sl 8 --balance 100 --entry-delay-seconds 120 --spread-multiplier 2 --output-label all3-delay120-spread2
```

`--strategies`只允许与`--offline`合用；无该参数沿用默认启用项。延迟必须是非负整分钟秒数，成交使用目标分钟的真实缓存Bid/Ask，保持原SL/TP及来源；缺分钟、新收盘K线、事件过期或实时报价不合法就拒绝。点差倍数同时影响入场、护盘和退出。每次输出带独立时间与标识的CSV/TXT/JSON，JSON记录启用项、原指纹、延迟、点差、零成交日和拒绝漏斗。`--split-date`默认2026-09-15，这份缓存已用于研究，前后分段不能当独立样本外结果。

事件文件默认位于日志目录，可用 `FIB_PA_STRATEGY_STATE_FILE` 指定路径。删除会丢失已用窗口和期限，迁移同一账户须保留。写入失败停止新入场；旧期限仍可读取供护盘。只运行一个进程实例。
