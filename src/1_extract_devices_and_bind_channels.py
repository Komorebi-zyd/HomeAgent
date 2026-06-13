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
You are the channel binding and channel discovery module of HomeAgent. Your task is to infer bindings between Home Assistant entities and physical/environmental channels for TCAE modeling.

Core ontology clarification:
1. In HomeAgent, a channel is a physical or environmental process dimension in a zone, i.e. something like y(zone, channel) in the environment state model. A channel is NOT the device's own on/off/open/closed/locked/unlocked/status value.
2. Valid channel examples include temperature, humidity, pressure, light, sound, motion, smoke, water_flow, air_quality, CO2, gas_concentration, air_flow, or other measurable/propagating environmental/resource-flow dimensions.
3. Invalid channel examples include openness, lock_state, door_state, window_state, access, security, privacy, permission, mode, scene, status, charging_status, auto_close_enabled, child_lock, normality, or any other value that mainly describes an entity's discrete internal state or automation helper state.
4. Door/window/lock/latch entities have important entity states, but those states belong to entity-state modeling x(t), not to environmental channel modeling y(t). Do NOT propose channels such as openness, lock_state, access, or security for them.
5. A door/window may affect an environmental channel only when a specific supplied rule context supports a real physical/environmental effect on a candidate channel, e.g. a high-temperature rule opening a window may be modeled as reducing temperature. If the physical effect and direction are not supported by a specific rule context, leave effects empty rather than inventing a generic side effect.
6. Logical helpers, mode switches, permission flags, buttons, timers, status flags, and scene switches usually have no observes/effects because they model automation context, not physical environmental channels.

Important principles:
1. Do NOT rely on any specific human language or hard-coded words. Entity object names may be Chinese pinyin, Japanese romanization, Spanish, English, abbreviations, or arbitrary user text. Treat names/descriptions/aliases/display names as useful semantic hints, but ground decisions in the supplied structured rule contexts whenever possible.
2. The platform/system variables you may rely on directly are Home Assistant domains, service names, service operations, trigger/condition/action positions, structured YAML fields, entity registry metadata, entity rule contexts, and the supplied related rule skeletons.
3. For entity bindings, use ONLY the candidate channel list supplied by the user. If a useful environmental channel is not in the candidate list, do NOT put it into observes/effects/effects_by_rule/effects_by_operation. Instead, add it to proposed_channels only if it satisfies the channel ontology and explicit-context requirement below.
4. proposed_channels must also be physical/environmental channels. Never propose device-state/control-state channels such as openness, lock_state, door_state, window_state, access, security, permission, mode, status, charging_status, or auto_close_enabled.
5. Do NOT propose a new channel merely because a device may have a generic physical side effect. Propose a new channel only when the supplied rule trigger/condition/action semantics explicitly depend on that missing physical/environmental dimension and none of the candidate_channels can express it.
6. Multi-channel effects are allowed when physically meaningful and rule-supported. For example, an air conditioner may decrease temperature in a cooling rule but increase temperature in a heating rule if the supplied rule context shows that behavior.
7. Action effects are rule-scoped. The direction of an actuator's channel effect must be determined under the specific rule where the action appears, not globally by device type or operation alone. The same entity and even the same operation may have different directions in different rules if the rule context shows different intended physical effects.
8. For actuator effects, prefer effects_by_rule. Legacy effects/effects_by_operation may be left empty unless the effect is genuinely rule-independent and direction is stable across rules.
9. If no physical/environmental channel or no rule-supported direction is clear, return empty effects/effects_by_rule/effects_by_operation for that relation and set needs_human_review=true only if the channel binding uncertainty matters.
10. Return strict JSON only. Do not include markdown or commentary.

Parameter definitions:
1. entity_id:
   - Must be exactly one entity_id from the supplied devices list.
   - Do not invent entities and do not output entities that are absent from the supplied context.
