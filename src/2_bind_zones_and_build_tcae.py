"""
Step 2: Interactively bind zones and build TCAE models.

Outputs:
- configurations/home/zones.json
- configurations/home/tcae.json

Zone binding is intentionally human-driven. The script never assumes that an
entity name contains a zone name. For each entity that observes or affects a
physical channel, the user selects its source/observation zone and reachable
zones from a user-provided zone list.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional, Tuple

from common import (
    entity_display_name,
    get_input_path,
    get_optional_input_path,
    get_output_path,
    listify,
    load_config,
    load_entity_registry,
    load_json,
    load_yaml,
    make_rule_uid,
    normalize_entity_ids,
    post_value_from_service,
    prompt_index_list,
    prompt_int,
    prompt_yes_no,
    relation_from_numeric_node,
    service_operation,
    unique_list,
    utc_now_iso,
    write_json,
)


def normalize_automation_list(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        return [x for x in raw.values() if isinstance(x, dict)]
    raise ValueError("Unsupported automations.yaml format. Expected a list or dict.")


def binding_has_physical_channel(binding: Dict[str, Any]) -> bool:
    if binding.get("observes"):
        return True
    if binding.get("effects"):
        return True
    if binding.get("effects_by_operation"):
        return any(binding.get("effects_by_operation", {}).values())
    return False


def load_binding_map(channels_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {b["entity_id"]: b for b in channels_data.get("bindings", []) if isinstance(b, dict) and b.get("entity_id")}


def load_device_map(devices_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e["entity_id"]: e for e in devices_data.get("entities", []) if isinstance(e, dict) and e.get("entity_id")}


def zh_domain(domain: Optional[str]) -> str:
    """Map Home Assistant domain to a user-facing Chinese explanation.

    This function only explains HA platform/domain types. It does not infer
    semantic meaning from the user-defined object_id after the dot.
    """
    mapping = {
        "input_number": "数值型辅助实体（通常表示传感器读数或可设置数值）",
        "number": "数值型实体",
        "counter": "计数型实体",
        "input_boolean": "开关型辅助实体（通常表示开/关状态或虚拟设备）",
        "switch": "开关设备",
        "light": "灯光设备",
        "binary_sensor": "二值传感器",
        "sensor": "传感器",
        "input_button": "按钮/事件型辅助实体",
        "button": "按钮实体",
        "input_datetime": "日期时间型辅助实体",
        "input_select": "选项型辅助实体",
        "select": "选项型实体",
        "lock": "门锁/锁具实体",
        "cover": "门窗/卷帘类实体",
        "fan": "风扇/通风类实体",
        "climate": "空调/恒温器类实体",
        "automation": "自动化规则实体",
    }
    return mapping.get(str(domain or ""), f"Home Assistant 域：{domain or 'unknown'}")



def zh_role(role: Optional[str]) -> str:
    mapping = {
        "sensor": "观测实体：主要用于读取环境或设备状态",
        "actuator": "执行实体：主要用于被规则动作控制，并可能影响环境",
        "hybrid": "混合实体：既可能被观测，也可能被动作控制",
        "logical": "逻辑实体：主要表示逻辑状态，未必对应物理通道",
        "unknown": "未确定角色",
    }
    return mapping.get(str(role or "unknown"), str(role or "unknown"))



def zh_position(position: Optional[str]) -> str:
    mapping = {
        "trigger": "触发器中使用：该实体状态变化可能触发规则",
        "condition": "条件中使用：该实体状态用于判断规则是否允许执行",
        "action": "动作中使用：该实体会被规则控制或设置",
    }
    return mapping.get(str(position or ""), str(position or ""))



def zh_channel(channel: Optional[str]) -> str:
    mapping = {
        "temperature": "温度",
        "humidity": "湿度",
        "pressure": "压力",
        "light": "光照/亮度",
        "sound": "声音",
        "motion": "运动/人体活动",
        "smoke": "烟雾",
        "water_flow": "水流/供水",
        "water_supply": "供水能力",
        "security": "安全/通行",
    }
    c = str(channel or "unknown")
    return mapping.get(c, c)



def zh_value_type(value_type: Optional[str]) -> str:
    mapping = {
        "numeric": "数值读数",
        "state": "状态值",
        "event": "事件/按钮触发",
        "datetime": "日期时间值",
        "select": "选项值",
        "unknown": "未知类型",
    }
    return mapping.get(str(value_type or "unknown"), str(value_type or "unknown"))



def zh_operation(operation: Optional[str]) -> str:
    mapping = {
        "default": "默认/一般情况",
        "turn_on": "打开/启用时",
        "turn_off": "关闭/停用时",
        "toggle": "切换状态时",
        "set_value": "设置数值时",
        "set_temperature": "设置温度时",
        "set_humidity": "设置湿度时",
        "open": "打开时",
        "close": "关闭时",
        "lock": "锁定时",
        "unlock": "解锁时",
        "start": "启动时",
        "stop": "停止时",
        "unknown": "未知动作时",
    }
    return mapping.get(str(operation or "unknown"), str(operation or "unknown"))



def zh_direction(direction: Optional[str], channel: Optional[str] = None) -> str:
    channel_text = zh_channel(channel)
    d = str(direction or "unknown")
    if d == "+1":
        return f"使{channel_text}升高/增强"
    if d == "-1":
        return f"使{channel_text}降低/减弱"
    if d == "0":
        return f"对{channel_text}无明显方向性影响"
    return f"对{channel_text}的影响方向不确定"



def zh_confidence(confidence: Any) -> str:
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "置信度未知"
    if c >= 0.85:
        level = "高"
    elif c >= 0.6:
        level = "中"
    elif c > 0:
        level = "低"
    else:
        level = "未知"
    return f"{level}置信度（{c:.2f}）"



def unique_effect_items(binding: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Return display-oriented effect items without duplicated lines."""
    items: List[Tuple[str, Dict[str, Any]]] = []
    seen = set()

    def add(op: str, eff: Dict[str, Any]) -> None:
        key = (op, eff.get("channel"), eff.get("direction"), eff.get("reason"))
        if key in seen:
            return
        seen.add(key)
        items.append((op, eff))

    for e in binding.get("effects", []) or []:
        add(str(e.get("operation", "default")), e)
    for op, effs in (binding.get("effects_by_operation", {}) or {}).items():
        for e in effs or []:
            add(str(op), e)
    return items



