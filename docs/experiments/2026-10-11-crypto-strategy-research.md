# BTC / ETH 副本研究与初步可行性

结论：三AI、定义重载和 Python 最后执行门可以复用，黄金固定 8 美元价格距离和 .01 手不能照搬。本轮复制当前黄金框架建立独立 MT5 CFD DEMO 副本，BTC/ETH 使用各自策略与波动风控。第一版每次选择一个币，账户单仓，不接交易所、不补仓、不启动交易。工程实现与离线模拟链路不构成盈利证明。

## 学习结果及实际取舍

BTC/ETH 原生市场全天交易；历史实证发现周末及不同时刻的波动/成交量分布不同，不能把黄金休市日或亚欧美固定开盘窗口直接搬过来。副本所有时段可分析，周末也可评估真实数据，但经纪商 CFD 的行情和可交易时间须以它自己的新报价及下单检查为准。[波动与流动性周期性原始研究](https://arxiv.org/abs/2109.12142)

日内可以考虑趋势回撤、支撑阻力角色转换后的回踩、区间边缘扫回。交易所说明这些价格行为及日内费用/滑点，但不证明具体参数有收益。AI 的几十秒响应支持 M5/M15 日内决策，不适合捕捉几秒的价差。本项目对网格、套利、永续资金费率、清算追单尚无对应数据或执行能力，本轮不假装具备。[Kraken 日内交易说明](https://www.kraken.com/learn/what-is-crypto-day-trading)、[Binance 突破解释](https://www.binance.bh/en/academy/glossary/breakout)

BTC 参考趋势回撤、压缩后扩张回踩、假突破收回，目标上限 3R、最长 90 分钟。ETH 参考自身波动中的回踩及收回，目标上限 2.5R、最长 60 分钟。此处不同期限和目标是第一版待验证假设，不是从样本选出来的赢家；AI 可自主选择其他有真实证据的打法，仍受 H1 许可和风险约束。不能只因 BTC 上涨而追买 ETH，BTC 联动没有同步数据时明示未知。

两币历史上常有较高相关性，买两币不自动成为有效分散；本版不同时持有两币，也不把历史相关系数当当前常数。[Coinbase 历史相关性分析](https://www.coinbase.com/institutional/research-insights/research/monthly-outlook/monthly-outlook-june-2024)

风险设计将结构价格距离和现金损失分开：BTC 上限 min(2.5 M15 ATR, 参考价 1.5%)，ETH 上限 min(2.5 M15 ATR, 参考价 2%)；H1、RR1.5、最近三根 M5 极值加缓冲、EMA1.5ATR、实时止损触发侧 2buffer 保留。现金预算 min(8 USD, 8% × min(净值,100 USD))，按真实经纪商最小手数、步长和账户货币损益向下计算，最小手数也超预算就拒绝；不裁短结构止损凑单。MT5 的损益计算以当前交易环境及账户货币返回结果，不能假定一手 BTC/ETH 与黄金有相同点值。[MT5 合约规格](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py)、[损益估算文档](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)

## 公开行情诊断

冻结窗口：2026-10-04 05:08 UTC 至 2026-10-11 05:08 UTC（不含终点）。Coinbase Exchange 匿名公开 M1，BTC-USD/ETH-USD 各 10,080 根；仅完整连续桶聚合，得到各 2,015 根 M5、671 根 M15、167 根 H1。最新部分桶不使用。账户、broker、.env、MT5、AI 均不导入或连接。[数据接口文档](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)

源 CSV SHA256：BTC `3a0f0afffda42cf8b4be2091caf9a10a4e768b6196c8121ab6dc1533cd6a8b10`；ETH `3a595e4ef5ffba8df52ad9f95aa7920113ec76e5b1139cf5b22efaaf99929486`。原始数据及 probe.json 仅保存忽略目录 `.superpowers/sdd/crypto-data/`，不推送行情文件。

仅诊断单向 H1 许可、M15 EMA 距离、近三 M5 保护和 ATR/百分比距离的可行性，不建立交易或账户回放。评估点以最新完整 M5 收盘为准，使用下一根 M1 open 作假设中价，BTC 假设 spread 2bps、ETH 4bps；保护 buffer 为 .15ATR（现货源没有 broker tick_size，不能证明实际量化后可行）。目标仅取 R 上限，没有 AI/反向障碍确认。研究形态是自拟简化探针，不是生产固定入场门。

| 诊断 | BTC | ETH |
| --- | ---: | ---: |
| 有足够闭合源的分析点 | 1,706 | 1,706 |
| M15 ATR 中位数/价格 | 0.204% | 0.238% |
| 结构价格风控可行分析点 | 1,057 | 777 |
| 其中直接沿用黄金价格距离8会拒绝 | 1,057 | 107 |
| 保护距离中位数/价格 | 0.148% | 0.192% |
| 假设往返10bps手续费后净RR不足1.5 | 623 | 550 |
| 同 SL/TP 延迟60秒失去价格可行性 | 112/1,057 | 131/777 |
| 同 SL/TP 延迟120秒失去价格可行性 | 208/1,057 | 219/777 |
| 趋势回收研究形态次数 | 64 | 58 |
| 突破回踩研究形态次数 | 21 | 25 |
| 扫回研究形态次数 | 100 | 107 |

可行分析点不是有效信号、成交数或独立机会。各形态可能重叠，多次触及也可能属于同一行情；不能相加推断日成交频率。实际 AI1 质量/目标障碍、AI2 拒绝、CFD 点差/合约/保证金/费用/时效会继续减少它们。没有利润/回撤/胜率/资金可持续性结果。

这份诊断支持三件具体实施：取消黄金的绝对价格单位；保持固定 SL/TP 的报价复核，重试一定刷新；把费用/滑点同时计入现金风险及净 RR。10bps 只是敏感性假设，不是当前服务器 CFD 收费。未使用这七天寻找最佳参数，也不选择某一种形态作为唯一策略。

脚本：[crypto_market_probe.py](crypto_market_probe.py)，测试：[test_crypto_market_probe.py](test_crypto_market_probe.py)。四项离线回归验证完整桶、缺口、非法/重复输入、量级归一化及未来影线不进入已有检查点。重算命令：

```powershell
python docs/experiments/crypto_market_probe.py --cache-dir .superpowers/sdd/crypto-data
python -m unittest discover -s docs/experiments -p test_crypto_market_probe.py -q
```

CSV 是现货交易所事实，不能供 CFD 直接执行，也不能用探针数量来承诺每日多单。真实三AI频率、费用和损益只能从后续由用户启动的模拟日志验收。
