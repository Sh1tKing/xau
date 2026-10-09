# 策略文件使用说明

现在修改或新增交易策略，入口是 `strategies/definitions/*.py`。框架自动发现其中的 `STRATEGY`，按当前时段评分，沿用本地粗筛 → AI-1自主提案 → AI-2审核 → AI-3护盘。默认仍启用原来的趋势回撤、区间边缘、突破回踩，没有把研究实验自动接入账户。

## 修改现有策略

| 文件 | 内容 |
| --- | --- |
| `pullback.py` | 趋势回撤评分、动量加减分、市场状态约束和三角色规则 |
| `range_edge.py` | 区间长度、宽度、边缘范围、拒绝条件、评分和详细证据 |
| `breakout.py` | 箱体长度、突破幅度、回踩容差、量能评分和详细证据 |

每个文件集中声明 `P` 参数、`evaluate(c)` 逻辑和 `STRATEGY`。修改参数时，评分、证据函数和AI规则都应引用同一个 `P`；修改算法时，也要更新同文件的证据与规则，框架不能推导任意Python代码的金融含义。默认三个策略的 `target_r` 都是1.8。

`STRATEGY` 的字段：

- `id`：唯一的小写策略标识；`name`：显示名称；`version`：供人工追踪的版本号。
- `enabled`：是否启用。`sessions` 指定允许的时段及并列评分的优先级，数值小者先选；优先级相同按id排序。分数更高者仍优先。
- `parameters`：向AI公开的参数；`evaluate`：实际评分函数；`ai_rules`：`entry/review/guardian` 三段策略规则。
- `target_r`：参考价下的目标倍数上限，允许1.5–6。Python仍可按结构收近目标，并检查实际报价RR。提高它不等于必达目标。
- `m5_boost`：是否沿用框架的M5拒绝确认加分；关闭后M5止损覆盖、时机和实时风控仍执行。
- `momentum`：可选RSI/MACD加减分函数，返回 `(整数加分, 说明)`。
- `regime_filter`：`pullback` 沿用窄区间观望和宽区间边缘约束；`none` 由策略自己在 `blockers` 中声明条件。

时段为北京时间：`asia` 07–15、`london` 15–20、`overlap` 20–01、`late_us` 01–07，未按夏令时调整。

## 新增策略

复制 `docs/strategies/strategy_template.py` 到 `strategies/definitions/`，给文件和 `id` 取新名称，再实现并验证策略。模板默认 `enabled=False`，只是接口示例，没有收益结论。无需修改主脚本的策略列表、分支或AI提示词。

`evaluate(c)` 收到已收盘的M15/H1/M5数据、ATR、ADX、方向、时段及公共数学函数。实时Bid/Ask由框架在每次AI请求、AI-2审核和下单前刷新；不让插件自行调用交易接口。当前窗口为M15最多160根、H1最多120根、M5最多40根；需要其他数据源或周期时，必须扩展框架。

评分函数返回 `Assessment(score, details, structure, evidence, blockers)`：

- `score` 必须为0–100整数。分数仅是本地评分，不代替AI质量门槛。
- `details` 给出说明，常用 `fib/volume/price_action/trend/fib_score/pa_score`；M5加分只在相应评分字段存在时使用。
- `structure` 可为公共 `FibLevels` 或 `None`，供框架生成保护价格。
- `evidence` 为JSON可序列化的真实事实，禁止NaN。AI收到的路径是 `/strategy_evidence/buy/evidence/...` 或sell。
- `blockers` 为字符串tuple。未完成策略硬条件时必须写入，Python会拦住本地与AI自主方案；不要只在提示词里声明。低分、普通量等软意见应留在评分与证据，不要误写为硬条件。

自定义收盘触发可以声明 `evidence.closed_trigger={confirmed,timeframe,bar_time}`。当confirmed为true，时间必须来自本次输入中真实的已收盘M15/M5；质量契约据此阻止模型把已触发说成完全没有触发。它不自动批准交易。

公共边界仍是H1方向、M15参考价、M5保护极值、止损8美元、RR至少1.5、EMA21追价1.5ATR、0.01手和最多1仓。服务器止损、本地锁盈及结构护盘仍是框架的公共保护；AI-3再结合入场保存的策略护盘规则。插件不改变这些限制，不能靠文本覆盖它们。

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

第一条只加载并验证策略声明，打印指纹、时段和参数，不连接MT5、不请求AI。自检针对默认三个策略；关闭或改动默认策略后，应使用相应策略的测试与回测，不将原样例失败机械解释为框架故障。离线回测不含AI，不保证实际成交或收益。
