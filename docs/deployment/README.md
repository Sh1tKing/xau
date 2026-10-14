# 当前默认策略：亚欧美三盘自主AI粗筛（模拟账户）

黄金新闻休息默认启用：美国非农、CPI/PPI、GDP/PCE及FOMC决议、发布会、纪要前后各15分钟暂停新开仓，已持仓继续AI-3和本地锁盈。完整复制两个主脚本、`strategies/news_calendar.py`、`strategies/calendars/us_major_events.json`及其余strategies文件并自行重启。实际.env保留即可采用默认规则，无需改模型/超时。窗口相接会合并，结束后重新获取行情，不排队执行旧单。

日历后台刷新，BLS、BEA、美联储为正式日程来源，BLS失败可用Forex Factory公开周历；启动备用及缓存最长核对有效期7天。当前开发机BLS返回403、公开周历超时、BEA/Fed可访问，这不是服务器网络测试。不能把加载备用称为所有来源实时联通；超过有效期且仍取不到必要来源会明确记录“新闻日历不可用”并暂停新入场，护盘继续。观察`news-pause-v1`日志与`xau_fib_pa_logs/news_calendar.json`；`python -m strategies.news_calendar`仅检查，`--refresh`主动取公开日历并写缓存，均不连接MT5/AI；命令读取当前进程环境变量及默认配置。不要删除服务器已核对的日历缓存来迁移。详见[设计](../superpowers/specs/2026-10-14-news-entry-pause.md)。

默认启用`asia_adaptive`、`london_adaptive`、`us_adaptive`，旧固定/区域定义保留但默认禁用。新三盘本地只粗筛，AI-1自主选择打法；本地no、低分、没有首次回踩或不在旧开盘窗口都不阻止正常分析。详见[设计](../superpowers/specs/2026-10-10-adaptive-session-demo.md)。本机`server_deploy/`是完整同步包，更新时必须同时替换两个交易主脚本与完整`strategies/`包，包含`adaptive.py`、`framework.py`、`events.py`、`regions.py`、`patterns.py`及`lifecycle.py`。保留服务器自己的实际`.env`、事件ledger与AI3入场上下文，用户自行重启，只有一个实例。

新三盘仅模拟账户，参考3R目标上限、90分钟持仓复核、60秒平仓冷却；北京时间亚盘07–15、欧盘15–20、美盘20–次日07，全时段覆盖，未把标签当作精确交易所/DST开盘钟。AI质量与实时风险门仍执行。先部署新辅助模块并重启，此后定义文件才支持下次评估自动加载。2026-10-14的holding-review-v1适用于新旧自主仓：保留原入场时钟与节点数值，到期进入常规AI-3复核，时间本身不强平；不重建其余契约/锁盈状态。历史非自主事件仍按原硬期限。辅助模块或实际`.env`改变仍需自行重启，旧仓时钟及冷却不随新定义改变。

收盘调度继续核对经纪商与本机时间，不把尚未收盘的数据放行。启动、新M15及空闲的新闭合M5可发起AI分析，同检查点去重；没有请求时日志必须写真实本地阻断原因，不能把它叫AI观望。AI1请求后的no、契约错、Python风险拒绝、AI2拒绝/请求失败分别记录。新增M5分析比旧开盘窗口模式更频繁，费用以真实usage和账单为准，单轮预算/生成上限和读超时不重复补发保持。

必须完整复制两个主脚本和 `strategies/`，安装requirements并由用户重启一次。实际`.env`的门槛、模型、档位、超时和协议不因本次策略更改。新模式AI以成功请求报价为参考，AI2/执行前再次刷新；H1方向、SL≤8、RR≥1.5、M5极值保护、触发侧2倍缓冲余量和EMA追价限制继续检查。检查点不是AI批准信号，关闭AI时粗筛模式不直接下单。

账户不是MT5模拟类型或账户类型未知时，程序拒绝新研究入场。已有仓仍保留原版本入场上下文。迁移同一账户需要保留 `xau_fib_pa_logs/strategy_event_state.json` 和AI3持仓上下文；不要删除事件状态以追同一窗口。实际.env模型、档位、门槛、超时和Messages无需因本次策略接入更改。只启动一个实例。

