"""Shared AI-1 scorecard contract; no market, account, or network side effects."""

VERSION = "entry-quality-v3"
PREVIOUS_VERSION = "entry-quality-v2"
LEDGER_VERSION = "entry-quality-v4"
CURRENT_VERSION = "entry-quality-v5"
LEDGER_GRADING_ORDER_VERSION = "ai1-issue-first-v1"
GRADING_ORDER_VERSION = "ai1-bound-severity-v1"
DIMENSIONS = ("structure", "trigger", "protection", "target", "counter_evidence")
LEVEL_RULES = {
    "strong": {"points": 20, "severity": None, "minimum_reservations": 0},
    "adequate": {"points": 16, "severity": "minor", "minimum_reservations": 0},
    "weak": {"points": 8, "severity": "material", "minimum_reservations": 1},
    "missing": {"points": 0, "severity": "blocking", "minimum_reservations": 1},
}
LEVEL_POINTS = {level: rule["points"] for level, rule in LEVEL_RULES.items()}
CAUSE_RULES = {
    "volume": {"minor_only": True, "observation_allowed": True, "meaning": "普通或偏弱量能，仅轻微观察；必要量数据缺失用missing_data。"},
    "shallow_retrace": {"minor_only": True, "observation_allowed": True, "meaning": "回撤深度略浅，仅轻微观察；深度下行、远离EMA不属于浅回撤。"},
    "trigger_weakness": {"minor_only": False, "observation_allowed": True, "meaning": "真实收盘触发不足或较弱；不得把已成立的触发说成不存在。轻微不足可观察，缺必要触发须保留意见。"},
    "opposing_structure": {"minor_only": False, "observation_allowed": True, "meaning": "有真实证据的反向信息；M15暂反向、单根M5颜色、未确认反转仅可作轻微观察，独立及时的重大反向结构须放reservations。"},
    "stop_vulnerability": {"minor_only": False, "observation_allowed": True, "meaning": "保护结构的风险意见；硬保护已满足的轻微顾虑可观察，命名结构或M5极值保护不足须保留意见并引用保护价和缓冲。"},
    "target_uncertainty": {"minor_only": False, "observation_allowed": True, "meaning": "有依据的目标或障碍意见；硬空间已满足的轻微不确定性可观察，重要障碍前空间不足须保留意见，不猜未来收益。"},
    "execution_drift": {"minor_only": False, "observation_allowed": True, "meaning": "报价变化影响当前执行；未影响合法执行的轻微波动可观察，实质执行风险须保留意见；旧收盘价与新报价不同本身不是冲突。"},
    "missing_data": {"minor_only": False, "observation_allowed": False, "meaning": "本次方案必需且输入确实缺失的数据，必须放reservations，不包括未知外部新闻。"},
    "hard_limit": {"minor_only": False, "observation_allowed": False, "meaning": "违反明确风险限制，必须放reservations；EMA偏离超过1.5ATR归此类，不归浅回撤。"},
}
CAUSES = frozenset(CAUSE_RULES)
MINOR_CAUSES = frozenset(cause for cause, rule in CAUSE_RULES.items() if rule["minor_only"])
SCORED_CAUSES = CAUSES - MINOR_CAUSES
OBSERVATION_CAUSES = frozenset(cause for cause, rule in CAUSE_RULES.items() if rule["observation_allowed"])


def dimension_fields(level, version=VERSION):
    fields = {"level", "reason", "evidence_refs"}
    if version == PREVIOUS_VERSION or level != "strong":
        fields.add("reservations")
    if version == VERSION and level != "strong":
        fields.add("observations")
    return fields


def allowed_causes(level, version=VERSION, observations=False):
    if observations:
        return OBSERVATION_CAUSES
    if version == PREVIOUS_VERSION and level == "adequate":
        return CAUSES
    return SCORED_CAUSES