2. role:
   - Backward-compatible semantic role for this channel-binding task.
   - sensor: The entity observes, measures, detects, reports, or represents a physical/environmental condition used by triggers/conditions. Example: a temperature sensor observing temperature, a smoke sensor observing smoke, a water-flow sensor observing water_flow.
   - actuator: The entity is a target of Home Assistant service actions and can change a device state, a physical process, or an automation-relevant control state. It should have effects only if its action changes a physical/environmental channel in a specific rule context; otherwise effects should be empty.
   - hybrid: The entity is both observable and controllable in the supplied context, or it appears on both observation side and action target side.
   - logical: The entity mainly represents a virtual mode, helper, button, timer, permission flag, status flag, scene selector, or automation context rather than directly observing/affecting a physical/environmental channel. Logical entities usually have empty observes/effects.
   - unknown: Use only when the supplied context is insufficient to infer a reliable role.
3. structural_role and semantic_role:
   - structural_role is supplied/validated by the program from trigger/condition/action positions and does not need to be invented.
   - semantic_role may be returned if useful; it uses the same values as role and should describe whether the entity is physical-channel-related or logical in this binding step.
   - needs_human_review in this script is about channel-binding uncertainty, not about whether an entity is safety-critical. Safety/normal-state review is handled by later steps.
4. channel:
   - Must be exactly one item from candidate_channels when used inside observes, effects, effects_by_rule, or effects_by_operation.
   - A channel is a physical/environmental dimension of a zone or resource-flow process, such as temperature, humidity, pressure, light, sound, motion, smoke, or water_flow.
   - A channel is not an entity's own state. Do not use or propose channels for open/closed, locked/unlocked, enabled/disabled, on/off status, mode, scene, permission, or automation state.
   - If the desired physical/environmental dimension is absent from candidate_channels, use proposed_channels only if the missing dimension is explicitly required by the supplied rule semantics and is truly environmental/resource-flow, not device-state.
5. value_type for observes:
   - numeric: The observed channel value is numeric or threshold-comparable, e.g., temperature value, humidity percentage, illuminance, water flow rate, pressure, sound level, smoke concentration.
   - state: The observed channel value is a finite environmental state, e.g., smoke detected/clear, motion detected/clear. Do NOT use state merely to encode a device's own open/closed/locked/unlocked status as a channel.
   - event: The entity represents an instantaneous or discrete event related to an environmental channel, e.g., smoke alarm triggered or motion event. Use only when event semantics are clear.
   - datetime: The entity represents a date, time, timestamp, schedule point, or time helper. Datetime entities are usually logical and often have no channel observes/effects.
   - select: The entity represents a choice from a finite option set, e.g., mode selector or input_select option. Select entities are usually logical and often have no channel observes/effects.
   - unknown: Use only when the supplied context does not support a reliable value type.
6. direction for effects_by_rule / effects / effects_by_operation:
   - +1: In this specific rule context, the action tends to increase, intensify, produce, activate, or make more likely the corresponding physical/environmental channel value. Examples: heater turn_on in a heating rule -> temperature +1; light turn_on -> light +1; humidifier turn_on -> humidity +1; sprinkler/pump turn_on -> water_flow +1.
   - -1: In this specific rule context, the action tends to decrease, reduce, suppress, remove, or make less likely the corresponding physical/environmental channel value. Examples: air conditioner turn_on in a cooling rule -> temperature -1; light turn_off -> light -1; dehumidifier turn_on -> humidity -1; exhaust fan turn_on -> smoke -1 or humidity -1 when supported; valve close -> water_flow -1.
   - 0: The operation is known to have no meaningful physical effect on that channel, or it keeps the channel effectively unchanged. Use 0 sparingly; if there is no relation, omit the effect instead of outputting direction 0.
   - unknown: There is a plausible physical/environmental relation to the channel, but the direction cannot be inferred from the supplied rule context. Prefer omitting that effect instead of outputting unknown. Use unknown only when the relation is explicitly present in the rule context and must be preserved for human review.
7. operation:
   - The Home Assistant service operation associated with the rule action, such as turn_on, turn_off, set_value, open, close, lock, unlock, press, start, stop, or another operation found in the supplied action context.
   - Use default only when the effect is operation-insensitive or the operation is not known.
8. observes:
   - Use only when an entity reports or detects a physical/environmental candidate channel.
   - Do not add observes for a helper/mode/status/permission entity unless it genuinely observes a candidate physical/environmental channel.
   - Do not add observes just because an entity has an observable device state such as open/closed, locked/unlocked, enabled/disabled, on/off, or charging/not_charging.