# 三AI黄金策略服务器部署

此部署包的代码提交以 `MANIFEST.json` 为准，证据版本 `entry-evidence-v5`，评分版本 `entry-quality-v4`，行情刷新版本 `request-freshness-v2`，JSON契约版本 `response-contract-v3`。

2026-10-14的`ai1-issue-first-v1`调整AI-1的Schema、模板与rubric，先输出唯一意见清单，再按独立证据评五项，避免同一反向事件同时撑触发和反证两项扣分；仍是v4，重复坏卡继续拒绝。同时修正“报告全部输出为思考，却有可见正文”的计量诊断，正文token显示未知。完整覆盖AI脚本及strategies后自行重启，保留服务器最新.env与状态；本次未改模型、档位、超时或上限。若自行将AI-1上限调至6144，费用按实际用量计，长请求可能增加费用与等待。详见[验证与费用分析](../experiments/2026-10-14-ai1-issue-first-validation.md)。

2026-10-14的`ai3-cache-prefix-v1`仅调整AI-3请求数据顺序：原入场契约及完整双向证据放在前面，动态观察/报价/K线放在后面，便于DeepSeek自动复用相同前缀。所有数据值、每分钟AI复核及每0.5秒本地锁盈保持，实际`.env`、模型、档位、上限和超时不改。用户更新部署清单内代码并自行重启后，启动日志及`ai_usage`应显示输入版本`ai3-cache-prefix-v1`，当前代码签名以MANIFEST和启动输出为准；比较新轮次的`cache_read_tokens/input_tokens`与账单验证效果。缓存为供应商尽力服务，首轮或原持仓契约改变时仍可能未命中，不能承诺固定节省比例。详见[护盘费用分析与验证](../experiments/2026-10-14-ai3-cost-review.md)。

2026-10-14的`close-settlement-v1`修复平仓通知结算：按稳定持仓标识查询全部真实成交，汇总开仓和平仓的利润/佣金/库存费/手续费，分批成交价格按成交量加权，不用最后浮盈代替结果，也不借同品种邻单。历史回执未齐时普通轮询继续核对，最终通知删除“未知（未查到历史成交）”、最后浮盈及缺失评分。待结算通知新增`close_notification_state.json`，默认在`xau_fib_pa_logs`、跟随guardian状态目录；迁移同一账户时连同原三个状态文件保存，用户不要用本机旧状态覆盖。更新两个主脚本和包含`settlement.py`的完整strategies后自行重启，启动应显示平仓结算版本，当前签名`4cc1c2c83cc0a4c7`。本次保持实际.env和所有交易/护盘规则。详见[结算说明与验证](../experiments/2026-10-14-close-settlement-review.md)。

运行入口是 `xauusd_fib_volume_pa_ai3.py`，它会导入同目录的 `xauusd_fib_volume_pa_trader.py` 和完整 `strategies/` 包。

2026-10-14评分显示修订：覆盖两个黄金主脚本并自行重启后，开仓及DRY-RUN分别显示所选方案的AI-1质量分和本地BUY/SELL粗筛分；纯本地入口只显示本地粗筛分。平仓通知使用相同标签，当前结算版省略缺失的开仓评分。自主模式的审核通知明确本地阈值仅供参考。本次仅修显示，保留服务器自己的`.env`、日志与持仓状态文件。

若从GitHub源码部署：将这两个脚本、完整 `strategies/` 文件夹及 `docs/deployment` 下的 `requirements.txt`、`install.cmd`、`start.cmd` 放在同一目录，再将 `.env.example` 复制为 `.env` 并填写自己的账户及接口配置。公开仓库不包含私人 `server_deploy` 目录、实际 `.env`、日志或持仓状态。下述“部署包”指本机生成的私人目录。

## 服务器环境

