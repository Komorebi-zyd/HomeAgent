"""
Step 4: Generate and review entity normal-state configuration.

Outputs:
- configurations/home/normal_config.json

This script prepares the entity normal configuration used by Step 5. It can use
AI to propose safety-/security-/resource-sensitive entities and their normal
states, then lets the user review and modify the result in a command-line flow.

Design principles:
- No system path, IP, API key, or model name is hard-coded. Paths come from
  configurations/config.json; AI settings come from .env / environment variables.
- No entity semantics are inferred from user-defined object_id substrings. The
  script sends Home Assistant domains, services, rule contexts, display names,
  channel bindings, zone bindings and TCAE actions to AI as semantic context.
- The AI proposal is treated as a draft. The user can keep, edit, drop, or add
  normal-state entries manually.
- The output schema is intentionally simple for Step 5:
  normal_entities[entity_id].normal_values is the authoritative normal-state set.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common import (
    HomeAgentError,
    call_ai_json,
    entity_display_name,
    get_input_path,
    get_optional_input_path,
    get_output_path,
    listify,
    load_config,
    load_entity_registry,
    load_env,
    load_json,
    prompt_yes_no,
    save_config,
    unique_list,
    utc_now_iso,
    write_json,
)


DEFAULT_NORMAL_CONFIG_PROMPT = r"""
You are the entity-normality configuration module of HomeAgent. Your task is to propose which Home Assistant entities should have a normal/safe/preferred state for detecting unexpected state transitions in a smart-home automation system.

Important principles:
1. Do NOT rely on any specific human language or hard-coded words. Entity object names may be Chinese pinyin, Japanese romanization, Spanish, English, abbreviations, or arbitrary user text. Treat names, aliases, descriptions and display names as semantic hints only.
2. The only platform/system variables you may rely on directly are Home Assistant domains, service operations, trigger/condition/action positions, channel bindings, zone bindings, TCAE actions, and rule contexts.
3. Select only entities for which a normal state is meaningful for safety, security, resource availability, privacy, or critical service continuity. Do not assign normal states to every comfort device by default.
4. A normal state is not simply the most common state. It is the state that should be preserved unless there is clear user intent or a legitimate automation context to leave it.
5. Use action post-states in TCAE to ensure the normal_values are comparable with the model. Typical post-state values are "on", "off", numeric values, or options. If uncertain, use needs_human_review=true and lower confidence.
6. If an entity has no clear normal state, omit it. If several states are acceptable, include all of them in normal_values.
7. Pay special attention to entities that can affect entry/access, fire response, water/electric/gas supply, privacy, physical openness, emergency availability, or other critical resources, but do not assume this from object_id words alone.
8. Return strict JSON only. Do not include markdown or commentary.

Output schema:
{
  "normal_entities": [
    {
      "entity_id": "string, must be one candidate entity_id",
      "normal_values": ["on or off or numeric/string value"],
      "abnormal_values": ["optional known abnormal values"],
      "category": "security|safety|resource|privacy|comfort|logical|other",
      "safety_level": "low|medium|high|critical",
      "confidence": 0.0,
      "reason": "brief reason grounded in the supplied rule/context information",
      "needs_human_review": true
    }
  ]
}
""".strip()


NORMAL_VALUE_KEYS = [
    "normal_values",
    "normal_value",
    "normal_states",
    "normal_state",
    "expected_values",
    "expected_value",
    "expected_states",
    "expected_state",
    "preferred_values",
    "preferred_value",
    "preferred_states",
    "preferred_state",
    "safe_values",
    "safe_value",
    "safe_states",
    "safe_state",
    "allowed_values",
    "allowed_value",
    "values",
]

VALID_CATEGORIES = {"security", "safety", "resource", "privacy", "comfort", "logical", "other"}
VALID_LEVELS = {"low", "medium", "high", "critical"}


# ---------------------------------------------------------------------------
# Config / prompt
# ---------------------------------------------------------------------------

def ensure_system_prompts(config: Dict[str, Any]) -> bool:
    prompts = config.setdefault("system_prompts", {})
    if not prompts.get("normal_config"):
        prompts["normal_config"] = DEFAULT_NORMAL_CONFIG_PROMPT
        return True
    return False


# ---------------------------------------------------------------------------
# Generic value helpers
# ---------------------------------------------------------------------------

def normalize_state_like(value: Any) -> Any:
    """Normalize generic platform-level state spellings.

    This does not infer entity semantics from entity names. It only normalizes
    common state values so user input, AI output and TCAE post-values can match.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        mapping = {
            "true": "on",
            "false": "off",
            "yes": "on",
            "no": "off",
            "enabled": "on",
            "enable": "on",
            "disabled": "off",
            "disable": "off",
            "open": "on",
            "opened": "on",
            "closed": "off",
            "close": "off",
            "locked": "off",
            "lock": "off",
            "unlocked": "on",
            "unlock": "on",
            "active": "on",
            "inactive": "off",
        }
        return mapping.get(v, v)
    return value