def dimension_error(name, item, version=VERSION):
    """Validate wire shape and grade/cause relations before resolving market semantics."""
    path = "/quality_assessment/dimensions/" + name
    if not isinstance(item, dict):
        return "AI-1质量评估字段错误: %s必须是对象" % path
    level = item.get("level")
    if not isinstance(level, str) or level not in LEVEL_RULES:
        return "AI-1质量评估等级无效: %s/level=%r" % (path, level)
    if set(item) != dimension_fields(level, version):
        return "AI-1质量评估字段错误: %s level=%s 必须包含%s" % (
            path, level, sorted(dimension_fields(level, version)))
    for container in ("reservations", "observations"):
        values = item.get(container, [])
        if not isinstance(values, list):
            return "AI-1质量评估字段错误: %s/%s必须是数组" % (path, container)
        if container == "reservations":
            if level == "strong" and values or len(values) < LEVEL_RULES[level]["minimum_reservations"]:
                return "AI-1质量评估程度不符: %s level=%s 缺少对应保留意见或strong含保留" % (path, level)
        for index, issue in enumerate(values):
            issue_path = "%s/%s/%s" % (path, container, index)
            if not isinstance(issue, dict) or set(issue) != {"cause", "event_group", "reason", "evidence_refs"}:
                return "AI-1质量评估保留意见字段错误: " + issue_path
            cause, event = issue.get("cause"), issue.get("event_group")
            if not isinstance(cause, str) or cause not in CAUSES or event is not None and not isinstance(event, str):
                return "AI-1质量评估原因无效: " + issue_path
            if cause not in allowed_causes(level, version, container == "observations"):
                return "AI-1质量评估程度不符: %s cause=%s level=%s；%s" % (
                    issue_path, cause, level,
                    ("轻微原因须放observations，不能支撑weak/missing" if version == VERSION
                     else "普通量/浅回撤仅是轻微意见，不能支撑weak/missing") if cause in MINOR_CAUSES
                    else "硬限制/必要数据缺失不能放observations，必须放reservations")
            if not isinstance(issue.get("reason"), str) or not issue["reason"].strip():
                return "AI-1质量评估原因缺失: " + issue_path
            refs = issue.get("evidence_refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                return "AI-1质量评估证据无效: " + issue_path + "/evidence_refs"
    return ""


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def dimension_schema(refs):
    """Use nested anyOf instead of unsupported if/then or array maxItems."""
    def issue_schema(causes):
        return _object({"cause": {"type": "string", "enum": sorted(causes)},
                        "event_group": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "reason": {"type": "string"}, "evidence_refs": refs})

    branches = []
    # weak/missing share wire constraints, reducing provider union complexity.
    for levels in (("strong",), ("adequate",), ("weak", "missing")):
        level = levels[0]
        properties = {"level": {"type": "string", "enum": list(levels)},
                      "reason": {"type": "string"}, "evidence_refs": refs}
        if "reservations" in dimension_fields(level):
            properties["reservations"] = {"type": "array", "minItems": LEVEL_RULES[level]["minimum_reservations"],
                                          "items": issue_schema(allowed_causes(level))}
            properties["observations"] = {"type": "array", "items": issue_schema(allowed_causes(level, observations=True))}
        branch = _object(properties)
        branch["description"] = ("strong只写level/reason/evidence_refs，不含意见数组。" if level == "strong" else
                                 "必须写level/reason/evidence_refs/reservations/observations；"
                                 "即使没有意见，也不能省略reservations或observations，必须写空数组[]。"
                                 + ("weak/missing至少一条reservations，observations可以为空。" if level == "weak" else ""))
        branches.append(branch)
    return {"anyOf": branches}


ISSUE_DEDUPLICATION_POLICY = (
    "先确定每个cause/event的唯一意见归属，再填五项；全卡reservations和observations合计只声明一次。"
    "同一事实可出现在多个维度的reason/evidence_refs，但不能复制意见条目。"
    "例如同一根M15阴线若已在counter_evidence/observations，structure可描述它但不再放入reservations。"
    "event_group为空时按同cause及引用集合判重复；改写reason或引用顺序不能变成新事件。"
)

RESERVATIONS_POLICY = (
    "level唯一声明程度：" + "/".join("%s%s" % (level, rule["points"]) for level, rule in LEVEL_RULES.items()) + "；"
    "strong只填level/reason/evidence_refs，不含reservations或observations。"
    "其他等级还须填reservations和observations两个数组；adequate的reservations为minor，"
    "weak为material，missing为blocking，weak/missing至少一条reservations。"
    "cause声明问题主题，不自动声明程度。observations一律是minor，不继承所在维度的weak/missing程度；"
    "允许主题" + "/".join(sorted(OBSERVATION_CAUSES)) + "，例如单根M5反向但无反转确认；"
    "硬限制/必要数据缺失必须放reservations，独立及时的重大反证不能作为轻微观察。"
    + "/".join(sorted(MINOR_CAUSES)) + "只能放observations；观察无额外扣分、不能独立造成weak/missing或重大反证观望；"
    "重大问题和轻微观察可并存，以重大原因定level。"
    + ISSUE_DEDUPLICATION_POLICY
    + "每条意见含cause/event_group/reason/evidence_refs，Python生成ID/归属/程度，不由模型重复声明。"
)
INSTRUCTIONS = (
    "按quality_rubric评五项，不猜胜率、不复制本地分、不抬分。"
    "quality_assessment={version,direction,dimensions}，version=" + VERSION + "。"
    + RESERVATIONS_POLICY
    + "direction为buy/sell，dimensions是五项为键的对象。仅有轻微意见不剥夺合法提案；"
    "硬伤、必要证据缺失、无触发或重要障碍前空间不足则no并列wait_basis；"
    "M15或M5已有确认不能以另一周期未确认声称缺少触发。每条等级理由、保留、观察及等待引用本次真实evidence_refs。"
)


LEDGER_POLICY = (
    "五个dimensions只含level/reason/evidence_refs；保留意见和轻微观察统一声明在quality_assessment/issues。"
    "每条issues含dimension/kind/cause/event_group/reason/evidence_refs，dimension是唯一归属，"
    "kind为reservation或observation；不要在五项内重复填写意见数组。"
    "同cause和同event全卡只声明一次；event_group为空时以实际引用集合识别，改写reason或引用顺序不是新事件。"
    "同一事实仍可在多个维度reason/evidence_refs描述，不能重复成两条issues。"
    "例如同一M15阴线只有一条opposing_structure意见，可归counter_evidence，structure只描述事实。"
)
GRADING_ORDER_POLICY = (
    "先核对事实和独立风险，先写issues并确定每个cause/event唯一归属，再写dimensions及等级，"
    "不要先把多项评weak再复制同一事件凑保留意见。"
    "trigger评本方向实际收盘触发，counter_evidence评独立反向信息。"
    "例如本方向M15触发已成立、同根M5存在重大反向回收且没有另一项独立触发缺陷："
    "M5事件只声明一次opposing_structure reservation（通常归counter_evidence）；"
    "trigger可描述该事实，但须按自身触发证据评级，不因同一反证再次降为weak。"
    "若触发确有独立不足，应引用该独立事件另列trigger_weakness，不能把同一反证改名绕过去重。"
    "这不是将cause固定给某维度；归属由事实决定，同cause的独立事件均须保留。"
    "硬风险保持hard_limit reservation，EMA超限不是shallow_retrace；"
    "止损风险须按正确方向计算参考价与保护价加缓冲的距离，再与rules.max_stop_abs比较，不得误报超限。"
    "不得隐藏重大反证、删除事实、将重大改minor或为了契约通过抬等级；应先按独立证据判断。"
)
LEDGER_RESERVATIONS_POLICY = (
    "level唯一声明程度：" + "/".join("%s%s" % (level, rule["points"]) for level, rule in LEVEL_RULES.items()) + "；"
    + LEDGER_POLICY
    + "strong不得归属任何意见；adequate的reservation为minor，weak为material，missing为blocking；"
    "weak/missing必须各有至少一条归属本维度的reservation，不能借其他维度的意见撑扣分。"
    "observation永远minor且无额外扣分，不能独立支撑weak/missing或重大反证观望。"
    "cause是问题主题，不自动决定程度；普通量volume/浅回撤shallow_retrace仅可observation，"
    "硬限制hard_limit/必要数据缺失missing_data仅可reservation。"
    "单根M5反向、暂时M15反向及未确认反转可作opposing_structure observation，"
    "独立及时的重大反证必须reservation；重大意见与独立轻微观察可以并存。"
    "没有意见也必须写issues=[]；Python生成原因ID和程度，不由模型声明分数或修改判断来满足格式。"
)
LEDGER_INSTRUCTIONS = (
    "按quality_rubric评五项，不猜胜率、不复制本地分、不抬分。"
    "quality_assessment={version,direction,issues,dimensions}，version=" + LEDGER_VERSION + "，direction为buy/sell。"
    + LEDGER_RESERVATIONS_POLICY
    + "仅有轻微意见不剥夺合法提案；硬伤、必要证据缺失、无触发或重要障碍前空间不足则no并列顶层wait_basis。"
    "M15或M5已有确认不能以另一周期未确认声称缺少触发；所有等级理由、意见及等待引用本次真实evidence_refs。"
    "按quality_rubric.grading_order_policy先写issues再评五项，不能用同一反证给触发和反证两项重复扣分。"
)


def ledger_schema(refs):
    """One issue list and one uniform dimension shape; no grade-dependent arrays."""
    dimension = _object({"level": {"type": "string", "enum": list(LEVEL_RULES)},
                         "reason": {"type": "string"}, "evidence_refs": refs})
    dimension["description"] = (
        "在唯一issues清单之后按本项独立证据评级。weak/missing须本项独立reservation，"
        "不能复制别项已声明的cause/event来撑扣分；本方向触发成立时同一反证不等于触发缺失。")
    branches = []
    for kind, causes in (("reservation", SCORED_CAUSES), ("observation", OBSERVATION_CAUSES)):
        branches.append(_object({
            "cause": {"type": "string", "enum": sorted(causes)},
            "event_group": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "dimension": {"type": "string", "enum": list(DIMENSIONS)},
            "kind": {"type": "string", "enum": [kind]},
            "reason": {"type": "string"}, "evidence_refs": refs}))
    card = _object({"version": {"type": "string", "enum": [LEDGER_VERSION]},
                    "direction": {"type": "string", "enum": ["buy", "sell"]},
                    "issues": {"type": "array", "items": {"anyOf": branches},
                               "description": "先声明每个cause/event的唯一归属，不按五个维度复制同一条意见。"},
                    "dimensions": _object({name: {"$ref": "#/$defs/quality_dimension"} for name in DIMENSIONS})})
    card["description"] = LEDGER_POLICY
    return card, dimension


def decode_ledger(card):
    """Decode a declared v4 ledger into the shared validator, never repair a v3 card."""
    import copy
    root = "/quality_assessment"
    if set(card) != {"version", "direction", "dimensions", "issues"}:
        return None, "AI-1质量评估字段错误: %s v4须包含version/direction/dimensions/issues" % root
    dimensions, issues = card["dimensions"], card["issues"]
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        return None, "AI-1质量评估无效: 五项缺失"
    if not isinstance(issues, list):
        return None, "AI-1质量评估字段错误: %s/issues必须是数组" % root
    decoded = {"version": VERSION, "direction": card["direction"], "dimensions": {}}
    for name, item in dimensions.items():
        path = root + "/dimensions/" + name
        if not isinstance(item, dict) or set(item) != {"level", "reason", "evidence_refs"}:
            return None, "AI-1质量评估字段错误: %s v4只含level/reason/evidence_refs" % path
        level = item["level"]
        if not isinstance(level, str) or level not in LEVEL_RULES:
            return None, "AI-1质量评估等级无效: %s/level=%r" % (path, level)
        value = copy.deepcopy(item)
        if level != "strong":
            value.update(reservations=[], observations=[])
        decoded["dimensions"][name] = value
    seen = {}
    fields = {"dimension", "kind", "cause", "event_group", "reason", "evidence_refs"}
    for index, issue in enumerate(issues):
        path = "%s/issues/%s" % (root, index)
        if not isinstance(issue, dict) or set(issue) != fields:
            return None, "AI-1质量评估意见字段错误: " + path
        name, kind = issue["dimension"], issue["kind"]
        if not isinstance(name, str) or name not in DIMENSIONS or kind not in ("reservation", "observation"):
            return None, "AI-1质量评估意见归属无效: " + path
        cause, event, refs = issue["cause"], issue["event_group"], issue["evidence_refs"]
        if (not isinstance(cause, str) or cause not in CAUSES
                or event is not None and not isinstance(event, str)):
            return None, "AI-1质量评估原因无效: " + path
        if not isinstance(issue["reason"], str) or not issue["reason"].strip():
            return None, "AI-1质量评估原因缺失: " + path
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            return None, "AI-1质量评估证据无效: " + path + "/evidence_refs"
        level = dimensions[name]["level"]
        if level == "strong":
            return None, "AI-1质量评估程度不符: %s归属strong维度%s" % (path, name)
        if cause not in allowed_causes(level, VERSION, kind == "observation"):
            return None, "AI-1质量评估程度不符: %s cause=%s kind=%s level=%s" % (path, cause, kind, level)
        key = (cause, event or tuple(sorted(set(refs))))
        if key in seen:
            return None, ("AI-1质量评估重复扣分/意见: %s cause=%s 与%s重复；同一原因和事件只能声明一次"
                          % (path, cause, seen[key]))
        seen[key] = path
        value = {key: copy.deepcopy(issue[key]) for key in ("cause", "event_group", "reason", "evidence_refs")}
        decoded["dimensions"][name][kind + "s"].append(value)
    for name, item in decoded["dimensions"].items():
        error = dimension_error(name, item, VERSION)
        if error:
            return None, error
    return decoded, ""


def ledger_rubric_fields():
    fields = rubric_fields()
    fields.update(version=LEDGER_VERSION, reservations_policy=LEDGER_RESERVATIONS_POLICY,
                  issue_layout="single_ledger_unique_dimension_owner",
                  grading_order_version=LEDGER_GRADING_ORDER_VERSION, grading_order_policy=GRADING_ORDER_POLICY)
    return fields


def rubric_fields():
    return {"version": VERSION, "level_points": dict(LEVEL_POINTS), "reservations_policy": RESERVATIONS_POLICY,
            "reservation_causes": sorted(CAUSES), "cause_rules": {cause: dict(rule) for cause, rule in CAUSE_RULES.items()}}


CURRENT_RESERVATIONS_POLICY = (
    "意见与降级同源：五个dimensions只含support/reason/evidence_refs，support仅adequate或strong，"
    "说明本项正向证据支持，不独立输出level或分数。所有不利程度只在唯一issues清单声明一次。"
    "每条含dimension/kind/cause/event_group/severity/reason/evidence_refs；"
    "observation的severity只能minor；reservation按事实为minor/material/blocking。"
    "同cause/event全卡仅一个归属；空event按真实引用集合去重，不改名规避，同cause独立事件仍保留。"
    "Python逐项取最严重意见：blocking reservation为missing0，material reservation为weak8；"
    "仅有轻微意见为adequate16，无意见则按support给adequate16或strong20。每项只计一次，观察不叠扣。"
    "volume/shallow_retrace只能minor observation；hard_limit/missing_data必须blocking reservation。"
    "必要证据不存在或触发未成立须在实际所属项声明blocking，不能用support=adequate掩盖缺失；"
    "支持强也不能覆盖阻断问题。已有真实触发不能说成缺失，轻微反向不作重大或阻断。"
    "同一事实可在多项reason中说明，不重复声明为多项问题；独立的结构、触发、保护或目标缺陷须各有唯一意见。"
    "没有意见也须issues=[]，不得省掉独立重大反证来抬分或为了格式改变buy/sell/no。"
)
BOUND_GRADING_POLICY = (
    "先核实真实事实、必需数据、原始收盘触发与硬风险，逐个声明独立问题的唯一归属及程度，"
    "再填五项正向support和证据；最终等级由Python按本项最严重意见生成，不再另外声明weak/missing。"
    "trigger评本方向触发，counter_evidence评独立反向信息，同一反证不能同时当触发缺失重复扣分。"
    "独立触发缺陷或目标障碍必须声明自己的原因与真实证据，不能借其他项的意见。"
    "方向许可不等于触发；不能编造缺陷、隐藏事实或把重大意见降为轻微来通过契约。"
)
CURRENT_INSTRUCTIONS = (
    "按quality_rubric分析，不猜胜率、不复制本地分、不抬分。quality_assessment={version,direction,issues,dimensions}，"
    "version=" + CURRENT_VERSION + "，direction为buy/sell。" + CURRENT_RESERVATIONS_POLICY + BOUND_GRADING_POLICY
    + "仅轻微意见不剥夺合法提案；硬伤、必要证据缺失、无触发或重要障碍前空间不足则no并写顶层wait_basis。"
    "M15或M5已有确认不能因另一周期未确认声称没有触发；所有理由、意见和等待条件引用本次真实evidence_refs。"
)


def bound_schema(refs):
    """The only adverse severity is in its uniquely owned issue, never a second grade."""
    dimension = _object({"support": {"type": "string", "enum": ["adequate", "strong"]},
                         "reason": {"type": "string"}, "evidence_refs": refs})
    dimension["description"] = "只声明正向支持，不另写最终level；必要证据缺失须在issues声明blocking，不能藏在reason中。"
    branches = []
    for kind, causes, severities in (
            ("reservation", SCORED_CAUSES - {"hard_limit", "missing_data"}, ("minor", "material", "blocking")),
            ("reservation", {"hard_limit", "missing_data"}, ("blocking",)),
            ("observation", OBSERVATION_CAUSES, ("minor",))):
        branches.append(_object({
            "cause": {"type": "string", "enum": sorted(causes)},
            "event_group": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "dimension": {"type": "string", "enum": list(DIMENSIONS)},
            "kind": {"type": "string", "enum": [kind]},
            "severity": {"type": "string", "enum": list(severities)},
            "reason": {"type": "string"}, "evidence_refs": refs}))
    card = _object({"version": {"type": "string", "enum": [CURRENT_VERSION]},
                    "direction": {"type": "string", "enum": ["buy", "sell"]},
                    "issues": {"type": "array", "items": {"anyOf": branches}},
                    "dimensions": _object({name: {"$ref": "#/$defs/quality_dimension"} for name in DIMENSIONS})})
    card["description"] = CURRENT_RESERVATIONS_POLICY
    return card, dimension


def decode_bound(card):
    """Compile a declared v5 card; do not migrate, complete or regrade older cards."""
    import copy
    root = "/quality_assessment"
    if (not isinstance(card, dict) or set(card) != {"version", "direction", "issues", "dimensions"}
            or card["version"] != CURRENT_VERSION):
        return None, "AI-1质量评估字段错误: v5须包含version/direction/issues/dimensions"
    if not isinstance(card["direction"], str) or card["direction"] not in ("buy", "sell"):
        return None, "AI-1质量评估无效: 方向不一致"
    dimensions, issues = card["dimensions"], card["issues"]
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        return None, "AI-1质量评估无效: 五项缺失"
    if not isinstance(issues, list):
        return None, "AI-1质量评估字段错误: /quality_assessment/issues必须是数组"
    for name, item in dimensions.items():
        path = root + "/dimensions/" + name
        if not isinstance(item, dict) or set(item) != {"support", "reason", "evidence_refs"}:
            return None, "AI-1质量评估字段错误: %s v5只含support/reason/evidence_refs" % path
        if not isinstance(item["support"], str) or item["support"] not in ("adequate", "strong"):
            return None, "AI-1质量评估支持程度无效: " + path
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            return None, "AI-1质量评估原因缺失: " + path
        refs = item["evidence_refs"]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            return None, "AI-1质量评估证据无效: " + path + "/evidence_refs"
    seen, owned, reservations = {}, {name: [] for name in DIMENSIONS}, []
    fields = {"dimension", "kind", "cause", "event_group", "severity", "reason", "evidence_refs"}
    for index, issue in enumerate(issues):
        path = "%s/issues/%s" % (root, index)
        if not isinstance(issue, dict) or set(issue) != fields:
            return None, "AI-1质量评估意见字段错误: " + path
        name, kind, severity = issue["dimension"], issue["kind"], issue["severity"]
        if not isinstance(name, str) or name not in DIMENSIONS or kind not in ("reservation", "observation"):
            return None, "AI-1质量评估意见归属无效: " + path
        if not isinstance(severity, str) or severity not in ("minor", "material", "blocking"):
            return None, "AI-1质量评估意见程度无效: " + path
        cause, event, refs = issue["cause"], issue["event_group"], issue["evidence_refs"]
        if not isinstance(cause, str) or cause not in CAUSES or event is not None and not isinstance(event, str):
            return None, "AI-1质量评估原因无效: " + path
        if (kind == "observation" and (severity != "minor" or cause not in OBSERVATION_CAUSES)
                or kind == "reservation" and cause not in SCORED_CAUSES
                or cause in ("hard_limit", "missing_data") and (kind != "reservation" or severity != "blocking")):
            return None, "AI-1质量评估程度不符: %s cause=%s kind=%s severity=%s" % (path, cause, kind, severity)
        if not isinstance(issue["reason"], str) or not issue["reason"].strip():
            return None, "AI-1质量评估原因缺失: " + path
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
            return None, "AI-1质量评估证据无效: " + path + "/evidence_refs"
        key = (cause, event or tuple(sorted(set(refs))))
        if key in seen:
            return None, "AI-1质量评估重复扣分/意见: %s cause=%s 与%s重复；同一原因和事件只能声明一次" % (path, cause, seen[key])
        seen[key] = path
        owned[name].append(issue)
        if kind == "reservation":
            reservations.append({key: copy.deepcopy(value) for key, value in issue.items() if key != "kind"}
                                | {"id": "issue:%s" % index})
    decoded = {"version": "entry-quality-v1", "direction": card["direction"],
               "dimensions": {}, "reservations": reservations}
    for name in DIMENSIONS:
        entries = owned[name]
        severities = {item["severity"] for item in entries if item["kind"] == "reservation"}
        level = ("missing" if "blocking" in severities else "weak" if "material" in severities else
                 "adequate" if entries else dimensions[name]["support"])
        decoded["dimensions"][name] = {"level": level, "reason": dimensions[name]["reason"],
            "evidence_refs": copy.deepcopy(dimensions[name]["evidence_refs"]),
            "reservation_ids": [item["id"] for item in reservations if item["dimension"] == name]}
    return decoded, ""


def bound_rubric_fields():
    fields = rubric_fields()
    fields.update(version=CURRENT_VERSION, reservations_policy=CURRENT_RESERVATIONS_POLICY,
                  issue_layout="single_ledger_bound_severity", grading_order_version=GRADING_ORDER_VERSION,
                  grading_order_policy=BOUND_GRADING_POLICY)
    return fields