- Windows 64位服务器，安装 Python 3.12 64位（包含 Python Launcher，即 `py` 命令）。依赖版本与打包机器一致；不是Linux原生部署包。
- 安装经纪商的 MetaTrader 5 桌面终端，在运行脚本的同一Windows用户下打开终端，确认服务器、账户和交易品种可用，并允许外部Python自动交易。
- 能访问经纪商、配置中的AI接口及飞书接口。首次安装依赖需要能访问Python包下载源。

## 部署步骤

1. 按 `MANIFEST.json` 的文件清单，将三个黄金脚本、完整 `strategies/`、文档及安装/启动文件复制到服务器固定目录，例如 `C:\MYQ`，保持目录结构。更新已有服务器时保留它自己的`.env`和`xau_fib_pa_logs/`；本机目录中已有的调试日志、缓存与状态不属于本次代码更新清单。
2. 检查服务器实际 `.env` 中的账户、经纪商服务器、品种、AI接口和交易开关。若迁移到新机器，私下复制核对后的配置，并在原进程停止后迁移最新事件ledger与AI3持仓上下文；不能用本机旧状态覆盖正在运行的服务器状态。
3. 双击 `install.cmd`，安装依赖到该文件夹的 `.venv`。安装脚本不会启动交易。
4. 打开MT5终端。迁移同一账户前，先在原机器停止旧交易脚本，避免两台机器同时管理同一账户。
5. 双击 `start.cmd` 启动三AI策略。该操作会按 `.env` 的交易开关运行，可能实际下单；只运行一个实例。
6. 查看启动输出：应显示 `adaptive-session-v1`、三个启用的adaptive名称和策略指纹，并说明本地分不是新模式AI派发门槛；证据版本为 `entry-evidence-v5`、质量评分为 `entry-quality-v3`、行情刷新为 `request-freshness-v2`。AI门槛以实际 `.env` 为准，AI分比较五项质量总分（非胜率）；旧固定模式仍使用本地门槛。日志写入 `xau_fib_pa_logs`，跨日后第一条记录会按服务器本地日期创建对应日期日志；每次AI结果可查看质量明细、状态与观望依据。更新后需自行重启加载，旧进程不会自动换代码或配置。

## 2026-10-13 分级利润保护

本次完整覆盖两个黄金主脚本和`strategies/`（含新增`profit_guard.py`），部署清单/代码签名以MANIFEST为准。保留服务器自己的.env和最新状态，不用本机同步下来的旧状态覆盖服务器；原进程需由用户重启，保持单实例。辅助模块不能像策略定义一样热加载。

未配置`FIB_PA_GUARDIAN_POLICY`时，新订单默认`profit_floor_v1`。现有仓没有冻结新标记时继续旧护盘。新订单参数在入场时冻结，修改.env只影响之后的新订单；显式`legacy`同样只影响之后的新订单。实际.env没有被这次更新修改，轮询仍使用`FIB_PA_POLL_SECONDS`（当前0.5秒）。

持续1秒的新有利报价才提高预计利润保护；初始净保护至少0.20账户币种，初始30%，达到原风险1R/2R后40%/50%，没有必须先到1R的门。仍须留出M5 ATR、点差及broker最小距离的回撤空间，因此刚有微小浮盈时可能尚不能设置利润止损。已确认线不可回退；AI3 hold保留它，tighten必须更紧且有足够空间。原SL/TP继续执行；自主仓到时间节点只复核，AI-3失败也不因时间平仓，锁盈仍继续。策略性退出交给AI3，旧盈利前虚拟退出/本地结构退出不再套用到新profit-floor仓。

迁移同一账户时，在旧进程停止后保留最新`strategy_event_state.json`、`ai3_position_state.json`及新增`guardian_profit_state.json`。新锁盈状态默认与事件ledger同目录，也可用`FIB_PA_GUARDIAN_STATE_FILE`指定；不要删除它以重建水位。状态损坏/写入失败阻止新锁盈入场，现有服务器保护仍保留。日志`锁盈确认`分别记录保护线、预计净金额与费用状态；费用预留、滑点及止损成交不保证实际净收益等于预计值。

