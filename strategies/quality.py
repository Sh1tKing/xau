"""Shared AI-1 scorecard contract; no market, account, or network side effects."""

VERSION = "entry-quality-v3"
PREVIOUS_VERSION = "entry-quality-v2"
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


def rubric_fields():
    return {"version": VERSION, "level_points": dict(LEVEL_POINTS), "reservations_policy": RESERVATIONS_POLICY,
            "reservation_causes": sorted(CAUSES), "cause_rules": {cause: dict(rule) for cause, rule in CAUSE_RULES.items()}}