9. effects_by_rule:
   - Preferred structure for actuator effects.
   - It maps each related rule_uid to the physical/environmental channel effects caused by this entity's action in that specific rule.
   - The key must be a rule_uid from the supplied related rules for the entity.
   - Each effect should include channel, direction, operation, service, post_value if available, confidence, and reason.
   - The reason should explain why the rule context implies that direction. Example: a rule triggered by high temperature and opening a window may justify temperature -1 as the intended effect.
10. effects and effects_by_operation:
   - Legacy/global structures. Use only for stable rule-independent effects. Prefer leaving them empty when direction depends on rule context.
   - If these are provided together with effects_by_rule, they must not contradict effects_by_rule.
11. confidence:
   - A number from 0.0 to 1.0.
   - 0.9-1.0: strongly supported by structured rule context.
   - 0.7-0.89: likely but with minor ambiguity.
   - 0.4-0.69: plausible but needs human review.
   - below 0.4: weak inference; usually prefer empty binding or proposed_channels with needs_human_review=true.
12. needs_human_review:
   - true when the role, channel, value_type, rule-specific direction, or proposed channel is uncertain or ambiguous for this channel-binding step.
   - false only when the supplied structured context strongly supports the binding.
   - Do not set true merely because a device is safety/security sensitive; safety-sensitive normal-state review belongs to later steps.
13. notes/reason:
   - Keep concise, concrete, and grounded in supplied rule contexts.
   - Entity names and aliases may be used as semantic hints, but for effects_by_rule the reason should mention the relevant rule trigger/condition/action semantics whenever possible.
14. proposed_channels:
   - Use only for missing physical/environmental or resource-flow dimensions not covered by candidate_channels.
   - The proposed channel name should be lower_snake_case.
   - proposed_channels must not be used directly in observes/effects/effects_by_rule/effects_by_operation in the same response.
   - Do not propose channels that represent entity state, device state, security state, access state, lock state, door/window openness, helper status, permission flags, or automation modes.
   - Energy/electricity channels may be proposed only if the context contains explicit measurement or physical load/flow semantics. Do not propose electric_power merely because an entity is an on/off charger status helper.
   - Do not propose more fine-grained channels such as air_flow merely because of generic physical common sense. Propose them only if the supplied rule semantics explicitly require that missing dimension and existing candidate_channels cannot express the rule relation.