离线工程检查与固定缓存控制见[验证记录](../experiments/2026-10-13-profit-floor-validation.md)。纯本地对照没有AI3且结果较差，不能用它证明该组合收益改善；下一步应观察新的模拟仓日志，不按本轮缓存调整默认参数。

2026-10-13报价修订`guardian-quote-clock-v2`修复同步日志中服务器比本机快约2～3秒导致秒级锁盈持续跳过的问题：持仓报价容许最多5秒有限时钟领先，最老报价仍5秒，原UTC偏移固定，入场闭合K线时间检查不变。轮询、修改SL和平仓使用同一检查。跳过日志显示真实原因、时间差及服务器SL/确认线/目标；相同原因30秒内只打印一次，检查仍按原轮询频率进行。AI3保护证据显示broker_sl和quote_status，模型文字不能代替真实修改成功回执。详见[修复验证](../experiments/2026-10-13-profit-guard-clock-review.md)。覆盖两个主脚本并保留完整strategies及服务器最新状态，自行重启即可继续原profit-floor持仓，无需删除状态或修改.env。

## 2026-10-12 评分契约v3更新

同步AI脚本及完整strategies包，包含新增 `quality.py`；普通量/浅回撤使用observations，重大问题使用reservations，提示词、Schema与Python共享同一等级关联规则。有效旧卡按原版本处理，坏卡不改分、不补发审核；错误通知显示实际路径/cause/level，并可附独立风险诊断。详情见 [验证记录](../experiments/2026-10-12-ai1-quality-contract-v3.md)。如需运行离线Schema回归，可安装 `requirements-test.txt`；正常交易仅需 `requirements.txt`，不依赖jsonschema。

17:36日志后的兼容修订：未确认反转的单根M5反向等轻微观察允许使用对应原因主题，不再只允许量能/浅回撤。重大反证仍使用reservations，hard_limit/missing_data禁止放observations；等级、分数及Python硬风险门保持。评分标签仍为entry-quality-v3，请核对MANIFEST的代码签名确认本次构建；必须覆盖AI脚本及完整strategies后自行重启，保留服务器自己的.env、日志与持仓状态。

## 2026-10-09 入场可靠性更新

本次另修复止损舍入：启动日志新增 `止损价格=protective-stop-v1`。生成止损、AI定稿和M5保护价统一保持两位小数，BUY向下、SELL向上，避免四舍五入进入保护缓冲；取整后超过8美元仍拒绝。日志区分真正超限与无法生成，不再将覆盖失败一律称为超过8美元。该缺陷与本次15轮连续触发EMA追价限制分开处理；EMA1.5ATR、RR1.5和运行配置保持。更新须同时替换两个主脚本，自行重启，保留服务器自己的 `.env`。

评分卡将保留意见嵌入唯一所属维度，Python按模型声明的等级生成ID、归属与程度，避免重复填写产生矛盾。真实证据引用、同一原因去重、已有触发与阻断条件仍严格校验；坏卡不会自动修成交易，也不会付费重审。日志的 `status` 区分 `contract_invalid`、`watch`、`python_rejected`、`review_pending`，契约错误通知明确为输出无效。

0.6ATR/0.8美元继续用于M15参考价下生成止损；实时成交检查改为止损触发侧BUY Bid/SELL Ask至少剩余 `2*stop_buffer(atr)`（现有参数下1.2–2美元），并考虑点差、止损8美元上限、RR1.5、EMA21追价及旧K线过期。价格向有利方向变化不再仅因低于生成宽度而拒绝，贴近止损仍拒绝。AI-2与实际下单使用同一检查。详见入场可靠性v2设计；本次不修改实际配置和本地策略。

2026-10-09协议迁移时，私人部署包的AI-1/2/3主路与已填写的备用均统一为Messages：中转站使用 `https://hapiopen.cc/v1/messages`，DeepSeek官网使用 `https://api.deepseek.com/anthropic/v1/messages`。空白备用项仍继承主路，不新增通道。模型、密钥、档位、超时和交易门槛保留各自配置。本次三盘策略更新没有改实际 `.env`；同步时保留服务器当前配置，旧进程需自行重启。

