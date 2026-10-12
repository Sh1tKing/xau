# 黄金分级利润保护构建与验证

用户授权“开始构建代码”后实现[护盘设计](../superpowers/specs/2026-10-13-profit-floor-guardian.md)。黄金新仓默认使用`profit-floor-v1`：本地及时保护部分可执行的预计净利润，AI3负责原交易结构是否应继续持有。只有新订单冻结新规则；旧仓不迁移。

## 行为与边界

`strategies/profit_guard.py`提供冻结参数、报价确认、分级比例和单调保护线；`xauusd_fib_volume_pa_trader.py`负责实际MT5现金估计、身份/时效检查、费用历史、保存/恢复和SL执行。`xauusd_fib_volume_pa_ai3.py`共享同一RLock，快照明确区分desired目标与已确认protected SL，并提供费用状态和最小回撤空间，AI3不能撤销或放宽保护。

运行配置的0.5秒轮询保留。最优平仓侧报价持续1秒、推进至少max(0.20价格单位,0.10×ATR)才晋级；初始预计净保护至少0.20账户币种，30%/1R40%/2R50%，没有1R启动门。最小空间max(1×M5 ATR,3×点差,0.50价格单位,broker止损/冻结距离+1tick)，ATR参考只增不降。首次模拟参数不声称最优。

BUY使用Bid、SELL使用Ask，因此不能再次扣点差。单位现金收益来自[MT5 order_calc_profit](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)，按账户币种和实际手数估算；入场费用通过[按position查询成交历史](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py)确认，再预留平仓费用并计入负swap。费用未知时明确为估计，不声明锁盈金额必然实现。

新仓不再执行旧sigmoid盈利前虚拟退出、旧保本触发或本地结构提前退出，原SL/TP和冻结持仓期限仍有效。AI3请求失败时原服务器保护及本地利润保护继续；这不等于本地能够代替AI3的结构判断。单独运行本地脚本/回放没有AI3，不代表三AI组合表现。

同通道HTTP预算、JSON/证据契约、AI1/AI2、三盘定义、H1/SL8/RR1.5/M5极值/EMA追价/.01单仓限制保持。实际.env、crypto_demo和local_guard_demo没有修改。

## 持久化与执行

当前adaptive模式每笔订单在原事件reserve中保存guardian_policy、原SL/TP、UTC偏移，再发送订单；成交后精确绑定ticket/identifier/symbol/magic/方向/开仓价/手数/时间，保存原风险、水位、参数和确认线到`guardian_profit_state.json`。若成交后绑定前崩溃，原execution token恢复该事件和冻结规则/期限。旧固定非事件定义的即时绑定路径保留，但本次崩溃恢复验证针对当前默认事件模式，不宣称为无事件订单提供额外回执恢复协议。

原子写入使用临时文件、fsync和replace。状态文件损坏不覆盖，不把已知新仓重建为legacy；不可写时新订单不POST，持仓不确认新的锁盈。失败SL修改仅保留desired意图，下一次正常轮询按最新价重新检查；成功broker回执才推进protected。保存失败不改变服务器已接受的保护。

每次修改重新读取最新持仓和SL/TP，按经纪商tick向保护外侧量化，检查stop/freeze及TP；不夹价、不放宽建议。最新价不足以留下空间时暂缓同一目标。原确认线被删除/放宽时尝试恢复，价格已穿线按最新身份/报价退出；即使ATR或现金模型缺失也保留/恢复原线。已保存UTC偏移固定用于时效检查（允许1秒以内时钟领先、报价最老5秒），不能将整小时旧tick通过重算偏移当成新行情。

## 离线工程验证

安全入口`python docs/experiments/verify_gold_profit_guard.py`禁止未mock的MT5连接/下单和HTTP请求，并隔离事件、入场上下文及锁盈状态。完整黄金回归538项；新增40项利润规则/执行/恢复测试及5项M1回放测试。覆盖双向、成本、微利空间不足、亏损不追损、尖刺/持续确认、ATR下降、AI3 hold/过近/回退、失败重试、当前报价变化、精确身份、整小时旧tick、原SL恢复、原事件崩溃恢复及90分钟期限、原子replace失败、坏状态、旧仓和observe。已有493项入场/AI请求/契约等回归继续覆盖。

另执行两个内置self-test、`python -m strategies.check`、回放`--help`和源码编译。三盘定义指纹维持`0a16c78772e79b466eceb7b150182da1c7787eb015157d893f93261e1394ae89`。真实配置及现有状态SHA前后保持；未启动/重启交易，不调用付费AI。

## 固定入场缓存控制：结果较差

按预先登记的默认参数，从现有旧本地逐笔回放中取时间顺序最后100个入场（2026-10-01 20:25～10-06 16:25），不按方向、策略或盈亏选样。入口、SL/TP固定，两组同为0.01手、SL8、90分钟期限、每笔0.10现金费用假设。调用同一个`Backtest`引擎，逐票独立回放，不激活旧策略，不修改.env。

运行：

```powershell
python docs/experiments/replay_gold_profit_guard.py --entries xau_fib_pa_logs/backtest/trades_legacy_base_20261010_121738_891629_e76a49b4.csv --limit 100
```

新退出相对旧退出：17票现金结果改善，53票变差，30票相同；独立票现金合计旧−21.21、新−64.52。**新纯本地对照整体更差，不支持收益提升。** 不据此调默认参数，亦不能将测试通过当收益验证。

该控制没有AI1/2/3；新方案去掉旧亏损阶段虚拟/本地结构退出，把策略判断交给不存在于回放中的AI3，所以它同时体现分工变化。新规则只在M1收盘报价形成目标，下一根才生效；不按当前high先生成同根止损，也没有模拟持续1秒确认或秒级tick路径、真实broker限制和完整滑点。不同退出可能持仓重叠，独立票合计不是单仓账户资金曲线、可持续收益或成交频率。旧研究缓存重复使用，不是独立OOS，也不是当前三盘AI方案的成交样本。原始结果和输入/代码SHA写到忽略目录`xau_fib_pa_logs/backtest/profit_guard_fixed_*.json`。

## 部署

现有`server_deploy`原文件同步，完整包含两个主脚本、回放脚本、`strategies/profit_guard.py`、完整strategies、测试和说明；以MANIFEST的确切字节SHA和代码签名检查加载版本。没有创建修复包。保留服务器自己的.env、事件ledger、AI3上下文及最新guardian_profit_state；用户复制代码后自行重启，保持单实例。下一步证据来自新模拟仓实际日志，不能保证每笔利润都被保住或AI3一定改善此缓存结果。

本次最终核对：根目录538项/54.747秒、部署黄金清单538项/55.543秒均通过，两地各两个内置自检通过。部署清单113个文件，109个根目录源文件与副本逐字节一致，另4个安装/启动/依赖辅助文件保留；遗留币种文件不在黄金清单内。运行代码签名`0d2970b0fdecd45f`，策略定义指纹未变。MANIFEST记录实际源提交，安装/启动进程仍由用户执行。
