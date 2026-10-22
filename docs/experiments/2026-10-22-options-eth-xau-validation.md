# ETH/XAU 范围与退出提示验证

日期：2026-10-22。实现设计见 `docs/superpowers/specs/2026-10-22-options-eth-xau.md`；源提交及 ZIP 签名以交付 MANIFEST、`.sha256`、`.verification.json` 为准。

## 用户提供的服务器记录

仅解析用户粘贴文件，不连接服务器或其 SQLite。共 117 条控制台 JSON：BTC 为 already_scanned 1、WATCH 38；ETH 为 already_scanned 1、WATCH 35、failed 2、review_rejected 1；XAU 为 already_scanned 1、WATCH 22、failed 16。

ETH 的两次失败分别是回复字段缺失/多余和重复 JSON 字段。未提供这两次完整输入/回复，无法进一步确认哪一字段或替供应商补齐；不剪字段、猜引用或重新付费修卡。07:32:09 的 ETH 为 quoted 后 review_rejected，说明到过真实候选复核分支，控制台不含拒绝正文，不能断言这单原本应买。

XAU 的 16 次失败包含旧/未来 tick 4、最后闭合 30m 过期 2、近期 30m 缺口 4、1h 缺口 6。两次过期最后收盘均为北京时间 2026-10-22 04:59:59，观察年龄 3729.3/4645.8 秒，允许 1920 秒，均已重读 21 次、等待 20 秒。不能单凭这些记录确认经纪商休市、同步或偏移原因；不以增加模型 timeout 放过旧报价/缺口。最后三个标的均 WATCH，不等于整条链路故障。

## 离线回归

执行：

```cmd
python -X utf8 -m unittest discover -s options_analysis/tests -p "test_*.py" -q
```

源码 **257 项通过，18.507 秒**。原有 234 项继续执行，机会样例从 BTC 迁到 ETH，保留原拒绝条件和断言；只读 BTC 身份、历史、公开读取器及原账本兼容测试保留。新增 23 项覆盖：

- 默认/旧配置有效范围与私有文件字节保留；CLI、直接 run_asset/analyze、最终买入门均不能恢复 BTC 新机会；无人工 BTC 持仓为零采集/零扫描记录/零模型/零 GET/零通知。
- 已录入旧 BTC 持仓仅 GUARDIAN、无 ANALYST/REVIEWER/选合约，不关闭账本；ETH 和 XAU 均完成原两角色实际 wire-fixture 全链路，公开请求没有 BTC 专属指数或盘口。
- 1h/4h/12h/1d 比较时间逐一相同并进入引用；四根窗口的历史缺口不可由相同最后收盘掩盖；参考来源变化整组丢弃；参考失败不替换/阻断 ETH，原生异常正文不泄露；XAU 不采集 BTC。
- 原 XAU PUT 到目标场景分别说明未买入与已买入；CALL/PUT 的目标/失效镜像，新闻/来源变化不误称到目标/自动退出；无通知仍清理旧 idea；重复不重发，关联人工仓保持 open；真实合约精度完整、旧卡不猜合约。
- 固定计划距离、ATR/时间换算、阻挡顺序、CALL/PUT 镜像、最新同来源 Bid 重算及 source 变化拒绝；新增事实可引用、原计划与 ANALYST 回复 Schema 不变。

中间一次完整 252 项通过后，新增同来源新报价审计和进一步范围/参考隔离场景，最终 257 项重新完整通过。此前迁移前 BTC 机会样例按新范围返回 reference_only，测试随用户范围迁到 ETH，没有把这些失败改成放行 BTC 的理由。测试数据、行情、模型回复全部合成，不代表真实交易条件。

## 保留与交付核对

主备 timeout/输出上限/模型档位、1800 秒总研究、新 30m/新闻、90 秒报价、0.25 ATR 偏移、真实期权/基差/数量/权利金/成本门及 JSON/scope/证据严格校验保持。未读取账户或实际成交，不自动买入/卖出。

保护散列覆盖根、期权、crypto_demo、local_guard_demo 的四份私有配置及黄金三个脚本。实际 options runtime 只读计数：requests 9、scans 1、notices 0、positions 0。提交及交付时再核对，源码测试/demo 均使用隔离目录，不清理实际账本。未启动/重启进程，不请求真实 AI/币安/飞书/MT5，不推远程。

完整包从已提交源构建，排除 .env、runtime、Git 历史、日志、缓存/转写与部署包本身；检查实际私密值未混入。解压后逐文件 SHA256、模块导入来源、同一完整测试集及合成 ETH demo 再验证。解压验证的准确耗时、状态和保护散列见 ZIP 旁置 `.verification.json`，不能等同服务器已更新或全链联网通过。