Messages统一使用顶层system、user消息、`anthropic-version`、`x-api-key`（保留中转站要求的Bearer认证）、`max_tokens`及兼容的`output_config.effort`；显式关闭AI-3扩展思考时省略effort。DeepSeek官方的output_config支持effort、不支持format，因此首发直接使用提示词JSON并记录 `json_mode_sent=prompt`，Python仍严格校验JSON和策略契约；其它路线继续优先Schema，明确不支持才降级。历史Chat兼容分支保留，但本包配置没有主备路线走Chat。截断拒绝、5次总预算和每次POST刷新行情保持。详见 [Messages迁移记录](../superpowers/specs/2026-10-09-messages-route-migration.md)。

退出脚本使用控制台 `Ctrl+C`。此包是前台运行方式，没有注册Windows服务或开机任务。关闭控制台、注销Windows用户或服务器重启会中断运行，需自行重新启动。

## AI备用接口

`.env`已预留三套备用配置。将编号替换为1、2、3即可分别配置对应角色：

```dotenv
AI1_BACKUP_URL=
AI1_BACKUP_KEY=
FIB_PA_AI1_BACKUP_MODEL=
FIB_PA_AI1_BACKUP_EFFORT=
FIB_PA_AI1_BACKUP_TIMEOUT=
```

空白或未写的项逐项继承对应主接口的URL、密钥、模型、推理档位和超时。备用超时须为正整数秒，空白或无效值沿用主路；单独填写超时不会创建新备用通道。全部留空时仍使用原配置。备用中转站可能需要填写它自己的密钥和支持的模型名。

2026-10-08完整AI-2审核复放发现：Grok主路约85.7秒才返回，DeepSeek V4 Pro的`medium`实际映射为`high`，一轮4096输出token全部用于思考、没有最终JSON。仅提高超时仍可能得到截断。当前AI-2主路读取等待改为90秒，已配置的DeepSeek备用改用`low`并独立等待40秒：

```dotenv
FIB_PA_AI2_TIMEOUT=90
FIB_PA_AI2_BACKUP_EFFORT=low
FIB_PA_AI2_BACKUP_TIMEOUT=40
```

这是该部署配置的修复值，不强制改写所有供应商的思考档位。没有独立备用时不要仅填一个不同effort来虚构备用。完整快照本机单次对照中，DeepSeek `low`约13.8秒返回完整审核，修复后的call_chat复核约19.8秒也返回完整审核；Grok主路90秒仍可能超时，因此修复验收关注自动备用接手，不能保证主路不再超时或服务器长期时延。AI-1与AI-2共享360秒接管期限不变，过期回复仍丢弃，正常REJECT与截断均不换模型重审。

每次分析先请求主接口，主接口发生超时、连接失败、HTTP错误、非JSON响应或可见输出为空时切换独立备用。主备以及参数兼容恢复合计最多请求 `FIB_PA_AI_HTTP_RETRIES` 次（默认5）。读超时后，上游可能仍在生成并计费，因此默认不向同一通道重复补发；有独立备用则切换一次，两边都读超时就结束该轮。连接失败/连接超时仍可在总预算内重试，网络重试间隔2秒。正常观望/REJECT不换模型重审。启动日志显示备用是否独立；切换日志显示primary/backup、模型和总请求序号，不显示密钥。两条通道都不可用时开仓仍会拒绝，AI-3仍由本地护盘和原服务器止损保护。

编辑配置后需自行重启进程才能加载。将更新后的脚本复制到服务器，同时将这些备用配置项加入服务器实际使用的 `.env`，保留服务器自己的主接口、账户及其他参数。

## AI用量与成本控制

启动日志应显示 `AI成本控制=cost-latency-v2`，备用配置行同时显示主路及备用等待秒数。关键证据、完整K线窗口、实际配置的质量门槛和Python风控保留；输入只移除与已序列化OHLC计算结果一致的重复candle_parts、完全重复的元数据和AI-2无需重评的评分卡，来源精度更高的影线数值仍保留。规则与评分标准提前排列以便缓存。三角色要求简短JSON解释，不复述全部行情。

