# 2026-10-21 期权研究改用 MT5：验证记录

用户明确更正使用 MT5 获取分析 K 线，不依赖 Binance。主分析、独立审核与飞书默认流程现为 MT5 标的方向研究，币安用于用户手动核对合约及交易，程序不请求币安。

## 代码与配置

- `mt5_market.py` 只连接已运行终端，公开行情及服务器命名空间，不调用 login、order_send、positions_get 或账户交易同步。不保存完整 account_info/登录号/余额，关闭仅断开 Python IPC。
- 原生七周期 shift=1/180根、至少100根预热；OHLC/时间/量字段校验、原始时间偏移、tick鲜活、短周期三根连续、原生高周期相位和历史缺口披露。tick量不冒充交易所量。
- `__main__.py` 仅选择 MT5Market；无币安实例、日程/期权链读取或行情失败回退。无期权报价仍可走 AI1→固定标的计划AI2→新报价→research通知；不调用定价、不虚构合约成本。
- 来源摘要、完整事实scope/短引用绑定、前后报价/原30m ATR偏移/触发/新闻/有效期保留。旧跨来源建议撤销、旧跨来源持仓不套价位且不阻塞新研究。
- 人工成交新增明确 side/strike/expiry/unit，标注用户提供、未经交易所核验；实际结算仅按真实录入与费用，AI3不估计未知期权浮盈。
- 只在期权私有.env增加 MT5 参数；BTCUSD/ETHUSD/XAUUSD，偏移3小时沿用本机既有crypto/local配置，未实测确认。模型和飞书连接字段逐项比较不变，私有文件不提交。

## 离线证据

执行：

```text
python -X utf8 -m unittest discover -s options_analysis/tests -p 'test_*.py' -q
Ran 81 tests
OK
```

包含前版52项回归及29项MT5/研究流程测试：

- 无终端、路径不符、多个终端、初始化前退出，全部不调用 initialize 或另起终端。
- 七周期原生采集、3小时换算、H4相位、未闭合柱剔除、历史缺口披露、最近缺口/重复/逆序/坏OHLC/不足预热/过期拒绝。
- 准确品种、不猜后缀、美元和基币身份、服务器身份、ETH/BTC同闭合窗口、采集中和后续来源变化。
- 真正 AIClient HTTP 契约对接离线模型传输，完整 MT5→AI1→AI2→刷新→模拟通知；测试禁止 Binance.get 和期权choose调用，仍得到 research。
- 主分析WAIT仅一POST且静默，AI2 REJECT结束，审核后价格变化拒绝；跨边界不POST，坏tick不伪装WAIT。
- tick/real量无损序列化，公共输入不含账户号/余额；API异常隐藏原始内容。
- 显式人工元数据/真实费用、AI3没有虚构当前盈亏、旧来源仓不迁移和通知去重、干跑不消费通知状态。
- CLI doctor 与 once 均只用注入MT5采集器，未调用币安；无通知模式不构造通知器。

合成完整演示：

```text
python -X utf8 -m options_analysis demo
演示流程结果：research
```

输出 `options_analysis/runtime_demo/demo_analysis.json`，synthetic明确为true、candidate为空、option_execution_verified=false，使用固定假模型；不是真实MT5、真实AI或当前市场意见。

## 本机真实检查与限制

已安装 MetaTrader5 5.0.5735，七个所需原生周期常量存在。本机没有运行的 terminal64。

实际执行 `python -X utf8 -m options_analysis doctor --mt5`，三个模型仅配置格式核对；三标的均提示“请手动打开指定MT5终端”，返回1。程序没有自动启动/登录MT5，没有付费AI或飞书。当前真实实时K线→AI→通知链路未验证，必须用户打开已配置终端后先通过doctor，再执行once禁发通知。

私有正式数据库检查仍为 requests=9、notices=0、scans=1、positions=0，全部请求为上一阶段，当前没有新增付费请求、真实通知或持仓；不会把旧 Binance 失败历史误称为新 MT5 执行。

本次没有启动 run、模拟/实盘进程、后台服务，没有修改或重启黄金/crypto/local交易，没有部署或推送。`.env`、`crypto_demo/.env`、`local_guard_demo/.env`、两个黄金主脚本的SHA256须与开工记录相同；交付前另做源语法、差异和敏感值扫描。离线通过不证明模型遵约率、真实期权成本、收益或可执行性。
