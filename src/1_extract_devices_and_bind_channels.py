"""
Step 1: Extract Home Assistant entities and bind physical channels with AI.

Outputs:
- configurations/home/devices.json
- configurations/home/channels.json

Design goals:
- Do not classify by user-defined entity object names with hard-coded language
  rules. Only HA domains/services/positions are treated as system variables.
- Entity names, rule aliases and descriptions are provided to AI as semantic
  hints. The AI returns a JSON binding that can be reviewed by users.
- If AI is unavailable, the script still writes devices.json and conservative
  empty channel bindings.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from common import (
    HomeAgentError,
    call_ai_json,
    domain_of,
    ensure_dir,
    enrich_entity_with_registry,
    entity_display_name,
    get_input_path,
    get_optional_input_path,
    get_output_path,
    infer_value_type_from_domain,
    listify,
    load_config,
    load_entity_registry,
    load_env,
    load_yaml,
    make_rule_uid,
    normalize_entity_ids,
    post_value_from_service,
    save_config,
    service_domain,
    service_operation,
    unique_list,
    utc_now_iso,
    write_json,
)


DEFAULT_CHANNEL_BINDING_PROMPT = r"""
You are the channel binding and channel discovery module of HomeAgent. Your task is to infer physical-channel bindings for Home Assistant entities from automation context and propose missing channels when the current channel ontology is insufficient.

Important principles:
1. Do NOT rely on any specific human language or hard-coded words. Entity object names may be Chinese pinyin, Japanese romanization, Spanish, English, abbreviations, or arbitrary user text. Treat names/descriptions/aliases only as semantic hints.
2. The only platform/system variables you may rely on are Home Assistant domains, service names, trigger/condition/action positions, and structured YAML fields.
3. For entity bindings, use ONLY the candidate channel list supplied by the user. If a useful channel is not in the candidate list, do NOT put it into observes/effects. Instead, add it to proposed_channels.
4. Distinguish sensors from actuators:
   - A sensor/observable entity observes environmental channels.
   - An actuator/action target may affect environmental channels.
   - A hybrid entity may have both.
5. Multi-channel effects are allowed. For example, an air conditioner may decrease temperature and humidity; a heater may increase temperature; a humidifier may increase humidity; a sprinkler may affect humidity/water_flow. Use context to decide.
6. Effects must be action-specific when possible. For an actuator, fill effects_by_operation for operations such as turn_on, turn_off, set_value, open, close, lock, unlock. Direction must be +1, -1, 0, or unknown.
7. If an entity is security/logical/resource related and the candidate channel list lacks a suitable channel, leave observes/effects empty for that missing relation and propose a new channel. For example, locks/windows may suggest security or access; valves/sprinklers may suggest water_supply if water_flow is insufficient.
8. proposed_channels are suggestions only. They will be manually reviewed and added to config.json by the user before rerunning this script.
9. Return strict JSON only. Do not include markdown or commentary.