def parse_user_value(raw: str) -> Any:
    text = raw.strip()
    if text == "":
        return ""
    lower = text.lower()
    if lower in {"none", "null"}:
        return None
    if lower in {"true", "false", "on", "off", "open", "closed", "locked", "unlocked", "enabled", "disabled"}:
        return normalize_state_like(text)
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return normalize_state_like(text)


def parse_value_list(raw: str) -> List[Any]:
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    return [parse_user_value(p) for p in parts]


def normalize_values(values: Any) -> List[Any]:
    out = []
    for value in listify(values):
        v = normalize_state_like(value)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        out.append(v)
    return unique_list(out)


# ---------------------------------------------------------------------------
# Loading maps and compact context
# ---------------------------------------------------------------------------

def load_device_map(devices_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e["entity_id"]: e for e in devices_data.get("entities", []) if isinstance(e, dict) and e.get("entity_id")}


def load_channel_binding_map(channels_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {b["entity_id"]: b for b in channels_data.get("bindings", []) if isinstance(b, dict) and b.get("entity_id")}


def load_zone_binding_map(zones_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return dict(zones_data.get("bindings") or {})


def collect_action_targets_from_tcae(tcae: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Collect entities that appear as action targets in TCAE."""
    targets: Dict[str, Dict[str, Any]] = {}
    for rule in tcae.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        for action in rule.get("A", []) or []:
            if not isinstance(action, dict):
                continue
            eid = action.get("target_entity")
            if not eid:
                continue
            rec = targets.setdefault(
                eid,
                {
                    "entity_id": eid,
                    "post_values": [],
                    "operations": [],
                    "rules": [],
                    "actions": [],
                },
            )
            if action.get("post") not in rec["post_values"]:
                rec["post_values"].append(action.get("post"))
            if action.get("operation") not in rec["operations"]:
                rec["operations"].append(action.get("operation"))
            rule_info = {
                "rule_uid": rule.get("rule_uid"),
                "alias": rule.get("alias") or rule.get("display_alias"),
                "description": rule.get("description"),
            }
            if rule_info not in rec["rules"]:
                rec["rules"].append(rule_info)
            rec["actions"].append(
                {
                    "operation": action.get("operation"),
                    "service": action.get("service"),
                    "post": action.get("post"),
                    "data": action.get("data"),
                    "rule_uid": rule.get("rule_uid"),
                    "rule_alias": rule.get("alias") or rule.get("display_alias"),
                }
            )
    return dict(sorted(targets.items()))


def entity_candidate_summary(
    entity_id: str,
    device_map: Dict[str, Dict[str, Any]],
    channel_map: Dict[str, Dict[str, Any]],
    zone_map: Dict[str, Dict[str, Any]],
    action_targets: Dict[str, Dict[str, Any]],
    registry_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    device = device_map.get(entity_id, {})
    binding = channel_map.get(entity_id, {})
    zone = zone_map.get(entity_id, {})
    target = action_targets.get(entity_id, {})
    return {
        "entity_id": entity_id,
        "display_name": device.get("display_name") or entity_display_name(entity_id, registry_map or {}),
        "domain": device.get("domain"),
        "value_type_hint": device.get("value_type_hint"),
        "positions": device.get("positions", []),
        "primary_role": device.get("primary_role"),
        "operations_in_rules": target.get("operations", device.get("operations", [])),
        "possible_post_values_from_tcae": target.get("post_values", []),
        "channel_binding": {
            "role": binding.get("role"),
            "observes": binding.get("observes", []),
            "effects": binding.get("effects", []),
            "effects_by_operation": binding.get("effects_by_operation", {}),
        },
        "zone_binding": {
            "source_zones": zone.get("source_zones", []),
            "reachable_zones": zone.get("reachable_zones", []),
        },
        "rules": target.get("rules", [])[:8],
        "actions": target.get("actions", [])[:12],
        "raw_contexts": (device.get("raw_contexts") or [])[:6],
    }


def build_ai_context(
    devices_data: Dict[str, Any],
    channels_data: Dict[str, Any],
    zones_data: Dict[str, Any],
    tcae: Dict[str, Any],
    include_all_entities: bool = False,
    registry_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    device_map = load_device_map(devices_data)
    channel_map = load_channel_binding_map(channels_data)
    zone_map = load_zone_binding_map(zones_data)
    action_targets = collect_action_targets_from_tcae(tcae)

    if include_all_entities:
        candidate_ids = sorted(device_map.keys())
    else:
        # Normal states are most useful for entities that can be changed by actions.
        # Observation-only entities are omitted by default, but the user can still
        # add them manually during review.
        candidate_ids = sorted(action_targets.keys())

    candidates = [
        entity_candidate_summary(eid, device_map, channel_map, zone_map, action_targets, registry_map)
        for eid in candidate_ids
    ]

    return {
        "task": "Propose entity normal-state configuration for detecting weak unexpected post-states in HomeAgent.",
        "candidate_entities": candidates,
        "rule_count": len(tcae.get("rules", []) or []),
        "notes": [
            "Only propose entities whose normal state matters for safety/security/resource/privacy/critical service continuity.",
            "Use possible_post_values_from_tcae when choosing normal_values so later static pruning can compare values directly.",
            "If uncertain, set needs_human_review=true and explain why.",
        ],
    }


# ---------------------------------------------------------------------------
# AI validation and fallback
# ---------------------------------------------------------------------------

def extract_ai_entities(ai_data: Any) -> List[Dict[str, Any]]:
    if not isinstance(ai_data, dict):
        raise ValueError("AI response must be a JSON object")
    raw = ai_data.get("normal_entities")
    if raw is None:
        raw = ai_data.get("entities")
    if isinstance(raw, dict):
        out = []
        for eid, rec in raw.items():
            if isinstance(rec, dict):
                item = dict(rec)
                item.setdefault("entity_id", eid)
                out.append(item)
            else:
                out.append({"entity_id": eid, "normal_values": listify(rec)})
        return out
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    raise ValueError("AI response must contain normal_entities as list or object")


def first_present(record: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def normalize_normal_entry(item: Dict[str, Any], candidate_entity_ids: Set[str]) -> Optional[Dict[str, Any]]:
    eid = str(item.get("entity_id") or "").strip()
    if eid not in candidate_entity_ids:
        return None
    normal_values = normalize_values(first_present(item, NORMAL_VALUE_KEYS))
    if not normal_values:
        return None
    abnormal_values = normalize_values(item.get("abnormal_values", []))
    category = str(item.get("category") or "other").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "other"
    level = str(item.get("safety_level") or item.get("level") or "medium").strip().lower()
    if level not in VALID_LEVELS:
        level = "medium"
    try:
        confidence = float(item.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return {
        "entity_id": eid,
        "normal_values": normal_values,
        "abnormal_values": abnormal_values,
        "category": category,
        "safety_level": level,
        "confidence": confidence,
        "reason": str(item.get("reason") or item.get("notes") or ""),
        "needs_human_review": bool(item.get("needs_human_review", True)),
        "source": item.get("source") or "ai",
    }


def validate_ai_normal_config(ai_data: Any, candidate_entity_ids: Set[str]) -> Dict[str, Any]:
    entries: Dict[str, Dict[str, Any]] = {}
    for item in extract_ai_entities(ai_data):
        rec = normalize_normal_entry(item, candidate_entity_ids)
        if rec:
            entries[rec["entity_id"]] = rec
    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "ai_with_human_review_pending",
        "normal_entities": dict(sorted(entries.items())),
    }


def build_empty_normal_config(method: str = "empty_no_ai") -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": method,
        "normal_entities": {},
    }


def propose_normal_config_with_ai(context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (config.get("system_prompts") or {}).get("normal_config") or DEFAULT_NORMAL_CONFIG_PROMPT
    ai_data = call_ai_json(prompt, context, temperature=0.0)
    candidate_ids = {e["entity_id"] for e in context.get("candidate_entities", []) if e.get("entity_id")}
    return validate_ai_normal_config(ai_data, candidate_ids)


# ---------------------------------------------------------------------------
# Human review CLI
# ---------------------------------------------------------------------------

def print_candidate_table(context: Dict[str, Any]) -> None:
    print("\n[候选实体列表]")
    candidates = context.get("candidate_entities", []) or []
    for idx, item in enumerate(candidates, start=1):
        posts = ",".join(str(v) for v in item.get("possible_post_values_from_tcae", [])) or "-"
        zones = ",".join(item.get("zone_binding", {}).get("source_zones", []) or []) or "-"
        print(f"{idx:>3}. {item.get('display_name') or item.get('entity_id')} | posts={posts} | zones={zones}")


def print_normal_entry(eid: str, rec: Dict[str, Any], display_map: Dict[str, str]) -> None:
    print("-" * 88)
    print(f"实体: {display_map.get(eid, eid)}")
    print(f"  normal_values : {rec.get('normal_values', [])}")
    print(f"  abnormal_values: {rec.get('abnormal_values', [])}")
    print(f"  category      : {rec.get('category')}")
    print(f"  safety_level  : {rec.get('safety_level')}")
    print(f"  confidence    : {rec.get('confidence')}")
    print(f"  reason        : {rec.get('reason')}")


def input_with_default(question: str, default: Optional[str] = None) -> str:
    if default is None or default == "":
        return input(f"{question}: ").strip()
    raw = input(f"{question} [默认: {default}]: ").strip()
    return raw if raw else default


def choose_from_set(question: str, valid: Set[str], default: str) -> str:
    valid_text = "/".join(sorted(valid))
    while True:
        raw = input_with_default(f"{question} ({valid_text})", default).strip().lower()
        if raw in valid:
            return raw
        print(f"请输入以下值之一: {valid_text}")


def edit_entry(eid: str, rec: Dict[str, Any], display_map: Dict[str, str]) -> Dict[str, Any]:
    print(f"\n编辑实体: {display_map.get(eid, eid)}")
    normal_default = ",".join(str(v) for v in rec.get("normal_values", []))
    abnormal_default = ",".join(str(v) for v in rec.get("abnormal_values", []))

    normal_values: List[Any] = []
    while not normal_values:
        normal_raw = input_with_default("请输入 normal_values，多个值用逗号分隔", normal_default)
        normal_values = normalize_values(parse_value_list(normal_raw))
        if not normal_values:
            print("normal_values 不能为空。")

    abnormal_raw = input_with_default("请输入 abnormal_values，可空，多个值用逗号分隔", abnormal_default)
    abnormal_values = normalize_values(parse_value_list(abnormal_raw)) if abnormal_raw else []
    category = choose_from_set("请选择 category", VALID_CATEGORIES, str(rec.get("category") or "other"))
    level = choose_from_set("请选择 safety_level", VALID_LEVELS, str(rec.get("safety_level") or "medium"))
    reason = input_with_default("请输入 reason", str(rec.get("reason") or "human reviewed"))

    new_rec = dict(rec)
    new_rec.update(
        {
            "entity_id": eid,
            "normal_values": normal_values,
            "abnormal_values": abnormal_values,
            "category": category,
            "safety_level": level,
            "reason": reason,
            "needs_human_review": False,
            "source": "human_review",
        }
    )
    return new_rec


def add_manual_entry(
    normal_entities: Dict[str, Dict[str, Any]],
    context: Dict[str, Any],
    display_map: Dict[str, str],
) -> None:
    candidates = context.get("candidate_entities", []) or []
    entity_ids = [c["entity_id"] for c in candidates if c.get("entity_id")]
    if not entity_ids:
        print("没有可添加的候选实体。")
        return

    print_candidate_table(context)
    valid = set(range(1, len(entity_ids) + 1))
    while True:
        raw = input("请输入要添加的实体编号，或直接回车取消: ").strip()
        if not raw:
            return
        try:
            idx = int(raw)
        except ValueError:
            print("请输入数字编号。")
            continue
        if idx not in valid:
            print(f"编号范围为 1-{len(entity_ids)}")
            continue
        eid = entity_ids[idx - 1]
        base = normal_entities.get(
            eid,
            {
                "entity_id": eid,
                "normal_values": [],
                "abnormal_values": [],
                "category": "other",
                "safety_level": "medium",
                "confidence": 1.0,
                "reason": "manually added",
                "needs_human_review": False,
                "source": "human_added",
            },
        )
        normal_entities[eid] = edit_entry(eid, base, display_map)
        return


def build_display_map(context: Dict[str, Any], devices_data: Dict[str, Any]) -> Dict[str, str]:
    display: Dict[str, str] = {}
    for entity in devices_data.get("entities", []) or []:
        if entity.get("entity_id"):
            display[entity["entity_id"]] = entity.get("display_name") or entity["entity_id"]
    for item in context.get("candidate_entities", []) or []:
        if item.get("entity_id"):
            display[item["entity_id"]] = item.get("display_name") or item["entity_id"]
    return display


def review_normal_config_cli(
    normal_config: Dict[str, Any],
    context: Dict[str, Any],
    devices_data: Dict[str, Any],
) -> Dict[str, Any]:
    print("\n[实体常态配置人工审核]")
    print("说明：normal_values 表示该实体的常态/安全偏好状态。后续静态剪枝会把目标后态不属于 normal_values 的动作视为弱非预期后态。")

    display_map = build_display_map(context, devices_data)
    normal_entities: Dict[str, Dict[str, Any]] = dict(normal_config.get("normal_entities") or {})

    if normal_entities:
        print(f"\nAI/已有配置给出了 {len(normal_entities)} 个常态实体。")
    else:
        print("\n当前没有常态实体配置。可以手动添加。")

    for eid in list(sorted(normal_entities)):
        rec = normal_entities[eid]
        print_normal_entry(eid, rec, display_map)
        while True:
            action = input("操作：保留[k] / 编辑[e] / 删除[d] ? [k]: ").strip().lower() or "k"
            if action in {"k", "keep"}:
                rec["needs_human_review"] = False
                normal_entities[eid] = rec
                break
            if action in {"e", "edit"}:
                normal_entities[eid] = edit_entry(eid, rec, display_map)
                break
            if action in {"d", "delete", "drop"}:
                normal_entities.pop(eid, None)
                break
            print("请输入 k/e/d。")

    while prompt_yes_no("是否手动添加其它实体常态？", default=False):
        add_manual_entry(normal_entities, context, display_map)

    reviewed = dict(normal_config)
    reviewed.update(
        {
            "schema_version": "1.0",
            "generated_at": utc_now_iso(),
            "method": "ai_with_human_review" if normal_config.get("method", "").startswith("ai") else "human_review",
            "normal_entities": dict(sorted(normal_entities.items())),
            "summary": {
                "normal_entity_count": len(normal_entities),
                "levels": dict(sorted({lvl: sum(1 for r in normal_entities.values() if r.get("safety_level") == lvl) for lvl in VALID_LEVELS}.items())),
            },
        }
    )
    return reviewed


# ---------------------------------------------------------------------------
# Existing config loading / merging
# ---------------------------------------------------------------------------

def normalize_existing_normal_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize existing normal_config.json to the output schema."""
    raw = data.get("normal_entities") if isinstance(data, dict) else None
    entries: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        for eid, rec in raw.items():
            if isinstance(rec, dict):
                item = dict(rec)
                item.setdefault("entity_id", eid)
            else:
                item = {"entity_id": eid, "normal_values": listify(rec)}
            normalized = normalize_normal_entry(item, {str(eid)})
            if normalized:
                # Preserve fields not covered by normalize_normal_entry.
                if isinstance(rec, dict):
                    preserved = dict(rec)
                    preserved.update(normalized)
                    normalized = preserved
                entries[normalized["entity_id"]] = normalized
    elif isinstance(raw, list):
        ids = {str(x.get("entity_id")) for x in raw if isinstance(x, dict) and x.get("entity_id")}
        for item in raw:
            if isinstance(item, dict):
                normalized = normalize_normal_entry(item, ids)
                if normalized:
                    entries[normalized["entity_id"]] = normalized
    return {
        "schema_version": "1.0",
        "generated_at": data.get("generated_at") or utc_now_iso(),
        "method": data.get("method") or "existing_normalized",
        "normal_entities": dict(sorted(entries.items())),
    }


def merge_existing_over_proposal(proposal: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(proposal)
    proposal_entries = dict(proposal.get("normal_entities") or {})
    existing_entries = dict(existing.get("normal_entities") or {})
    proposal_entries.update(existing_entries)
    merged["normal_entities"] = dict(sorted(proposal_entries.items()))
    merged["method"] = f"{proposal.get('method', 'proposal')}_merged_existing"
    merged["generated_at"] = utc_now_iso()
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and review entity normal-state configuration for HomeAgent.")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI and start from empty/existing normal config.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive review and write the generated proposal directly.")
    parser.add_argument("--force-ai", action="store_true", help="Ignore existing normal_config.json when creating the initial proposal.")
    parser.add_argument("--use-existing", action="store_true", help="Use existing normal_config.json as the initial proposal without calling AI.")
    parser.add_argument("--include-all-entities", action="store_true", help="Send all extracted entities to AI. Default sends action-target entities only.")
    parser.add_argument("--keep-prompt", action="store_true", help="Do not auto-fill empty system_prompts.normal_config in config.json.")
    args = parser.parse_args()

    config = load_config()
    if not args.keep_prompt and ensure_system_prompts(config):
        save_config(config)
        print("已向 config.json 写入默认 normal_config system prompt。")

    load_env()
    registry_path = get_optional_input_path(config, "entity_registry", "./configurations/core.entity_registry")
    registry_map = load_entity_registry(registry_path)

    devices_path = get_output_path(config, "devices")
    channels_path = get_output_path(config, "channels")
    zones_path = get_output_path(config, "zones")
    tcae_path = get_output_path(config, "tcae")
    normal_path = get_output_path(config, "normal_config")

    devices_data = load_json(devices_path)
    channels_data = load_json(channels_path)
    zones_data = load_json(zones_path)
    tcae = load_json(tcae_path)

    context = build_ai_context(
        devices_data,
        channels_data,
        zones_data,
        tcae,
        include_all_entities=args.include_all_entities,
        registry_map=registry_map,
    )

    existing = None
    if normal_path.exists():
        try:
            existing = normalize_existing_normal_config(load_json(normal_path))
        except Exception as exc:
            print(f"[警告] 已有 normal_config.json 解析失败，将忽略: {exc}")
            existing = None

    if args.use_existing:
        if existing is None:
            raise FileNotFoundError(f"--use-existing was specified but no valid normal_config.json found: {normal_path}")
        proposal = existing
    elif args.no_ai:
        proposal = existing if existing is not None and not args.force_ai else build_empty_normal_config("empty_no_ai")
    else:
        try:
            proposal = propose_normal_config_with_ai(context, config)
            if existing is not None and not args.force_ai:
                proposal = merge_existing_over_proposal(proposal, existing)
                print("已将已有 normal_config.json 覆盖合并到 AI 建议中。")
        except Exception as exc:
            print(f"[警告] AI 常态配置生成失败: {exc}")
            if existing is not None and not args.force_ai:
                print("将使用已有 normal_config.json 作为初始配置。")
                proposal = existing
            else:
                print("将使用空配置。可在人工审核阶段手动添加实体常态。")
                proposal = build_empty_normal_config("empty_ai_failed")

    print("\n[Normal Config Proposal Summary]")
    print(json.dumps({"normal_entity_count": len(proposal.get("normal_entities", {})), "method": proposal.get("method")}, ensure_ascii=False, indent=2))

    if args.yes:
        output = proposal
    else:
        output = review_normal_config_cli(proposal, context, devices_data)

    write_json(normal_path, output)
    print(f"\n已写入实体常态配置: {normal_path}")
    print(json.dumps(output.get("summary", {"normal_entity_count": len(output.get("normal_entities", {}))}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