Output schema:
{
  "bindings": [
    {
      "entity_id": "string; must be one supplied entity_id",
      "role": "sensor|actuator|hybrid|logical|unknown",
      "semantic_role": "sensor|actuator|hybrid|logical|unknown",
      "observes": [
        {
          "channel": "one candidate channel only",
          "value_type": "numeric|state|event|datetime|select|unknown",
          "confidence": 0.0,
          "reason": "brief reason grounded in supplied context"
        }
      ],
      "effects_by_rule": {
        "automation.example_rule_uid": [
          {
            "channel": "one candidate channel only",
            "direction": "+1|-1|0|unknown",
            "operation": "Home Assistant service operation for that rule action",
            "service": "full Home Assistant service name if available",
            "post_value": "action post-state if available",
            "confidence": 0.0,
            "reason": "brief reason grounded in that rule's trigger/condition/action context"
          }
        ]
      },
      "effects": [],
      "effects_by_operation": {},
      "needs_human_review": true,
      "notes": "brief notes"
    }
  ],
  "proposed_channels": [
    {
      "channel": "lower_snake_case_new_environmental_channel_name",
      "description": "what physical/environmental or resource-flow dimension this channel represents",
      "reason": "which supplied rule semantics explicitly require this missing environmental dimension and why candidate_channels cannot express it",
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


def compact_devices_for_ai(devices: Dict[str, Any], rules_by_uid: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    rules_by_uid = rules_by_uid or {}
    for e in devices.get("entities", []):
        contexts = []
        for ctx in e.get("raw_contexts", [])[:12]:
            contexts.append(
                {
                    "rule_uid": ctx.get("rule_uid"),
                    "rule_alias": ctx.get("rule_alias"),
                    "rule_description": ctx.get("rule_description"),
                    "section": ctx.get("section"),
                    "yaml_path": ctx.get("yaml_path"),
                    "platform": ctx.get("platform"),
                    "condition": ctx.get("condition"),
                    "service": ctx.get("service"),
                    "operation": ctx.get("operation"),
                    "post_value": ctx.get("post_value"),
                    "node_excerpt": ctx.get("node_excerpt"),
                }
            )
        related_rules = []
        for rule_uid in e.get("rules", [])[:12]:
            if rule_uid in rules_by_uid:
                related_rules.append(rules_by_uid[rule_uid])
        compact.append(
            {
                "entity_id": e["entity_id"],
                "display_name": e.get("display_name", e["entity_id"]),
                "registry": e.get("registry", {}),
                "domain": e["domain"],
                "value_type_hint": e.get("value_type_hint"),
                "positions": e.get("positions", []),
                "structural_role": e.get("primary_role"),
                "primary_role": e.get("primary_role"),
                "operations": e.get("operations", []),
                "contexts": contexts,
                "related_rules": related_rules,
            }
        )
    return compact


def compact_rule_for_ai(rule: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """Build a compact Home Assistant rule skeleton for AI channel binding.

    Step 1 does not have TCAE yet, so this keeps the original HA structure while
    exposing action operation/post-state metadata needed for rule-scoped effects.
    """
    rule_uid = make_rule_uid(rule, idx)
    triggers = []
    for t_idx, trigger in enumerate(listify(rule.get("trigger"))):
        if isinstance(trigger, dict):
            triggers.append(
                {
                    "index": t_idx,
                    "platform": trigger.get("platform"),
                    "entity_refs": [eid for eid, _ in traverse_entity_references(trigger, f"trigger[{t_idx}]")],
                    "node_excerpt": compact_node(trigger),
                }
            )
    conditions = []
    for c_idx, condition in enumerate(listify(rule.get("condition"))):
        if isinstance(condition, dict):
            conditions.append(
                {
                    "index": c_idx,
                    "condition": condition.get("condition"),
                    "entity_refs": [eid for eid, _ in traverse_entity_references(condition, f"condition[{c_idx}]")],
                    "node_excerpt": compact_node(condition),
                }
            )
    actions = []
    for a_idx, action in enumerate(listify(rule.get("action"))):
        if isinstance(action, dict):
            data = action.get("data") if isinstance(action.get("data"), dict) else {}
            actions.append(
                {
                    "index": a_idx,
                    "service": action.get("service"),
                    "operation": service_operation(action.get("service")),
                    "post_value": post_value_from_service(action.get("service"), data),
                    "target_entities": collect_action_targets(action),
                    "entity_refs": [eid for eid, _ in traverse_entity_references(action, f"action[{a_idx}]")],
                    "node_excerpt": compact_node(action),
                }
            )
    return {
        "rule_uid": rule_uid,
        "rule_id": rule.get("id"),
        "alias": rule.get("alias"),
        "description": rule.get("description"),
        "mode": rule.get("mode"),
        "entity_refs": unique_list([eid for eid, _ in traverse_entity_references(rule, "rule")]),
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
    }


def compact_rules_for_ai(automations: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not automations:
        return []
    return [compact_rule_for_ai(rule, idx) for idx, rule in enumerate(automations, start=1) if isinstance(rule, dict)]

def default_empty_binding(entity: Dict[str, Any]) -> Dict[str, Any]:
    structural_role = entity.get("primary_role", "unknown")
    return {
        "entity_id": entity["entity_id"],
        # Backward-compatible semantic role used by older scripts.
        "role": structural_role,
        # Structural role is derived from trigger/condition/action positions.
        "structural_role": structural_role,
        # Semantic role may be adjusted by AI for channel-binding purposes.
        "semantic_role": structural_role,
        "observes": [],
        # Legacy/global effects. Rule-scoped effects should use effects_by_rule.
        "effects": [],
        "effects_by_operation": {},
        "effects_by_rule": {},
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


def build_rule_display_list(devices: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "rule_uid": r.get("rule_uid"),
            "rule_id": r.get("rule_id"),
            "alias": r.get("alias"),
            "description": r.get("description"),
        }
        for r in devices.get("rules", [])
        if isinstance(r, dict)
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
        allowed_roles = {"sensor", "actuator", "hybrid", "logical", "unknown"}
        structural_role = device_map[eid].get("primary_role", "unknown")
        if structural_role not in {"sensor", "actuator", "hybrid", "unknown"}:
            structural_role = "unknown"
        semantic_role = item.get("semantic_role") or item.get("role") or binding.get("semantic_role") or binding["role"]
        if semantic_role not in allowed_roles:
            semantic_role = binding.get("semantic_role", binding["role"])
        binding["structural_role"] = structural_role
        binding["semantic_role"] = semantic_role
        # Keep role for backward compatibility; it now mirrors semantic_role.
        binding["role"] = semantic_role
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

        effects_by_rule: Dict[str, List[Dict[str, Any]]] = {}
        valid_rule_uids = set(device_map[eid].get("rules", []))

        def append_rule_effect(rule_uid: Any, eff: Dict[str, Any], location: str) -> None:
            if not isinstance(rule_uid, str) or rule_uid not in valid_rule_uids:
                return
            ch = eff.get("channel")
            if ch not in channel_set:
                mention_invalid(ch, eid, location)
                return
            effect_record = {
                "channel": ch,
                "direction": normalize_direction(eff.get("direction")),
                "operation": str(eff.get("operation", "default")),
                "service": eff.get("service"),
                "post_value": eff.get("post_value"),
                "confidence": float(eff.get("confidence", 0) or 0),
                "reason": str(eff.get("reason", "")),
            }
            effects_by_rule.setdefault(rule_uid, []).append(effect_record)

        raw_ebr = item.get("effects_by_rule") or {}
        if isinstance(raw_ebr, dict):
            for rule_uid, effects in raw_ebr.items():
                for eff in effects or []:
                    if isinstance(eff, dict):
                        append_rule_effect(rule_uid, eff, f"effects_by_rule.{rule_uid}")

        # Also accept list-style rule_effects for robustness, but normalize to effects_by_rule.
        raw_rule_effects = item.get("rule_effects") or []
        if isinstance(raw_rule_effects, list):
            for eff in raw_rule_effects:
                if isinstance(eff, dict):
                    append_rule_effect(eff.get("rule_uid"), eff, "rule_effects")

        binding["effects_by_rule"] = effects_by_rule
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
        "rule_display": build_rule_display_list(devices),
        "bindings": [out_map[eid] for eid in sorted(out_map)],
        "proposed_channels": proposed,
        "invalid_channel_mentions": sorted(invalid_mentions.values(), key=lambda x: x["channel"]),
    }


def bind_channels_with_ai(
    devices: Dict[str, Any],
    config: Dict[str, Any],
    automations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    channels = config.get("channels", [])
    prompt = config.get("system_prompts", {}).get("channels_binding") or DEFAULT_CHANNEL_BINDING_PROMPT
    compact_rules = compact_rules_for_ai(automations)
    rules_by_uid = {r["rule_uid"]: r for r in compact_rules}
    payload = {
        "task": "Bind Home Assistant entities to physical/environmental channels for TCAE modeling. Use rule-scoped effects_by_rule for actuator directions.",
        "candidate_channels": channels,
        "schema_notes": {
            "channel_ontology": "channels are environmental/resource-flow dimensions, not entity states",
            "preferred_actuator_effect_schema": "effects_by_rule",
            "direction_scope": "direction is determined under each specific rule context, not globally by device type or operation alone",
            "proposed_channel_policy": "propose only explicitly required missing environmental channels; do not propose generic finer-grained channels from common sense alone",
        },
        "rules": compact_rules,
        "devices": compact_devices_for_ai(devices, rules_by_uid=rules_by_uid),
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
        "rule_display": build_rule_display_list(devices),
        "bindings": [default_empty_binding(e) for e in sorted(devices.get("entities", []), key=lambda x: x["entity_id"])],
        "proposed_channels": [],
        "invalid_channel_mentions": [],
    }


def print_binding_review_hint(channels_data: Dict[str, Any], channels_path: str) -> None:
    print("\n[Channel Binding Summary]")
    display_map = {
        e.get("entity_id"): e.get("display_name", e.get("entity_id"))
        for e in channels_data.get("entity_display", [])
        if isinstance(e, dict)
    }
    rule_map = {
        r.get("rule_uid"): r
        for r in channels_data.get("rule_display", [])
        if isinstance(r, dict) and r.get("rule_uid")
    }

    def rule_label(rule_uid: str) -> str:
        info = rule_map.get(rule_uid, {})
        alias = info.get("alias")
        if alias:
            return f"{alias} [{rule_uid}]"
        return rule_uid

    def fmt_observes(binding: Dict[str, Any]) -> str:
        parts = []
        for obs in binding.get("observes", []) or []:
            channel = obs.get("channel", "?")
            value_type = obs.get("value_type", "unknown")
            conf = obs.get("confidence")
            suffix = f"/{value_type}"
            if conf not in (None, ""):
                suffix += f"/conf={conf}"
            parts.append(f"{channel}({suffix.lstrip('/')})")
        return ", ".join(parts) or "-"

    def fmt_effect(effect: Dict[str, Any]) -> str:
        channel = effect.get("channel", "?")
        direction = effect.get("direction", "unknown")
        fields = [f"channel={channel}", f"direction={direction}"]
        operation = effect.get("operation")
        service = effect.get("service")
        post_value = effect.get("post_value")
        confidence = effect.get("confidence")
        if operation:
            fields.append(f"op={operation}")
        if service:
            fields.append(f"service={service}")
        if post_value is not None:
            fields.append(f"post={post_value}")
        if confidence not in (None, ""):
            fields.append(f"conf={confidence}")
        return " | ".join(fields)

    for b in channels_data.get("bindings", []):
        if not isinstance(b, dict):
            continue
        entity_id = b.get("entity_id", "")
        name = display_map.get(entity_id, entity_id)
        semantic_role = b.get("semantic_role", b.get("role"))
        structural_role = b.get("structural_role")
        role_text = f"role={semantic_role}"
        if structural_role and structural_role != semantic_role:
            role_text += f" | structural_role={structural_role}"
        observes = fmt_observes(b)
        review = " REVIEW" if b.get("needs_human_review") else ""
        print(f"- {name} | {role_text} | observes={observes}{review}")

        effects_by_rule = b.get("effects_by_rule") or {}
        if effects_by_rule:
            print("  rule_effects:")
            for rule_uid in sorted(effects_by_rule):
                effects = effects_by_rule.get(rule_uid) or []
                if not effects:
                    continue
                print(f"    * {rule_label(rule_uid)}")
                for eff in effects:
                    if not isinstance(eff, dict):
                        continue
                    print(f"      - {fmt_effect(eff)}")
                    reason = str(eff.get("reason", "")).strip()
                    if reason:
                        print(f"        reason: {reason}")
        else:
            # Legacy/global display is kept for backward compatibility, but rule_effects
            # is the preferred rule-scoped semantics after this script update.
            legacy_parts = []
            for op, effects in (b.get("effects_by_operation") or {}).items():
                for eff in effects or []:
                    if isinstance(eff, dict):
                        legacy_parts.append(f"operation[{op}] -> {fmt_effect(eff)}")
            for eff in b.get("effects", []) or []:
                if isinstance(eff, dict):
                    legacy_parts.append(f"global -> {fmt_effect(eff)}")
            if legacy_parts:
                print("  legacy_effects:")
                for part in legacy_parts:
                    print(f"    - {part}")
            else:
                print("  rule_effects: -")

    proposals = channels_data.get("proposed_channels", []) or []
    if proposals:
        print("\n[Proposed Channels]")
        print("AI 发现当前 config.json.channels 可能不足，以下 channel 仅作为建议，不会自动参与绑定。")
        print("注意：只有当规则语义明确依赖缺失的环境通道，且现有 candidate_channels 无法表达时，才建议扩展 channel。")
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
            channels_data = bind_channels_with_ai(devices, config, automations=automations)
        except Exception as exc:
            print(f"[警告] AI channel 绑定失败: {exc}")
            print("将写入保守空绑定。可之后手动编辑 channels.json 或重新运行脚本。")
            channels_data = build_empty_channels(devices, config, method="empty_ai_failed")

    write_json(channels_path, channels_data)
    print_binding_review_hint(channels_data, str(channels_path))


if __name__ == "__main__":
    main()