```dotenv
FIB_PA_AI_READ_TIMEOUT_RETRIES=0
FIB_PA_AI1_MAX_TOKENS=4096
FIB_PA_AI2_MAX_TOKENS=4096
FIB_PA_AI3_MAX_TOKENS=2048
```

`READ_TIMEOUT_RETRIES`为每通道读超时后的额外补发次数，0代表不重复同一慢请求；仍受HTTP总预算限制。减少重复请求意味着主线路较慢且没有备用时，本轮会较早结束，不再靠第2至5次同线路补发等回复。备用项留空仍没有新服务可用。

当前Messages路线无论模型是Claude、GPT、Grok还是DeepSeek，都发送 `max_tokens`，兼容时发送`output_config.effort`，不因GPT模型名改用Chat字段；AI-3的显式`thinking=disabled`不同时发送effort。接口拒绝必填上限时切独立备用或结束，不取消上限。可选effort或Schema明确不兼容时仍可按原预算恢复，并在进程内记住结果。历史Chat兼容分支仍保留自己的字段转换。中转如何映射思考参数及计费需要看实际usage，不能仅凭HTTP200认定档位生效。

## 2026-10-12 AI-3思考开关与截断诊断

黄金版请求层同步`xau-ai3-thinking-v1`。`low`是档位，关闭扩展思考使用独立开关。实际`.env`由用户维护，本次没有改模型、档位、输出上限、超时或交易门槛；需要关闭时在运行目录的`.env`填写：

```dotenv
FIB_PA_AI3_THINKING=disabled
FIB_PA_AI3_BACKUP_THINKING=
```

第一项只作用于AI-3，空白或未配置保留接口默认行为；备用空白继承主路。若只关闭备用的扩展思考，主路留空、备用填`disabled`。不支持`low`、`enabled`等开关值，不按模型名自动关闭。开关本身不会创建新的备用通道。Messages/Chat按原URL分别发送顶层`thinking={"type":"disabled"}`，省略effort但保留输出上限和JSON契约；供应商需支持此参数。接口明确拒绝时只允许已有独立备用接手；没有可用备用则本轮失败，本地护盘继续，不移除开关后偷偷重发。

请求准备日志区分配置档位`requested_effort`、实际发送`effort`、`thinking`和`max_tokens`，另记录刷新行情耗时。`ai_usage`新增`reasoning_effort_requested`、`thinking_mode_sent`、`visible_text_chars`、`thinking_blocks`等字段，仅记录思考块数量，不保存私有思考正文。未提供reasoning用量时仍为未知，不能当作0。截断通知显示实际发送上限/档位/开关、报告输出和可见正文长度；仍拒绝不完整决策，不增加付费重审。输入token不占本次输出上限，客户端也不能保证中转实际执行参数或撤回已发生费用。

本机`server_deploy`此前混有币种代码与说明。本次黄金更新以`MANIFEST.json`中黄金文件清单为准，入口仍是`xauusd_fib_volume_pa_ai3.py`，同级策略及辅助模块与根目录黄金版本一致。遗留币种文件不在黄金清单中，不用于黄金启动；独立币种版继续使用`crypto_demo/`。复制清单中的代码后由用户自行重启，保留服务器的`.env`、日志和持仓状态。

回复结束原因是 `length`/`max_tokens` 时视为截断，即使可解析JSON也不执行、不另换模型重审。日志会提示调整对应角色上限；上限在部分模型上包含思考token，因此不要仅按可见JSON长度继续压低。

