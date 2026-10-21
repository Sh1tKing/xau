# 期权 BTC 可见回复格式兼容与引用容量修复

日期：2026-10-21。范围：独立 options_analysis；MT5 方向分析、公开选合约、人工交易流程不变。

## 用户证据和结论

用户从服务器 SQLite 只读导出 BTC/ANALYST 失败记录，模型均为 claude-sonnet-5-5：

- created=1792592102.1720233，北京时间22:15:02请求、22:15:38失败通知。完整裸 JSON，action=WATCH、direction=CALL、primary_timeframe=4h。support_refs 为 r18/r301/r303/r304/r307/r308/r309/r312/r117/r192/r193/r510 共12条，旧上限8，故在数组容量校验处被拒。
- created=1792590302.1242049，北京时间21:45:02请求。完整 JSON 外包一层 json代码框，action=WATCH、direction=CALL。它不是21:30那轮记录，最初的时间对应已纠正。
- 用户补查21:30时段后提供 created=1792589402.1259153，北京时间21:30:02请求，对应21:30:31通知。该可见回复同样是完整 JSON 外包一层json框，action=WATCH、direction=CALL，各8条support_refs/counter_refs，计划数字字段为null，没有被截断。

三条均是等待观察，不是已经获准购买。用户未提供完整 input_json，尚不能核实原始每个引用的语义或判断是否通过后续全部时效/交易条件。

## 实现

- `ai.py`：`options-ai-json-v2`。主/复核/持仓引用分别1–16个，足够容纳已观察到的12条多周期证据；每条仍必须来自原scope的真实目录枚举。保留全部引用，不截成八条，也不猜或补写引用。其他数组限制不变。
- 模型提示明确单个裸JSON、所有引用数量和无空数组规则。供应商structured开关保持现有配置，不假定网关支持或已强制遵约。
- `decode_ai_reply`只允许整个可见回复恰好是一份JSON，或其外面仅有一层json/无语言代码框及空白。剥离外框后由原严格解析器和完整Schema校验，不搜索正文、拼接多对象、二次解码字符串、补括号或修语法。HTTP接口信封仍直接严格解析，不开放代码框。
- `core.py`不放宽JSON，新增安全语法行列/重复字段/非有限值分类和数组实际数量/上下限诊断，不在异常中回显正文。位置指解析的JSON文档；若存在外框则相对于框内文档。
- `store.py`增加可空response_meta_json列，后续请求记录协议、实际structured_requested、结束原因、解析阶段、原始可见文本及错误分类。只保存文本块，不保存思考、密钥或HTTP头；旧状态不补写、不重跑旧scope或清零预算。
- WAIT/WATCH/REJECT与业务失败的原处理、主备次数和每日预算不变。格式合法仍需通过原scope/目录、类型/枚举、时效、新K线、来源、方向及成本门。

## 验证

先新增三项回归并在旧实现运行：12引用、完整单框均ERROR，数量诊断FAIL，重现已知错误形状。修复后同三项通过。

最终命令：

```cmd
python -X utf8 -m unittest discover -s options_analysis/tests -p "test_*.py" -q
```

结果：144项通过，10.212秒（原127项+新17项）。新测试使用合成行情和本次真实目录，不把用户未提供的旧输入伪造成已核对事实，覆盖：

- 12/16条完整保留，0/17/未知引用和旧scope拒绝；三个角色及wire/schema一致。
- 裸JSON、单个json/无语言框、CRLF；前后说明、多个对象/代码框、未知语言、截断、尾逗号、重复嵌套字段、非有限数值及JSON字符串二次解码均拒绝。
- Messages/Chat、原始可见文本及安全诊断；非文本隐藏块不落盘。
- 老库增加列后保留原失败回复及已消费预算。
- 带框且12引用的WATCH经完整模拟引擎仍为watch，只有一次主分析POST，零公开选合约、零买入通知、零新持仓。

交付从本地master提交归档，包内逐文件SHA256和源提交以DEPLOYMENT_MANIFEST.json为准；解压后的独立测试及ZIP SHA256另见旁置.verification.json/.sha256。不把本地离线测试称为服务器已更新或供应商以后无格式错误。

本次没有真实付费请求、MT5采集、飞书发送或交易启动。根/期权/crypto/local的私有配置和黄金两主脚本SHA256与修改前一致；本地runtime仍requests=9/scans=1/notices=0/positions=0。服务器须用户先停止扫描、覆盖完整源码并保留.env/runtime，再自行重启。服务器模型、档位、超时、代理和实际交易规则不需调整。
