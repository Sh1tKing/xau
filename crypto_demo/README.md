# BTC / ETH 独立 CFD 模拟副本

黄金7f3df37框架的独立复制版本。先复制整个 `crypto_demo` 目录，Windows/Python3.12可用 `python -m pip install -r crypto_demo/requirements.txt` 安装本次验证依赖；经纪商合约仍须在自己的MT5模拟账户核对。唯一运行入口是 `run.py`，本轮没有启动交易、登录账户或付费请求。

```powershell
python crypto_demo/run.py --asset BTC --check
python crypto_demo/run.py --asset ETH --check
python crypto_demo/run.py --self-test
python crypto_demo/run.py --help
```

复制本目录 `.env.example` 为本目录 `.env`。填写独立USD模拟账户及精确合约名 `CRYPTO_BTC_SYMBOL` / `CRYPTO_ETH_SYMBOL`（含后缀，不猜合约），可填写 `CRYPTO_MT5_PATH` 指定终端。只读取副本配置，黄金配置不继承。AI主路的URL/模型/密钥、档位、超时、输出上限各自配置；备用空字段继承本角色主路。建议三角色主备全部配置Messages：中转 `/v1/messages`，DeepSeek官网 `/anthropic/v1/messages`。按URL路径解析协议，不按模型名猜测，失败不自动改走Chat；全部HTTP尝试共用最多5次预算，同通道读超时不重复补发。

用户自行启动的示例：

```powershell
python crypto_demo/run.py --asset BTC --mode observe
python crypto_demo/run.py --asset ETH --mode dry-run --symbol ETHUSD.your-suffix
python crypto_demo/run.py --asset BTC --mode demo --symbol BTCUSD.your-suffix
```

默认 `observe` 不请求AI、不下单、不修改或关闭持仓。`dry-run` 允许付费AI分析，仍不修改任何订单。只有显式 `demo` 可对USD模拟账户操作。建议与黄金用不同模拟账户/终端；黄金不参加本副本锁。一账号只能运行一个副本且最多一个账户持仓，手动仓/黄金仓/其他品种仓都会挡新入场。

飞书通知由本目录 `.env` 的 `FEISHU_NOTIFY_ENABLED=true` 开启，填写 `FEISHU_WEBHOOK_URL` 及机器人启用签名校验时的 `FEISHU_WEBHOOK_SECRET`。通知沿用启动、AI决策、API失败、开仓及平仓事件，标题标明当前合约；`observe` / `dry-run` 也可在明确开启通知时发送对应状态。未配置开关默认关闭，离线 `--check` / `--self-test` 不读取私有配置或发送通知。日志只显示开关和配置是否存在；HTTP成功后仍核对机器人业务返回码，错误/未知响应不报发送成功。参见[飞书自定义机器人文档](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。更新需替换 `run.py`、`crypto_trader.py`、`crypto_ai3.py` 及私有 `.env`，由用户重启生效。

BTC参考3R/90分钟，ETH参考2.5R/60分钟，两者冷却60秒；来自币种定义并冻结到原持仓。BTC观察自身趋势、压缩扩张和回踩回收；ETH观察自身证据、ATR归一化波动、失败突破、延伸和费用空间。菜单仅参考，AI-1自主选择有真实收盘证据的打法，本地no/低分或无固定形态不禁止分析。H1方向许可、M15风险、三根M5保护保留；ETH不能只因BTC上涨开多，联动无同期来源则未知。资金费率/OI/订单簿/清算/新闻未知，tick_volume仅经纪商代理。

8USD是估计现金亏损预算，不是币价距离；预算 `min(8,8%*min(实际正净值,100))`。BTC价格距离上限 `min(2.5*M15ATR,1.5%参考价)`，ETH为2%；近三根M5极值加 `max(.15ATR,2tick)` 缓冲，生成至少.6ATR；不能裁短SL凑预算。实时RR1.5、EMA21偏离1.5ATR、触发侧余量2buffer、价差.25ATR硬门保留。按真实合约 `order_calc_profit` 向下取手数，同时满足现金和90%可用保证金；最小手数不行就拒绝。tick精度、broker stops/freeze/filling和 `order_check` 校验。AI-2到提交SL/TP固定，新报价或规格变化可拒绝。

手续费配置是来回USD/手或来回bps，滑点是入场及退出各自不利tick预留；费用计入风险及净RR，净RR也至少1.5。零值表示未配置/未知，不是测得免费成交。bps估算要求合约的profit currency为USD，否则须使用USD/手费用或拒绝。无法保证实际滑点永不超过预留。

`CRYPTO_SERVER_UTC_OFFSET_HOURS` 明确配置原始tick/K线时间偏移（默认0），必须匹配实际经纪商时钟，夏令时变化由用户更新。程序不从旧tick猜偏移；换算UTC后quote必须在30秒内且非未来。周末不固定禁止，仍需新tick及完整闭合K线。

日志/策略状态在 `crypto_logs/<账号服务器哈希>/<asset>`。OS账号锁跨BTC/ETH及目录副本；不确定订单回执先写入稳定用户状态 `%LOCALAPPDATA%/MYQCryptoDemo/<哈希>/pending_order.json`（无LOCALAPPDATA时 `~/.local/state/MYQCryptoDemo`）。切换账户立即阻止请求/交易/护盘，不能清除原账户状态。模糊回执会在重启或换目录后继续挡两币入场，只有精确token/symbol/magic的持仓可自动核对；没有持仓不代表没成交。无法核实时需用户调查经纪商历史并有意处理状态，不猜最新ticket。

黄金旧盈亏换算回放已禁用。`crypto_backtest.py --csv <文件>` 只检查研究OHLC，不计算CFD/AI收益。公开现货行情不能冒充CFD可执行报价。离线mock只证明代码通路，不证明实际AI频率、费用、收益或盈利。

更新或迁移已运行的副本时，由用户停止旧实例，保留本目录的私有 `.env` 和 `crypto_logs` 原状态；换机器时也要保留前述账户待确认回执状态（存在时）。账号锁只协调同一机器和用户下的副本。原入场契约、证据及理由在送单前保存；持仓延迟可见或重启后只按精确原标识恢复，找不到原记录时保持未知，不能用当前策略补造旧仓规则。