每次实际HTTP请求均写入 `xau_fib_pa_logs/ai_usage/YYYY-MM-DD.jsonl`，包括角色、call_id、decision_id、trigger、主备、请求/回复ID、耗时、上限、发送档位及输入/输出/思考/缓存读写token。v2另记`read_timeout_sec`、`headers_elapsed_sec`和`body_complete`；收到响应头后正文超时仍保留实际HTTP状态和请求ID，不把已连接误写成完全不可达。未知字段为null，读超时费用未知，绝不记作免费；缓存未命中与缓存写入分开，不重复累计provider已含缓存的输入。美元费用留空，实际费用以中转账单为准。迟到的过期回复也记用量后丢弃，不下单。

`trigger=startup/M15/M5/M1`分别表示启动立即评估、M15主评估、已收盘M5复核、持仓M1护盘；新三盘的M5复核不要求本地分过线，旧固定模式保留本地门槛。同call_id的不同attempt是重试，AI-2沿用本轮入场decision_id。AI-2请求快照另记 `ai2_input`，便于与账单和最终方案核对。以上用量日志不包含密钥。

## 文件说明

2026-10-12集中请求更新 `ai-review-reliability-v1`：AI-2也支持独立思考开关。本机部署实际.env新增`FIB_PA_AI2_THINKING=disabled`和`FIB_PA_AI2_BACKUP_THINKING=disabled`，并将`FIB_PA_AI2_BACKUP_TIMEOUT=`留空，使备用继承主路120秒，而非旧40秒。若服务器已有自己的新配置，只合并这三个字段，其余账户、模型、密钥和门槛保留。disabled会省略实际effort，不把low当作关闭思考；接口明确拒绝时不偷偷删开关。上游报告输出超过发送上限即结束本轮并暂停该配置的后续付费请求，下一轮可用已有健康备用，两路都违规则不发HTTP，本地护盘继续。暂停仅存当前进程，核查上游后通过明确配置变化或用户重启恢复。启动应显示新可靠性版本、AI-2主备disabled、备用等待120秒及加载时代码签名；API失败/契约通知也附版本。详见[诊断及离线验证](../experiments/2026-10-12-ai2-review-reliability.md)。

2026-10-12黄金AI-1引用更新 `ai1-citations-v1`：每次请求发送可引用证据目录，模型复制短ID，Python按成功请求快照解析并严格核验候选隔离；维度/保留意见/等待依据使用同一目录和动态Schema。真实共享EMA/风险字段不再误拒绝，错误旧Pointer不会被猜测修补成交易。启动日志应出现`AI-1证据引用=ai1-citations-v1`。更新必须覆盖AI脚本及完整`strategies/`（含`citations.py`），只换主脚本会缺少依赖；覆盖后由用户重启，保留服务器自己的.env及日志状态。详细诊断和请求长度影响见[验证记录](../experiments/2026-10-12-ai1-citation-contract-validation.md)。

- 两个 `.py`：三AI与本地策略/交易/护盘模块。
- `xauusd_fib_pa_backtest.py`：另附的离线回放入口；不参与`start.cmd`启动。`--offline`仅读已有缓存，普通模式会连接MT5读取历史。
- `strategies/`：必需的框架、公共证据工具和策略定义，保留文件夹结构。更新策略只改 `definitions/*.py`；部署新框架第一次仍需自行重启。参见策略文件说明。
- `.env`：当前实际配置，含密钥和账户信息，仅私下传输保存，不提交Git或公开上传。
- `requirements.txt`：交易依赖及Windows IANA时区数据；本次DST回归使用tzdata 2023.3。
- `install.cmd` / `start.cmd`：安装与启动入口。
- `MANIFEST.json`：打包时间、源代码版本、脚本SHA256，便于检查是否复制完整。

不需要复制历史日志、回测结果、其他交易脚本、`.env.example` 或本机虚拟环境。
本次打包时未发现 `xau_fib_pa_logs/ai3_position_state.json`，因此未包含持仓理由状态。如果正式迁移时已有该文件，应在旧脚本完全停止后，将最新文件复制到新机器同名目录；缺少入场历史时AI-3会将相关上下文标记为未知。

本地已核对脚本复制一致和依赖元数据；服务器安装、MT5连接、AI连通性与交易执行需要在目标机器确认。本次打包没有启动或重启任何交易进程。
