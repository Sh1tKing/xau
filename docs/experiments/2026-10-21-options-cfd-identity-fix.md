# 2026-10-21 MT5 USD 基币规格兼容修复

## 实际错误与规格

用户服务器于北京时间19:43:52–19:43:54依次报告BTC、ETH、XAU基币不符。随后在同一终端只读查询确认：

| 配置名与API返回名 | description | currency_base | currency_profit |
| --- | --- | --- | --- |
| BTCUSD | Bitcoin VS USD | USD | USD |
| ETHUSD | Ethereum VS USD | USD | USD |
| XAUUSD | Gold vs US Dollar / Spot | USD | USD |

根因是代码只将 currency_base 与 BTC/XBT、ETH、XAU/GOLD 比较，忽略了这台服务器的实际规格约定，不是用户把三种标的都选成USD。不能据此断言全部经纪商都采用此约定。

## 修复

`options_analysis/mt5_market.py` 的纯身份辅助函数保留原生基币匹配分支及其来源结构。仅当 base=profit=USD 时，增加 `mt5-cfd-identity-v1` 联合规则：

- API name 必须与明确配置完全一致，不查找或替换品种。
- 名称必须完整对应 BTC/XBT、ETH、XAU/GOLD 兑USD，可有标点分隔的明确后缀；BTCUSDT、BTCUSDJPY、WBTCUSD及BTCUPUSD等不能仅凭包含BTC通过。
- 完整 description 必须为对应资产兑USD/US Dollar，可有明确 Spot/CFD/Cash 后缀。不接受反向、其他报价、Bitcoin Cash/Wrapped Bitcoin/ETF、空或模糊描述。
- 缺失/未知基币、其他资产基币、非USD利润货币不能借此兼容。原生基币及USDT原路径不变。
- 保存原始USD字段和实际联合依据，不伪造 currency_base=BTC/ETH/XAU。依据进入 source_identity/source_id；采集中或后续有效描述变化也失效旧快照，沿用原scope/引用/刷新门。

不依赖品种路径分类，不按价格猜资产，不新增跳过校验开关，不修改实际配置、模型、预算、K线时间处理或新闻规则。

## 验证

先用用户三组公开规格、合成报价复现旧版错误。新回归未修复时运行两项测试，三资产采集都报原错误，完整研究返回failed（3 errors、1 failure）。

修复后系统Python执行：

```text
python -X utf8 -m unittest discover -s options_analysis/tests -p 'test_*.py' -q
Ran 93 tests in 6.000s
OK
```

新增12项覆盖三真实规格的七周期采集/同源刷新、空/冲突基币、错误/反向/包装描述、返回名与配置不一致、其他资产/报价名称、非USD报价、准确标点后缀、原生来源摘要不变、采集及刷新过程中描述变化、完整BTC模拟AI1→AI2→通知和错配时0模型POST。

以上报价、AI和通知为离线fixture，不代表服务器真实行情或模型已通过。本轮不连接终端、不发真实AI/飞书、不启动扫描或交易。原实际服务器错误说明进程与通知当时可运行，但不证明完整研究链路已通。

## 交付

本地master保存后生成仅含一个运行文件的补丁ZIP，附源提交、替换前后文件SHA256和CMD说明，不包含.env/runtime或Git历史。补丁用于原c5bf386源码包；交付前须在原包解压副本上覆盖该文件，验证独立导入及完整离线测试，具体产物与哈希以其清单为准。

服务器用户先Ctrl+C、覆盖根目录下options_analysis/mt5_market.py，再从根目录执行doctor --mt5，真实验证通过后run。预算、通知去重、人工账本与已消费扫描槽保持；不删库重跑同槽。新错误如时间偏移或缺历史须按具体输出处理，不能关闭检查。步骤见options_analysis/UPDATE_CFD_FIX_CMD.md。