Output schema:
{
  "bindings": [
    {
      "entity_id": "string",
      "role": "sensor|actuator|hybrid|logical|unknown",
      "observes": [
        {
          "channel": "one candidate channel only",
          "value_type": "numeric|state|event|datetime|select|unknown",
          "confidence": 0.0,
          "reason": "brief reason"
        }
      ],
      "effects": [
        {
          "channel": "one candidate channel only",
          "direction": "+1|-1|0|unknown",
          "operation": "default or Home Assistant service operation",
          "confidence": 0.0,
          "reason": "brief reason"
        }
      ],
      "effects_by_operation": {
        "turn_on": [
          {
            "channel": "one candidate channel only",
            "direction": "+1|-1|0|unknown",
            "confidence": 0.0,
            "reason": "brief reason"
          }
        ]
      },
      "needs_human_review": true,
      "notes": "brief notes"
    }
  ],
  "proposed_channels": [
    {
      "channel": "lower_snake_case_new_channel_name",
      "description": "what this channel represents",
      "reason": "why candidate_channels cannot express this relation well",
      "related_entities": ["entity_id_1", "entity_id_2"],
      "suggested_value_type": "numeric|state|event|unknown",
      "confidence": 0.0
    }
  ]
}
""".strip()


OBSERVATION_SECTIONS = {"trigger", "condition"}
ACTION_SECTION = "action"


def ensure_system_prompts(config: Dict[str, Any]) -> bool:
    """Ensure config.json has a good channel-binding prompt.

    Returns True if config was modified.
    """
    prompts = config.setdefault("system_prompts", {})
    if not prompts.get("channels_binding"):
        prompts["channels_binding"] = DEFAULT_CHANNEL_BINDING_PROMPT
        return True
    return False


def normalize_automation_list(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        # Some HA exports may be a dictionary keyed by id.
        return [x for x in raw.values() if isinstance(x, dict)]
    raise ValueError("Unsupported automations.yaml format. Expected a list or dict.")


def traverse_entity_references(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    """Return (entity_id, yaml_path) from an arbitrary YAML subtree."""
    refs: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in {"entity_id", "device_id"}:
                # Only entity_id has HA entity format. device_id is intentionally
                # ignored if it does not look like an entity id.
                for eid in normalize_entity_ids(value):
                    refs.append((eid, child_path))
            else:
                refs.extend(traverse_entity_references(value, child_path))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            refs.extend(traverse_entity_references(value, f"{path}[{idx}]"))
    return refs


def collect_action_targets(action_node: Dict[str, Any]) -> List[str]:
    targets: List[str] = []
    target = action_node.get("target")
    if isinstance(target, dict):
        targets.extend(normalize_entity_ids(target.get("entity_id")))
    # Some HA actions put entity_id in data.
    data = action_node.get("data")
    if isinstance(data, dict):
        targets.extend(normalize_entity_ids(data.get("entity_id")))
    # Some service calls place entity_id at top level.
    targets.extend(normalize_entity_ids(action_node.get("entity_id")))
    return unique_list(targets)


def empty_entity_record(entity_id: str) -> Dict[str, Any]:
    domain = domain_of(entity_id)
    return {
        "entity_id": entity_id,
        "domain": domain,
        "object_id": entity_id.split(".", 1)[1] if "." in entity_id else entity_id,
        "value_type_hint": infer_value_type_from_domain(domain),
        "positions": [],
        "roles": [],
        "rules": [],
        "operations": [],
        "raw_contexts": [],
    }


def append_entity_context(
    entities: Dict[str, Dict[str, Any]],
    entity_id: str,
    section: str,
    rule: Dict[str, Any],
    rule_uid: str,
    node: Dict[str, Any],
    yaml_path: str,
    operation: Optional[str] = None,
    post_value: Any = None,
) -> None:
    rec = entities.setdefault(entity_id, empty_entity_record(entity_id))
    if section not in rec["positions"]:
        rec["positions"].append(section)
    if rule_uid not in rec["rules"]:
        rec["rules"].append(rule_uid)
    if operation and operation not in rec["operations"]:
        rec["operations"].append(operation)

    context = {
        "rule_uid": rule_uid,
        "rule_id": rule.get("id"),
        "rule_alias": rule.get("alias"),
        "rule_description": rule.get("description"),
        "section": section,
        "yaml_path": yaml_path,
        "platform": node.get("platform") if isinstance(node, dict) else None,
        "condition": node.get("condition") if isinstance(node, dict) else None,
        "service": node.get("service") if isinstance(node, dict) else None,
        "operation": operation,
        "post_value": post_value,
        "node_excerpt": compact_node(node),
    }
    rec["raw_contexts"].append(context)


def compact_node(node: Any, max_chars: int = 1200) -> Any:
    """Make a JSON-serializable compact node excerpt."""
    try:
        text = json.dumps(node, ensure_ascii=False)
        if len(text) <= max_chars:
            return node
        return json.loads(text[:max_chars] + '"..."')
    except Exception:
        s = str(node)
        return s[:max_chars]


def finalize_roles(entity: Dict[str, Any]) -> None:
    positions = set(entity.get("positions", []))
    roles: List[str] = []
    if positions & OBSERVATION_SECTIONS:
        roles.append("sensor")
    if ACTION_SECTION in positions:
        roles.append("actuator")
    if not roles:
        roles.append("unknown")
    if set(roles) == {"sensor", "actuator"}:
        entity["primary_role"] = "hybrid"
    elif roles == ["sensor"]:
        entity["primary_role"] = "sensor"
    elif roles == ["actuator"]:
        entity["primary_role"] = "actuator"
    else:
        entity["primary_role"] = "unknown"
    entity["roles"] = roles


def extract_devices(automations: List[Dict[str, Any]], registry_map: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    registry_map = registry_map or {}
    entities: Dict[str, Dict[str, Any]] = {}
    rules_summary: List[Dict[str, Any]] = []

    for idx, rule in enumerate(automations, start=1):
        rule_uid = make_rule_uid(rule, idx)
        rules_summary.append(
            {
                "rule_uid": rule_uid,
                "rule_id": rule.get("id"),
                "alias": rule.get("alias"),
                "description": rule.get("description"),
                "mode": rule.get("mode"),
            }
        )

        for t_idx, trigger in enumerate(listify(rule.get("trigger"))):
            if not isinstance(trigger, dict):
                continue
            for eid, path in traverse_entity_references(trigger, f"trigger[{t_idx}]"):
                append_entity_context(entities, eid, "trigger", rule, rule_uid, trigger, path)

        for c_idx, condition in enumerate(listify(rule.get("condition"))):
            if not isinstance(condition, dict):
                continue
            for eid, path in traverse_entity_references(condition, f"condition[{c_idx}]"):
                append_entity_context(entities, eid, "condition", rule, rule_uid, condition, path)

        for a_idx, action in enumerate(listify(rule.get("action"))):
            if not isinstance(action, dict):
                continue
            op = service_operation(action.get("service"))
            post_value = post_value_from_service(action.get("service"), action.get("data") if isinstance(action.get("data"), dict) else {})
            targets = collect_action_targets(action)
            for eid in targets:
                append_entity_context(entities, eid, "action", rule, rule_uid, action, f"action[{a_idx}].target", op, post_value)
            # Also collect any non-target entity references as action context, but
            # do not mark them as action targets if the service does not target them.
            for eid, path in traverse_entity_references(action, f"action[{a_idx}]"):
                if eid not in targets:
                    append_entity_context(entities, eid, "action_reference", rule, rule_uid, action, path, op, post_value)

    for entity in entities.values():
        entity["positions"] = sorted(unique_list(entity["positions"]))
        entity["rules"] = sorted(unique_list(entity["rules"]))
        entity["operations"] = sorted(unique_list(entity["operations"]))
        finalize_roles(entity)
        enrich_entity_with_registry(entity, registry_map)

    entity_list = sorted(entities.values(), key=lambda x: x["entity_id"])
    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "source": "automations.yaml",
        "summary": {
            "rule_count": len(automations),
            "entity_count": len(entity_list),
        },
        "rules": rules_summary,
        "entities": entity_list,
    }


def compact_devices_for_ai(devices: Dict[str, Any]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for e in devices.get("entities", []):
        contexts = []
        for ctx in e.get("raw_contexts", [])[:8]:
            contexts.append(
                {
                    "rule_alias": ctx.get("rule_alias"),
                    "rule_description": ctx.get("rule_description"),
                    "section": ctx.get("section"),
                    "platform": ctx.get("platform"),
                    "condition": ctx.get("condition"),
                    "service": ctx.get("service"),
                    "operation": ctx.get("operation"),
                    "post_value": ctx.get("post_value"),
                    "node_excerpt": ctx.get("node_excerpt"),
                }
            )
        compact.append(
            {
                "entity_id": e["entity_id"],
                "display_name": e.get("display_name", e["entity_id"]),
                "registry": e.get("registry", {}),
                "domain": e["domain"],
                "value_type_hint": e.get("value_type_hint"),
                "positions": e.get("positions", []),
                "primary_role": e.get("primary_role"),
                "operations": e.get("operations", []),
                "contexts": contexts,
            }
        )
    return compact


def default_empty_binding(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entity_id": entity["entity_id"],
        "role": entity.get("primary_role", "unknown"),
        "observes": [],
        "effects": [],
        "effects_by_operation": {},
        "needs_human_review": True,
        "notes": "No AI binding available or no clear physical channel inferred.",
    }


def normalize_direction(value: Any) -> str:
    s = str(value).strip() if value is not None else "unknown"
    if s in {"+1", "1", "+", "increase", "up"}:
        return "+1"
    if s in {"-1", "-", "decrease", "down"}:
        return "-1"
    if s in {"0", "none", "neutral", "no_change"}:
        return "0"
    return "unknown"


def normalize_channel_name(value: Any) -> str:
    """Normalize an AI-proposed channel name to lower_snake_case."""
    import re

    name = str(value or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def normalize_proposed_channels(raw: Any, existing_channels: List[str], device_ids: Set[str]) -> List[Dict[str, Any]]:
    """Validate AI-proposed channels without adopting them automatically."""
    if not isinstance(raw, list):
        return []
    existing = set(existing_channels)
    seen: Set[str] = set()
    proposals: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        channel = normalize_channel_name(item.get("channel"))
        if not channel or channel in existing or channel in seen:
            continue
        related = []
        for eid in item.get("related_entities", []) or []:
            if isinstance(eid, str) and eid in device_ids:
                related.append(eid)
        proposals.append(
            {
                "channel": channel,
                "description": str(item.get("description", "")),
                "reason": str(item.get("reason", "")),
                "related_entities": unique_list(related),
                "suggested_value_type": str(item.get("suggested_value_type", "unknown")),
                "confidence": float(item.get("confidence", 0) or 0),
                "status": "proposed_only_not_in_config",
            }
        )
        seen.add(channel)
    return proposals


def build_entity_display_list(devices: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "entity_id": e.get("entity_id"),
            "display_name": e.get("display_name", e.get("entity_id")),
            "original_name": (e.get("registry") or {}).get("original_name"),
            "name": (e.get("registry") or {}).get("name"),
        }
        for e in sorted(devices.get("entities", []), key=lambda x: x.get("entity_id", ""))
    ]


def validate_and_normalize_bindings(ai_data: Any, devices: Dict[str, Any], channels: List[str]) -> Dict[str, Any]:
    if not isinstance(ai_data, dict):
        raise ValueError("AI response must be a JSON object")
    raw_bindings = ai_data.get("bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("AI response must contain a bindings list")

    channel_set = set(channels)
    device_map = {e["entity_id"]: e for e in devices.get("entities", [])}
    out_map: Dict[str, Dict[str, Any]] = {eid: default_empty_binding(e) for eid, e in device_map.items()}

    # Record channels that AI attempted to use directly but are outside config.
    # They are not accepted into bindings, but are exposed as invalid_channel_mentions
    # to help the user decide whether config.json should be extended.
    invalid_mentions: Dict[str, Dict[str, Any]] = {}

    def mention_invalid(ch: Any, eid: str, location: str) -> None:
        name = normalize_channel_name(ch)
        if not name or name in channel_set:
            return
        rec = invalid_mentions.setdefault(
            name,
            {
                "channel": name,
                "mentioned_by_entities": [],
                "locations": [],
                "status": "mentioned_but_rejected_not_in_config",
            },
        )
        if eid not in rec["mentioned_by_entities"]:
            rec["mentioned_by_entities"].append(eid)
        if location not in rec["locations"]:
            rec["locations"].append(location)

    for item in raw_bindings:
        if not isinstance(item, dict):
            continue
        eid = item.get("entity_id")
        if eid not in device_map:
            continue
        binding = default_empty_binding(device_map[eid])
        role = item.get("role") or binding["role"]
        if role not in {"sensor", "actuator", "hybrid", "logical", "unknown"}:
            role = binding["role"]
        binding["role"] = role
        binding["observes"] = []
        for obs in item.get("observes", []) or []:
            if not isinstance(obs, dict):
                continue
            ch = obs.get("channel")
            if ch not in channel_set:
                mention_invalid(ch, eid, "observes")
                continue
            binding["observes"].append(
                {
                    "channel": ch,
                    "value_type": obs.get("value_type") or device_map[eid].get("value_type_hint", "unknown"),
                    "confidence": float(obs.get("confidence", 0) or 0),
                    "reason": str(obs.get("reason", "")),
                }
            )
        binding["effects"] = []
        for eff in item.get("effects", []) or []:
            if not isinstance(eff, dict):
                continue
            ch = eff.get("channel")
            if ch not in channel_set:
                mention_invalid(ch, eid, "effects")
                continue
            binding["effects"].append(
                {
                    "channel": ch,
                    "direction": normalize_direction(eff.get("direction")),
                    "operation": str(eff.get("operation", "default")),
                    "confidence": float(eff.get("confidence", 0) or 0),
                    "reason": str(eff.get("reason", "")),
                }
            )
        effects_by_operation: Dict[str, List[Dict[str, Any]]] = {}
        raw_ebo = item.get("effects_by_operation") or {}
        if isinstance(raw_ebo, dict):
            for op, effects in raw_ebo.items():
                valid_effects = []
                for eff in effects or []:
                    if not isinstance(eff, dict):
                        continue
                    ch = eff.get("channel")
                    if ch not in channel_set:
                        mention_invalid(ch, eid, f"effects_by_operation.{op}")
                        continue
                    valid_effects.append(
                        {
                            "channel": ch,
                            "direction": normalize_direction(eff.get("direction")),
                            "confidence": float(eff.get("confidence", 0) or 0),
                            "reason": str(eff.get("reason", "")),
                        }
                    )
                if valid_effects:
                    effects_by_operation[str(op)] = valid_effects
        binding["effects_by_operation"] = effects_by_operation
        binding["needs_human_review"] = bool(item.get("needs_human_review", True))
        binding["notes"] = str(item.get("notes", ""))
        out_map[eid] = binding

    proposed = normalize_proposed_channels(ai_data.get("proposed_channels", []), channels, set(device_map.keys()))

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "ai_with_validation",
        "candidate_channels": channels,
        "entity_display": build_entity_display_list(devices),
        "bindings": [out_map[eid] for eid in sorted(out_map)],
        "proposed_channels": proposed,
        "invalid_channel_mentions": sorted(invalid_mentions.values(), key=lambda x: x["channel"]),
    }


def bind_channels_with_ai(devices: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    channels = config.get("channels", [])
    prompt = config.get("system_prompts", {}).get("channels_binding") or DEFAULT_CHANNEL_BINDING_PROMPT
    payload = {
        "task": "Bind Home Assistant entities to physical channels for TCAE modeling.",
        "candidate_channels": channels,
        "devices": compact_devices_for_ai(devices),
        "output_language": "English for JSON keys; reasons may be concise English.",
    }
    ai_data = call_ai_json(prompt, payload, temperature=0.0)
    return validate_and_normalize_bindings(ai_data, devices, channels)


def build_empty_channels(devices: Dict[str, Any], config: Dict[str, Any], method: str = "empty_fallback") -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": method,
        "candidate_channels": config.get("channels", []),
        "entity_display": build_entity_display_list(devices),
        "bindings": [default_empty_binding(e) for e in sorted(devices.get("entities", []), key=lambda x: x["entity_id"])],
        "proposed_channels": [],
        "invalid_channel_mentions": [],
    }


def print_binding_review_hint(channels_data: Dict[str, Any], channels_path: str) -> None:
    print("\n[Channel Binding Summary]")
    display_map = {e.get("entity_id"): e.get("display_name", e.get("entity_id")) for e in channels_data.get("entity_display", []) if isinstance(e, dict)}
    for b in channels_data.get("bindings", []):
        observes = ", ".join(o["channel"] for o in b.get("observes", [])) or "-"
        ebo_parts = []
        for op, effects in (b.get("effects_by_operation") or {}).items():
            ebo_parts.append(op + ":" + ",".join(f"{e['channel']}({e['direction']})" for e in effects))
        effects = "; ".join(ebo_parts) or ", ".join(f"{e['channel']}({e['direction']})" for e in b.get("effects", [])) or "-"
        review = " REVIEW" if b.get("needs_human_review") else ""
        name = display_map.get(b["entity_id"], b["entity_id"])
        print(f"- {name} | role={b.get('role')} | observes={observes} | effects={effects}{review}")

    proposals = channels_data.get("proposed_channels", []) or []
    if proposals:
        print("\n[Proposed Channels]")
        print("AI 发现当前 config.json.channels 可能不足，以下 channel 仅作为建议，不会自动参与绑定。")
        print("如果确认需要，请手动加入 config.json 的 channels 后重新运行脚本 1。")
        for idx, p in enumerate(proposals, start=1):
            related = ", ".join(p.get("related_entities", [])) or "-"
            print(f"{idx}. {p.get('channel')} | confidence={p.get('confidence')} | value_type={p.get('suggested_value_type')}")
            print(f"   description: {p.get('description')}")
            print(f"   reason: {p.get('reason')}")
            print(f"   related_entities: {related}")

    invalids = channels_data.get("invalid_channel_mentions", []) or []
    if invalids:
        print("\n[Rejected Out-of-Config Channel Mentions]")
        print("AI 在绑定中直接使用了以下非候选 channel；脚本已拒绝这些绑定。可参考它们决定是否扩展 config.json.channels。")
        for item in invalids:
            ents = ", ".join(item.get("mentioned_by_entities", [])) or "-"
            locs = ", ".join(item.get("locations", [])) or "-"
            print(f"- {item.get('channel')} | entities={ents} | locations={locs}")

    print(f"\n已写入: {channels_path}")
    print("如需人工审核/修改 channel，请直接编辑 channels.json；后续 zone 与 TCAE 脚本会读取修改后的结果。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract entities and bind channels for HomeAgent.")
    parser.add_argument("--config", default=str(load_config.__defaults__[0]) if load_config.__defaults__ else None, help="Path to config.json")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI and write empty conservative channel bindings.")
    parser.add_argument("--keep-prompt", action="store_true", help="Do not auto-fill empty system_prompts.channels_binding in config.json.")
    args = parser.parse_args()

    # Use default config path when argparse default construction is awkward.
    config_path = args.config if args.config and args.config != "None" else None
    config = load_config(config_path) if config_path else load_config()
    if not args.keep_prompt and ensure_system_prompts(config):
        save_config(config, config_path) if config_path else save_config(config)
        print("已向 config.json 写入默认 channels_binding system prompt。")

    load_env()
    automations_path = get_input_path(config, "automations")
    registry_path = get_optional_input_path(config, "entity_registry", "./configurations/core.entity_registry")
    registry_map = load_entity_registry(registry_path)
    if registry_map:
        print(f"已加载实体名称映射: {registry_path}（{len(registry_map)} 条）")
    else:
        print("未发现可用的 core.entity_registry，用户可见名称将回退为 entity_id。")
    devices_path = get_output_path(config, "devices")
    channels_path = get_output_path(config, "channels")

    automations = normalize_automation_list(load_yaml(automations_path, default=[]))
    devices = extract_devices(automations, registry_map=registry_map)
    write_json(devices_path, devices)
    print(f"已提取 {devices['summary']['entity_count']} 个实体，写入: {devices_path}")

    if args.no_ai:
        channels_data = build_empty_channels(devices, config, method="empty_no_ai")
    else:
        try:
            channels_data = bind_channels_with_ai(devices, config)
        except Exception as exc:
            print(f"[警告] AI channel 绑定失败: {exc}")
            print("将写入保守空绑定。可之后手动编辑 channels.json 或重新运行脚本。")
            channels_data = build_empty_channels(devices, config, method="empty_ai_failed")

    write_json(channels_path, channels_data)
    print_binding_review_hint(channels_data, str(channels_path))


if __name__ == "__main__":
    main()