def get_display_name(entity_id: str, device: Optional[Dict[str, Any]] = None, registry_map: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    if device and device.get("display_name"):
        return str(device["display_name"])
    return entity_display_name(entity_id, registry_map or {})


def print_entity_zone_context(entity_id: str, device: Dict[str, Any], binding: Dict[str, Any], registry_map: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Print zone-binding context in natural Chinese for human review."""
    print("\n" + "=" * 88)
    print(f"实体：{get_display_name(entity_id, device, registry_map)}")
    domain = device.get("domain")
    role = binding.get("role")
    positions = device.get("positions") or []
    print(f"实体类型：{zh_domain(domain)}")
    print(f"AI 初步判断角色：{zh_role(role)}")
    if positions:
        print("在自动化规则中的用途：")
        for p in positions:
            print(f"  - {zh_position(p)}")

    observes = binding.get("observes", []) or []
    effects_for_display = unique_effect_items(binding)

    if observes:
        print("\n该实体被识别为可能【观测】以下物理通道：")
        for idx, o in enumerate(observes, start=1):
            print(f"  {idx}. {zh_channel(o.get('channel'))}（{zh_value_type(o.get('value_type'))}，{zh_confidence(o.get('confidence'))}）")
            if o.get("reason"):
                print(f"     判断依据：{o.get('reason')}")
    if effects_for_display:
        print("\n该实体被识别为可能【影响】以下物理通道：")
        for idx, (op, e) in enumerate(effects_for_display, start=1):
            print(f"  {idx}. {zh_operation(op)}：{zh_direction(e.get('direction'), e.get('channel'))}（{zh_confidence(e.get('confidence'))}）")
            if e.get("reason"):
                print(f"     判断依据：{e.get('reason')}")
    if not observes and not effects_for_display:
        print("\n该实体当前没有明确的物理通道绑定。若不需要建模其 zone，可跳过。")

    print("\n相关规则上下文（用于帮助判断该实体实际位于哪个区域）：")
    contexts = device.get("raw_contexts", []) or []
    for ctx in contexts[:6]:
        section = zh_position(ctx.get("section"))
        print(f"  - 使用位置：{section}")
        print(f"    规则：{ctx.get('rule_alias')}")
        desc = ctx.get("rule_description")
        if desc:
            print(f"    规则描述：{desc}")
        if ctx.get("service"):
            print(f"    执行动作：调用服务 {ctx.get('service')}，含义：{zh_operation(ctx.get('operation'))}，目标状态/参数：{ctx.get('post_value')}")
    if len(contexts) > 6:
        print(f"  ... 还有 {len(contexts) - 6} 条上下文未显示。")


def interactive_zone_binding(
    devices_data: Dict[str, Any],
    channels_data: Dict[str, Any],
    existing: Optional[Dict[str, Any]] = None,
    registry_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    registry_map = registry_map or {}
    device_map = load_device_map(devices_data)
    binding_map = load_binding_map(channels_data)
    existing = existing or {}
    existing_bindings = existing.get("bindings", {}) if isinstance(existing.get("bindings"), dict) else {}

    print("\n请先输入家庭区域（zone）信息。zone 名称只作为用户定义的字符串，不依赖实体命名。")
    if existing.get("zones") and prompt_yes_no("检测到已有 zones.json，是否复用原 zone 列表？", default=True):
        zones = existing["zones"]
    else:
        n = prompt_int("一共有多少个 zone？ ", min_value=1)
        zones = []
        for i in range(n):
            while True:
                name = input(f"请输入 zone {i} 名称: ").strip()
                if name:
                    break
                print("zone 名称不能为空。")
            zones.append({"zone_id": i, "name": name})

    print("\nZone 列表:")
    for z in zones:
        print(f"  {z['zone_id']}: {z['name']}")
    valid_indices = [int(z["zone_id"]) for z in zones]
    name_by_id = {int(z["zone_id"]): z["name"] for z in zones}

    result_bindings: Dict[str, Any] = {}
    target_entities = [eid for eid, b in sorted(binding_map.items()) if binding_has_physical_channel(b)]

    print(f"\n需要绑定 zone 的实体数量: {len(target_entities)}")
    for eid in target_entities:
        device = device_map.get(eid, {"entity_id": eid, "display_name": entity_display_name(eid, registry_map)})
        binding = binding_map[eid]
        prev = existing_bindings.get(eid)
        print_entity_zone_context(eid, device, binding, registry_map)

        if prev:
            print("已有绑定:")
            print(json.dumps(prev, ensure_ascii=False, indent=2))
            if prompt_yes_no("是否保留该实体已有 zone 绑定？", default=True):
                result_bindings[eid] = prev
                continue

        zone_ids = prompt_index_list("请选择该实体的源/观测 zone id（通常一个；可多选）:", valid_indices)
        source_zones = [name_by_id[i] for i in zone_ids]

        reachable_default = zone_ids
        if binding.get("effects") or binding.get("effects_by_operation"):
            print("如果该执行器会跨区域影响环境，请选择所有可达 zone；例如开放式厨房可同时选择 Kitchen 和 LivingRoom。")
            reachable_ids = prompt_index_list("请选择 reachable zone id（可多选）:", valid_indices, default=reachable_default)
        else:
            reachable_ids = zone_ids
        reachable_zones = [name_by_id[i] for i in reachable_ids]

        result_bindings[eid] = {
            "entity_id": eid,
            "display_name": get_display_name(eid, device, registry_map),
            "source_zones": source_zones,
            "reachable_zones": reachable_zones,
            "source_zone_ids": zone_ids,
            "reachable_zone_ids": reachable_ids,
            "notes": "human_bound",
        }

    # Preserve non-physical entities if previously present, but do not require user input.
    for eid in sorted(binding_map):
        if eid not in result_bindings and eid in existing_bindings:
            result_bindings[eid] = existing_bindings[eid]

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "human_cli",
        "zones": zones,
        "bindings": result_bindings,
    }


def get_zone_binding(zones_data: Dict[str, Any], entity_id: str) -> Optional[Dict[str, Any]]:
    return (zones_data.get("bindings") or {}).get(entity_id)


def get_channel_binding(binding_map: Dict[str, Dict[str, Any]], entity_id: str) -> Optional[Dict[str, Any]]:
    return binding_map.get(entity_id)


def normalize_trigger_node(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    platform = node.get("platform")
    out: List[Dict[str, Any]] = []
    if platform == "state":
        for eid in normalize_entity_ids(node.get("entity_id")):
            out.append({"type": "state", "entity_id": eid, "from": node.get("from"), "to": node.get("to")})
    elif platform == "numeric_state":
        for eid in normalize_entity_ids(node.get("entity_id")):
            out.append({"type": "numeric_state", "entity_id": eid, "above": node.get("above"), "below": node.get("below"), "attribute": node.get("attribute")})
    elif platform == "time":
        out.append({"type": "time", "at": node.get("at")})
    else:
        refs = normalize_entity_ids(node.get("entity_id"))
        if refs:
            for eid in refs:
                out.append({"type": platform or "unknown", "entity_id": eid, "raw": node})
        else:
            out.append({"type": platform or "unknown", "raw": node})
    return out


def normalize_condition_node(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    ctype = node.get("condition")
    out: List[Dict[str, Any]] = []
    if ctype == "state":
        for eid in normalize_entity_ids(node.get("entity_id")):
            out.append({"type": "state", "entity_id": eid, "state": node.get("state")})
    elif ctype == "numeric_state":
        for eid in normalize_entity_ids(node.get("entity_id")):
            out.append({"type": "numeric_state", "entity_id": eid, "above": node.get("above"), "below": node.get("below"), "attribute": node.get("attribute")})
    elif ctype == "time":
        out.append({"type": "time", "before": node.get("before"), "after": node.get("after"), "weekday": node.get("weekday")})
    else:
        refs = normalize_entity_ids(node.get("entity_id"))
        if refs:
            for eid in refs:
                out.append({"type": ctype or "unknown", "entity_id": eid, "raw": node})
        else:
            out.append({"type": ctype or "unknown", "raw": node})
    return out


def normalize_action_node(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    service = node.get("service")
    op = service_operation(service)
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    post = post_value_from_service(service, data)
    targets: List[str] = []
    target = node.get("target")
    if isinstance(target, dict):
        targets.extend(normalize_entity_ids(target.get("entity_id")))
    if isinstance(data, dict):
        targets.extend(normalize_entity_ids(data.get("entity_id")))
    targets.extend(normalize_entity_ids(node.get("entity_id")))
    targets = unique_list(targets)
    if not targets:
        return [{"type": "service", "service": service, "operation": op, "target_entity": None, "post": post, "data": data, "raw": node}]
    return [{"type": "service", "service": service, "operation": op, "target_entity": eid, "post": post, "data": data} for eid in targets]


def select_observe_channels(binding: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return list(binding.get("observes", [])) if binding else []


def select_effects_for_operation(binding: Optional[Dict[str, Any]], operation: str) -> List[Dict[str, Any]]:
    if not binding:
        return []
    ebo = binding.get("effects_by_operation") or {}
    effects: List[Dict[str, Any]] = []
    if operation in ebo:
        effects.extend(ebo[operation])
    if not effects and "default" in ebo:
        effects.extend(ebo["default"])
    # Add generic effects whose operation matches current op/default.
    for eff in binding.get("effects", []) or []:
        eff_op = eff.get("operation", "default")
        if eff_op in {operation, "default", "unknown"}:
            effects.append(eff)
    return effects


def build_env_refs_from_numeric(
    node: Dict[str, Any],
    trigger: bool,
    binding_map: Dict[str, Dict[str, Any]],
    zones_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for eid in normalize_entity_ids(node.get("entity_id")):
        binding = get_channel_binding(binding_map, eid)
        zone_binding = get_zone_binding(zones_data, eid)
        if not binding or not zone_binding:
            continue
        observes = select_observe_channels(binding)
        relations = relation_from_numeric_node(node, trigger=trigger)
        for obs in observes:
            channel = obs.get("channel")
            for zone in zone_binding.get("source_zones", []):
                for relation, threshold in relations:
                    refs.append(
                        {
                            "entity_id": eid,
                            "zone": zone,
                            "channel": channel,
                            "relation": relation,
                            "threshold": threshold,
                            "source": "trigger" if trigger else "condition",
                            "confidence": obs.get("confidence", 0),
                        }
                    )
    return refs


def build_env_effects_from_action(
    action: Dict[str, Any],
    binding_map: Dict[str, Dict[str, Any]],
    zones_data: Dict[str, Any],
    defaults: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    defaults = defaults or {}
    effects: List[Dict[str, Any]] = []
    operation = action.get("operation")
    target = action.get("target_entity")
    if not target:
        return effects
    binding = get_channel_binding(binding_map, target)
    zone_binding = get_zone_binding(zones_data, target)
    if not binding or not zone_binding:
        return effects
    for eff in select_effects_for_operation(binding, operation):
        direction = eff.get("direction", "unknown")
        if direction == "unknown" or direction == "0":
            continue
        effects.append(
            {
                "entity_id": target,
                "operation": operation,
                "source_zones": zone_binding.get("source_zones", []),
                "reachable_zones": zone_binding.get("reachable_zones", zone_binding.get("source_zones", [])),
                "channel": eff.get("channel"),
                "direction": direction,
                "mu": defaults.get("mu", 1),
                "lambda": defaults.get("lambda", 0),
                "duration": defaults.get("duration", "inf"),
                "confidence": eff.get("confidence", 0),
                "reason": eff.get("reason", ""),
            }
        )
    return effects


def build_tcae(automations: List[Dict[str, Any]], devices_data: Dict[str, Any], channels_data: Dict[str, Any], zones_data: Dict[str, Any]) -> Dict[str, Any]:
    binding_map = load_binding_map(channels_data)
    device_map = load_device_map(devices_data)
    rules: List[Dict[str, Any]] = []

    for idx, rule in enumerate(automations, start=1):
        rule_uid = make_rule_uid(rule, idx)
        triggers: List[Dict[str, Any]] = []
        conditions: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        e_t: List[Dict[str, Any]] = []
        e_c: List[Dict[str, Any]] = []
        e_a: List[Dict[str, Any]] = []

        for node in listify(rule.get("trigger")):
            if not isinstance(node, dict):
                continue
            triggers.extend(normalize_trigger_node(node))
            if node.get("platform") == "numeric_state":
                e_t.extend(build_env_refs_from_numeric(node, True, binding_map, zones_data))

        for node in listify(rule.get("condition")):
            if not isinstance(node, dict):
                continue
            conditions.extend(normalize_condition_node(node))
            if node.get("condition") == "numeric_state":
                e_c.extend(build_env_refs_from_numeric(node, False, binding_map, zones_data))

        for node in listify(rule.get("action")):
            if not isinstance(node, dict):
                continue
            normalized_actions = normalize_action_node(node)
            actions.extend(normalized_actions)
            for action in normalized_actions:
                e_a.extend(build_env_effects_from_action(action, binding_map, zones_data))

        rules.append(
            {
                "rule_uid": rule_uid,
                "rule_id": rule.get("id"),
                "display_alias": rule.get("alias"),
                "entity_display": {
                    eid: device_map.get(eid, {}).get("display_name", eid)
                    for eid in sorted({
                        *(a.get("target_entity") for a in actions if a.get("target_entity")),
                        *(t.get("entity_id") for t in triggers if t.get("entity_id")),
                        *(c.get("entity_id") for c in conditions if c.get("entity_id")),
                    })
                },
                "alias": rule.get("alias"),
                "description": rule.get("description"),
                "mode": rule.get("mode"),
                "T": triggers,
                "C": conditions,
                "A": actions,
                "E": {
                    "E_T": e_t,
                    "E_C": e_c,
                    "E_A": e_a,
                },
                "raw": rule,
            }
        )

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "model": "TCAE",
        "rules": rules,
        "summary": {
            "rule_count": len(rules),
            "env_trigger_ref_count": sum(len(r["E"]["E_T"]) for r in rules),
            "env_condition_ref_count": sum(len(r["E"]["E_C"]) for r in rules),
            "env_action_effect_count": sum(len(r["E"]["E_A"]) for r in rules),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactively bind zones and build TCAE models.")
    parser.add_argument("--reuse-zones", action="store_true", help="Reuse existing zones.json without prompting if available.")
    parser.add_argument("--rebind-zones", action="store_true", help="Force interactive zone rebinding even if zones.json exists.")
    args = parser.parse_args()

    config = load_config()
    automations_path = get_input_path(config, "automations")
    registry_path = get_optional_input_path(config, "entity_registry", "./configurations/core.entity_registry")
    registry_map = load_entity_registry(registry_path)
    if registry_map:
        print(f"已加载实体名称映射: {registry_path}（{len(registry_map)} 条）")
    devices_path = get_output_path(config, "devices")
    channels_path = get_output_path(config, "channels")
    zones_path = get_output_path(config, "zones")
    tcae_path = get_output_path(config, "tcae")

    devices_data = load_json(devices_path)
    channels_data = load_json(channels_path)
    automations = normalize_automation_list(load_yaml(automations_path, default=[]))

    existing_zones = load_json(zones_path, default={}) if zones_path.exists() else {}
    if args.reuse_zones and existing_zones and not args.rebind_zones:
        zones_data = existing_zones
        print(f"复用已有 zones.json: {zones_path}")
    else:
        zones_data = interactive_zone_binding(devices_data, channels_data, existing=existing_zones, registry_map=registry_map)
        write_json(zones_path, zones_data)
        print(f"\n已写入 zone 绑定: {zones_path}")

    tcae = build_tcae(automations, devices_data, channels_data, zones_data)
    write_json(tcae_path, tcae)
    print(f"已生成 TCAE 模型: {tcae_path}")
    print(json.dumps(tcae.get("summary", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
