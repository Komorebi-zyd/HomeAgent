"""
Step 6: Graph-level iterative refinement of boundary scenarios.

For each iteration itX:
1. persist automations-itX.yaml,
2. rebuild TCAE / RAG / USTG,
3. discover pairwise boundary scenarios from the current USTG,
4. globally audit whether the scenario partition is correct and sufficient,
5. synthesize rule_completion / rule_modification updates,
6. apply valid updates to produce automations-it(X+1).yaml,
7. rebuild graphs and continue until no valid update is produced or the
   iteration limit is reached.

Only existing entities/services may be reused in proposed rule updates.
Notification fallbacks must also be expressed as Home Assistant-style rules.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common import (
    call_ai_json,
    ensure_dir,
    get_home_dir,
    get_input_path,
    get_output_path,
    load_config,
    load_env,
    load_json,
    load_yaml,
    make_rule_uid,
    save_config,
    utc_now_iso,
    write_json,
    write_yaml,
)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PAIR_SCENARIO_DISCOVERY_PROMPT = r"""
You are the pairwise boundary-scenario discovery module of HomeAgent.

Background reasoning framework:
1. Unexpected states arise from: (a) rule-association patterns, (b) special boundary scenarios, and (c) possible drift between virtual states/modes and real-world situations.
2. The same rule association can be normal in one situation and dangerous in another.
3. The goal is not to treat every association as dangerous, but to explicitly distinguish acceptable scenarios from dangerous ones.
4. Notification fallback means proposing a Home Assistant rule that sends a notification under a specific condition, NOT merely writing a free-text note.

Task:
Given the current smart-home system information and ONE terminal pairwise association candidate, determine whether this same association has both normal and dangerous situations. Generate concrete scenario instances for that candidate only.

Constraints:
- Use only the supplied structured information.
- Do NOT rely on any specific human language or hard-coded entity-name patterns.
- Do NOT invent new devices/helpers.
- Return strict JSON only.

Output schema:
{
  "candidate_key": "string",
  "scenario_instances": [
    {
      "scenario_id": "s1",
      "title": "short title",
      "scenario_type": "boundary_timing|stale_virtual_state|missing_reset|presence_context_gap|resource_conflict|other",
      "normal_situation": "when the same association is acceptable",
      "dangerous_situation": "when the same association becomes dangerous",
      "activation_conditions": [
        {
          "kind": "state|time|duration|absence|other",
          "description": "plain structured description"
        }
      ],
      "unexpected_outcomes": [
        {
          "entity_id": "existing entity_id",
          "post_value": "on/off/value",
          "why_unexpected": "reason"
        }
      ],
      "confidence": 0.0,
      "needs_rule_refinement": true
    }
  ],
  "needs_human_review": true
}
""".strip()


SCENARIO_AUDIT_PROMPT = r"""
You are the scenario-catalog audit module of HomeAgent.

Task:
Review the CURRENT scenario catalog discovered from all terminal pairwise candidates in the current iteration. Your goal is to judge whether the catalog already captures the important normal-vs-dangerous boundary situations for the current system.

Principles:
- A good catalog distinguishes normal and dangerous situations for the same association.
- Focus on missing boundary timing, stale mode, missing reset, missing exit rules, and context persistence.
- Do NOT invent new devices/helpers.
- Return strict JSON only.

Output schema:
{
  "is_complete": true,
  "reason": "brief explanation",
  "revised_candidate_scenarios": [
    {
      "candidate_key": "string",
      "scenario_instances": [
        {
          "scenario_id": "s1",
          "title": "short title",
          "scenario_type": "boundary_timing|stale_virtual_state|missing_reset|presence_context_gap|resource_conflict|other",
          "normal_situation": "when the same association is acceptable",
          "dangerous_situation": "when the same association becomes dangerous",
          "activation_conditions": [
            {
              "kind": "state|time|duration|absence|other",
              "description": "plain structured description"
            }
          ],
          "unexpected_outcomes": [
            {
              "entity_id": "existing entity_id",
              "post_value": "on/off/value",
              "why_unexpected": "reason"
            }
          ],
          "confidence": 0.0,
          "needs_rule_refinement": true
        }
      ]
    }
  ]
}
""".strip()


RULE_UPDATE_SYNTHESIS_PROMPT = r"""
You are the graph-level rule-update synthesis module of HomeAgent.

Background reasoning framework:
1. Unexpected states arise from rule associations, special boundary scenarios, and insufficient distinction between virtual states and real situations.
2. The primary solution is to refine the rule set so that it better distinguishes normal scenarios from dangerous scenarios.
3. Only if distinction cannot be adequately improved should notification-style fallback rules be proposed.
4. Notification fallback MUST be expressed as a Home Assistant-style rule in rule_updates, not just as free text.

Task:
Given the current smart-home system information, the current iteration graph, the initial graph/rules, and the audited scenario catalog, propose rule updates that improve scenario distinction.

Allowed update types:
- rule_completion: add a new rule
- rule_modification: modify an existing rule

Constraints:
- Reuse ONLY existing entity_ids/services.
- Do NOT invent new devices/helpers.
- For rule_modification, target_rule_uid must be an existing rule in the current iteration.
- Return strict JSON only.

Output schema:
{
  "candidate_reports": [
    {
      "candidate_key": "string",
      "coverage_status": "needs_more_iteration|covered_with_rules|notify_only|manual_review_only",
      "reason": "brief explanation",
      "remaining_context_gaps": [
        {
          "scenario_id": "s1",
          "reason": "what still cannot be cleanly distinguished"
        }
      ],
      "needs_human_review": true
    }
  ],
  "rule_updates": [
    {
      "update_id": "u1",
      "update_type": "rule_completion|rule_modification",
      "goal": "distinguish_context|timeout_reset|auto_restore|guard_condition|notify_guard|state_alignment|other",
      "reused_entity_ids": ["existing.entity_1", "existing.entity_2"],
      "target_rule_uid": "existing rule uid if update_type is rule_modification",
      "candidate_rule": {
        "alias": "rule alias",
        "description": "why this update exists",
        "trigger": [],
        "condition": [],
        "action": [],
        "mode": "single"
      },
      "expected_effect": "how this update improves distinction",
      "confidence": 0.0,
      "reason": "brief reason"
    }
  ],
  "needs_human_review": true
}
""".strip()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def ensure_config_extensions(config: Dict[str, Any]) -> bool:
    changed = False
    output_files = config.setdefault("output_files", {})
    if not output_files.get("iterative_refinement_plan"):
        output_files["iterative_refinement_plan"] = "iterative_refinement_plan.json"
        changed = True
    prompts = config.setdefault("system_prompts", {})
    if not prompts.get("iterative_pair_scenario_discovery"):
        prompts["iterative_pair_scenario_discovery"] = PAIR_SCENARIO_DISCOVERY_PROMPT
        changed = True
    if not prompts.get("iterative_scenario_audit"):
        prompts["iterative_scenario_audit"] = SCENARIO_AUDIT_PROMPT
        changed = True
    if not prompts.get("iterative_rule_update"):
        prompts["iterative_rule_update"] = RULE_UPDATE_SYNTHESIS_PROMPT
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Dynamic imports
# ---------------------------------------------------------------------------


def import_module_from_file(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_step_modules() -> Tuple[Any, Any, Any, Any]:
    current_dir = Path(__file__).resolve().parent
    step2 = import_module_from_file("step2_bind_zones", current_dir / "2_bind_zones_and_build_tcae.py")
    step3 = import_module_from_file("step3_rag", current_dir / "3_build_rule_association_graph.py")
    step5 = import_module_from_file("step5_ustg", current_dir / "5_build_unexpected_state_transition_graph.py")
    step7 = import_module_from_file("step7_resolution", current_dir / "7_generate_resolution_rules.py")
    return step2, step3, step5, step7


def map_by_key(items: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get(key):
            out[str(item[key])] = item
    return out


def normalize_normal_entities(normal_config: Dict[str, Any]) -> Dict[str, Any]:
    normal_entities = normal_config.get("normal_entities") or {}
    if isinstance(normal_entities, dict):
        return normal_entities
    if isinstance(normal_entities, list):
        return {str(x.get("entity_id")): x for x in normal_entities if isinstance(x, dict) and x.get("entity_id")}
    return {}



# ---------------------------------------------------------------------------
# Home context
# ---------------------------------------------------------------------------


def build_home_context(devices: Dict[str, Any], channels: Dict[str, Any], zones: Dict[str, Any], normal_config: Dict[str, Any]) -> Dict[str, Any]:
    device_map = map_by_key(devices.get("entities", []) or [], "entity_id")
    channel_map = map_by_key(channels.get("bindings", []) or [], "entity_id")
    zone_map = zones.get("bindings") or {}
    normal_map = normalize_normal_entities(normal_config)

    entities: List[Dict[str, Any]] = []
    all_entity_ids = sorted(set(device_map) | set(channel_map) | set(zone_map) | set(normal_map))
    for eid in all_entity_ids:
        device = device_map.get(eid, {})
        channel = channel_map.get(eid, {})
        zone = zone_map.get(eid, {}) if isinstance(zone_map, dict) else {}
        normal = normal_map.get(eid, {}) if isinstance(normal_map, dict) else {}
        entities.append(
            {
                "entity_id": eid,
                "display_name": device.get("display_name", eid),
                "domain": device.get("domain"),
                "value_type_hint": device.get("value_type_hint"),
                "positions": device.get("positions", []),
                "primary_role": device.get("primary_role"),
                "operations": device.get("operations", []),
                "channel_binding": {
                    "role": channel.get("role"),
                    "observes": channel.get("observes", []),
                    "effects": channel.get("effects", []),
                    "effects_by_operation": channel.get("effects_by_operation", {}),
                },
                "zone_binding": {
                    "source_zones": zone.get("source_zones", []),
                    "reachable_zones": zone.get("reachable_zones", []),
                },
                "normal_config": normal,
            }
        )
    return {
        "summary": {
            "entity_count": len(entities),
            "zone_count": len(zones.get("zones", []) or []),
            "normal_entity_count": len(normal_map),
            "candidate_channels": channels.get("candidate_channels", []),
        },
        "zones": zones.get("zones", []),
        "entities": entities,
        "normal_entities": normal_map,
    }



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def unique_list(values: Iterable[Any]) -> List[Any]:
    seen = set()
    out = []
    for v in values:
        key = stable_json(v)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


class RateLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, int(rpm))
        self.interval = 60.0 / self.rpm
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.time()
                if now >= self._next_allowed_at:
                    self._next_allowed_at = now + self.interval
                    return
                sleep_for = self._next_allowed_at - now
            if sleep_for > 0:
                time.sleep(sleep_for)


ALLOWED_SCENARIO_TYPES = {
    "boundary_timing",
    "stale_virtual_state",
    "missing_reset",
    "presence_context_gap",
    "resource_conflict",
    "other",
}
ALLOWED_UPDATE_TYPES = {"rule_completion", "rule_modification"}
ALLOWED_COVERAGE_STATUS = {
    "needs_more_iteration",
    "covered_with_rules",
    "notify_only",
    "manual_review_only",
}


def compact_graph_for_ai(graph: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "summary": graph.get("summary", {}),
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "unexpected_paths": graph.get("unexpected_paths", []),
    }


def normalize_scenario_instance(item: Dict[str, Any], idx: int) -> Dict[str, Any]:
    scenario_type = str(item.get("scenario_type") or "other")
    if scenario_type not in ALLOWED_SCENARIO_TYPES:
        scenario_type = "other"
    return {
        "scenario_id": str(item.get("scenario_id") or f"s{idx}"),
        "title": str(item.get("title") or f"Scenario {idx}"),
        "scenario_type": scenario_type,
        "normal_situation": str(item.get("normal_situation") or ""),
        "dangerous_situation": str(item.get("dangerous_situation") or ""),
        "activation_conditions": listify(item.get("activation_conditions", [])),
        "unexpected_outcomes": listify(item.get("unexpected_outcomes", [])),
        "confidence": max(0.0, min(1.0, float(item.get("confidence", 0) or 0))),
        "needs_rule_refinement": bool(item.get("needs_rule_refinement", True)),
    }


def normalize_pair_discovery_response(raw: Any, candidate_key: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    scenarios = [
        normalize_scenario_instance(item, idx)
        for idx, item in enumerate(listify(raw.get("scenario_instances", [])), start=1)
        if isinstance(item, dict)
    ]
    return {
        "candidate_key": candidate_key,
        "scenario_instances": scenarios,
        "needs_human_review": bool(raw.get("needs_human_review", True)),
    }


def normalize_scenario_audit_response(raw: Any, existing_candidate_keys: Set[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    revised = []
    for item in listify(raw.get("revised_candidate_scenarios", [])):
        if not isinstance(item, dict):
            continue
        ckey = str(item.get("candidate_key") or "")
        if ckey not in existing_candidate_keys:
            continue
        scenarios = [
            normalize_scenario_instance(s, idx)
            for idx, s in enumerate(listify(item.get("scenario_instances", [])), start=1)
            if isinstance(s, dict)
        ]
        revised.append({"candidate_key": ckey, "scenario_instances": scenarios})
    return {
        "is_complete": bool(raw.get("is_complete", False)),
        "reason": str(raw.get("reason") or ""),
        "revised_candidate_scenarios": revised,
    }


def collect_entity_ids_from_rule_skeleton(obj: Any) -> Set[str]:
    out: Set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "entity_id":
                for item in listify(value):
                    if isinstance(item, str) and "." in item:
                        out.add(item)
            else:
                out.update(collect_entity_ids_from_rule_skeleton(value))
    elif isinstance(obj, list):
        for item in obj:
            out.update(collect_entity_ids_from_rule_skeleton(item))
    return out


def normalize_rule_update(item: Dict[str, Any], idx: int, allowed_entity_ids: Set[str], current_rule_uids: Set[str]) -> Dict[str, Any]:
    update_type = str(item.get("update_type") or "rule_completion")
    if update_type not in ALLOWED_UPDATE_TYPES:
        update_type = "rule_completion"
    rule = item.get("candidate_rule") if isinstance(item.get("candidate_rule"), dict) else {}
    referenced = sorted(collect_entity_ids_from_rule_skeleton(rule))
    invalid = [eid for eid in referenced if eid not in allowed_entity_ids]
    target_rule_uid = item.get("target_rule_uid")
    target_valid = True if update_type == "rule_completion" else target_rule_uid in current_rule_uids
    return {
        "update_id": str(item.get("update_id") or f"u{idx}"),
        "update_type": update_type,
        "goal": str(item.get("goal") or "other"),
        "reused_entity_ids": [str(e) for e in listify(item.get("reused_entity_ids", [])) if isinstance(e, str)],
        "target_rule_uid": target_rule_uid,
        "candidate_rule": rule,
        "expected_effect": str(item.get("expected_effect") or ""),
        "confidence": max(0.0, min(1.0, float(item.get("confidence", 0) or 0))),
        "reason": str(item.get("reason") or ""),
        "referenced_entity_ids": referenced,
        "invalid_referenced_entity_ids": invalid,
        "entity_validation_passed": len(invalid) == 0,
        "target_rule_uid_valid": target_valid,
        "update_validation_passed": len(invalid) == 0 and target_valid,
    }


def normalize_rule_update_plan_response(raw: Any, candidate_keys: Set[str], allowed_entity_ids: Set[str], current_rule_uids: Set[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    candidate_reports = []
    for item in listify(raw.get("candidate_reports", [])):
        if not isinstance(item, dict):
            continue
        ckey = str(item.get("candidate_key") or "")
        if ckey not in candidate_keys:
            continue
        status = str(item.get("coverage_status") or "manual_review_only")
        if status not in ALLOWED_COVERAGE_STATUS:
            status = "manual_review_only"
        candidate_reports.append(
            {
                "candidate_key": ckey,
                "coverage_status": status,
                "reason": str(item.get("reason") or ""),
                "remaining_context_gaps": [g for g in listify(item.get("remaining_context_gaps", [])) if isinstance(g, dict)],
                "needs_human_review": bool(item.get("needs_human_review", True)),
            }
        )
    rule_updates = [
        normalize_rule_update(item, idx, allowed_entity_ids, current_rule_uids)
        for idx, item in enumerate(listify(raw.get("rule_updates", [])), start=1)
        if isinstance(item, dict)
    ]
    return {
        "candidate_reports": candidate_reports,
        "rule_updates": rule_updates,
        "needs_human_review": bool(raw.get("needs_human_review", True)),
    }


def validate_rule_update(update: Dict[str, Any], allowed_entity_ids: Set[str], current_rule_uids: Set[str]) -> Dict[str, Any]:
    # already normalized once, but keep this helper for consistency
    return normalize_rule_update(update, 1, allowed_entity_ids, current_rule_uids)


def choose_updates_for_application(raw_updates: List[Dict[str, Any]], allowed_entity_ids: Set[str], current_rule_uids: Set[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    validated = [validate_rule_update(u, allowed_entity_ids, current_rule_uids) for u in raw_updates]
    valid = [u for u in validated if u.get("update_validation_passed")]
    invalid = [u for u in validated if not u.get("update_validation_passed")]

    modifications: Dict[str, Dict[str, Any]] = {}
    completions: List[Dict[str, Any]] = []
    for update in sorted(valid, key=lambda x: float(x.get("confidence", 0) or 0), reverse=True):
        if update.get("update_type") == "rule_modification":
            target = str(update.get("target_rule_uid") or "")
            if target and target not in modifications:
                modifications[target] = update
        else:
            completions.append(update)

    seen = set()
    dedup_completion: List[Dict[str, Any]] = []
    for update in completions:
        key = stable_json({"type": update.get("update_type"), "rule": update.get("candidate_rule")})
        if key not in seen:
            seen.add(key)
            dedup_completion.append(update)

    applied = list(modifications.values()) + dedup_completion
    return applied, invalid


# ---------------------------------------------------------------------------
# Build iteration artifacts
# ---------------------------------------------------------------------------


def build_iteration_artifacts(iter_dir: Path, iter_index: int, automations: List[Dict[str, Any]], devices: Dict[str, Any], channels: Dict[str, Any], zones: Dict[str, Any], normal_config: Dict[str, Any], step2: Any, step3: Any, step5: Any) -> Dict[str, Any]:
    ensure_dir(iter_dir)
    automations_path = iter_dir / f"automations-it{iter_index}.yaml"
    tcae_path = iter_dir / f"tcae-it{iter_index}.json"
    rag_path = iter_dir / f"rule_association_graph-it{iter_index}.json"
    ustg_path = iter_dir / f"unexpected_state_transition_graph-it{iter_index}.json"

    write_yaml(automations_path, automations)
    tcae = step2.build_tcae(automations, devices, channels, zones)
    write_json(tcae_path, tcae)
    rag = step3.build_rule_association_graph(tcae)
    write_json(rag_path, rag)
    ustg = step5.build_unexpected_state_transition_graph(tcae, rag, normal_config)
    write_json(ustg_path, ustg)

    return {
        "index": iter_index,
        "files": {
            "automations": str(automations_path),
            "tcae": str(tcae_path),
            "rule_association_graph": str(rag_path),
            "unexpected_state_transition_graph": str(ustg_path),
        },
        "automations": automations,
        "tcae": tcae,
        "rag": rag,
        "ustg": ustg,
        "graph_summary": {
            "rule_count": len(tcae.get("rules", []) or []),
            "rag_summary": rag.get("summary", {}),
            "ustg_summary": ustg.get("summary", {}),
        },
    }


def compact_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rule_uid": rule.get("rule_uid"),
        "rule_id": rule.get("rule_id"),
        "alias": rule.get("alias") or rule.get("display_alias"),
        "description": rule.get("description"),
        "mode": rule.get("mode"),
        "T": rule.get("T", []),
        "C": rule.get("C", []),
        "A": rule.get("A", []),
        "E": rule.get("E", {}),
        "entity_display": rule.get("entity_display", {}),
    }


def edge_brief(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "edge_id": edge.get("edge_id"),
        "source": edge.get("source"),
        "target": edge.get("target"),
        "kind": edge.get("kind"),
        "polarity": edge.get("polarity"),
        "metadata": edge.get("metadata", {}),
    }



# ---------------------------------------------------------------------------
# AI payload construction
# ---------------------------------------------------------------------------


def build_pair_payload(candidate: Dict[str, Any], source_rule: Dict[str, Any], target_rule: Dict[str, Any], home_context: Dict[str, Any], base_iteration: Dict[str, Any], current_iteration: Dict[str, Any]) -> Dict[str, Any]:
    relevant_ids = set()
    for rule in [source_rule, target_rule]:
        for section in [rule.get("T", []), rule.get("C", []), rule.get("A", [])]:
            for node in section or []:
                if isinstance(node, dict):
                    if node.get("entity_id"):
                        relevant_ids.add(str(node["entity_id"]))
                    if node.get("target_entity"):
                        relevant_ids.add(str(node["target_entity"]))
        for env_key in ["E_T", "E_C", "E_A"]:
            for node in ((rule.get("E") or {}).get(env_key) or []):
                if isinstance(node, dict) and node.get("entity_id"):
                    relevant_ids.add(str(node["entity_id"]))
    for out in candidate.get("unexpected_outcomes", []) or []:
        if isinstance(out, dict) and out.get("entity_id"):
            relevant_ids.add(str(out["entity_id"]))

    return {
        "task": "Discover boundary scenarios for one pairwise rule association candidate.",
        "base_iteration": {
            "automations": base_iteration["automations"],
            "ustg": compact_graph_for_ai(base_iteration["ustg"]),
            "graph_summary": base_iteration["graph_summary"],
        },
        "current_iteration": {
            "automations": current_iteration["automations"],
            "ustg": compact_graph_for_ai(current_iteration["ustg"]),
            "graph_summary": current_iteration["graph_summary"],
        },
        "home_context": home_context.get("summary", {}),
        "zones": home_context.get("zones", []),
        "all_entities": home_context.get("entities", []),
        "relevant_entities": [e for e in home_context.get("entities", []) or [] if e.get("entity_id") in relevant_ids],
        "normal_entities": home_context.get("normal_entities", {}),
        "source_rule": compact_rule(source_rule),
        "target_rule": compact_rule(target_rule),
        "association_candidate": candidate,
    }


def build_audit_payload(base_iteration: Dict[str, Any], current_iteration: Dict[str, Any], home_context: Dict[str, Any], candidate_scenarios: List[Dict[str, Any]], audit_round: int) -> Dict[str, Any]:
    return {
        "task": "Audit whether the current scenario catalog is correct and sufficient.",
        "audit_round": audit_round,
        "base_iteration": {
            "automations": base_iteration["automations"],
            "ustg": compact_graph_for_ai(base_iteration["ustg"]),
            "graph_summary": base_iteration["graph_summary"],
        },
        "current_iteration": {
            "automations": current_iteration["automations"],
            "ustg": compact_graph_for_ai(current_iteration["ustg"]),
            "graph_summary": current_iteration["graph_summary"],
        },
        "home_context": home_context.get("summary", {}),
        "zones": home_context.get("zones", []),
        "normal_entities": home_context.get("normal_entities", {}),
        "candidate_scenarios": candidate_scenarios,
    }


def build_rule_update_payload(base_iteration: Dict[str, Any], current_iteration: Dict[str, Any], home_context: Dict[str, Any], candidate_scenarios: List[Dict[str, Any]], accepted_updates_so_far: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "task": "Generate rule updates that distinguish audited normal and dangerous scenarios.",
        "base_iteration": {
            "automations": base_iteration["automations"],
            "ustg": compact_graph_for_ai(base_iteration["ustg"]),
            "graph_summary": base_iteration["graph_summary"],
        },
        "current_iteration": {
            "automations": current_iteration["automations"],
            "ustg": compact_graph_for_ai(current_iteration["ustg"]),
            "graph_summary": current_iteration["graph_summary"],
        },
        "home_context": home_context.get("summary", {}),
        "zones": home_context.get("zones", []),
        "all_entities": home_context.get("entities", []),
        "normal_entities": home_context.get("normal_entities", {}),
        "candidate_scenarios": candidate_scenarios,
        "accepted_rule_updates_so_far": accepted_updates_so_far,
    }


# ---------------------------------------------------------------------------
# Rule application
# ---------------------------------------------------------------------------


def build_rule_uid_map(automations: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for idx, automation in enumerate(automations, start=1):
        out[make_rule_uid(automation, idx)] = idx - 1
    return out


def apply_updates_to_automations(automations: List[Dict[str, Any]], updates: List[Dict[str, Any]], next_iteration_index: int) -> List[Dict[str, Any]]:
    new_automations = copy.deepcopy(automations)
    uid_map = build_rule_uid_map(new_automations)
    completion_counter = 0

    for update in updates:
        rule = copy.deepcopy(update.get("candidate_rule") or {})
        if update.get("update_type") == "rule_modification":
            target_uid = update.get("target_rule_uid")
            if target_uid not in uid_map:
                continue
            idx = uid_map[target_uid]
            existing = new_automations[idx]
            rule.setdefault("id", existing.get("id"))
            rule.setdefault("alias", existing.get("alias"))
            rule.setdefault("description", existing.get("description", ""))
            rule.setdefault("trigger", existing.get("trigger", []))
            rule.setdefault("condition", existing.get("condition", []))
            rule.setdefault("action", existing.get("action", []))
            rule.setdefault("mode", existing.get("mode", "single"))
            new_automations[idx] = rule
        else:
            completion_counter += 1
            rule.setdefault("id", f"it{next_iteration_index:02d}_c{completion_counter:03d}")
            rule.setdefault("alias", f"IT{next_iteration_index:02d}-Completion-{completion_counter:03d}")
            rule.setdefault("description", "")
            rule.setdefault("trigger", [])
            rule.setdefault("condition", [])
            rule.setdefault("action", [])
            rule.setdefault("mode", "single")
            new_automations.append(rule)

    return new_automations


# ---------------------------------------------------------------------------
# Main iterative engine
# ---------------------------------------------------------------------------


def build_iterative_refinement_plan(devices: Dict[str, Any], channels: Dict[str, Any], zones: Dict[str, Any], normal_config: Dict[str, Any], config: Dict[str, Any], rpm: int = 10, workers: Optional[int] = None, max_iterations: int = 3, no_ai: bool = False, max_audit_rounds: int = 3) -> Dict[str, Any]:
    step2, step3, step5, step7 = load_step_modules()
    home_context = build_home_context(devices, channels, zones, normal_config)
    prompts = {
        "pair_discovery": (config.get("system_prompts") or {}).get("iterative_pair_scenario_discovery") or PAIR_SCENARIO_DISCOVERY_PROMPT,
        "scenario_audit": (config.get("system_prompts") or {}).get("iterative_scenario_audit") or SCENARIO_AUDIT_PROMPT,
        "rule_update": (config.get("system_prompts") or {}).get("iterative_rule_update") or RULE_UPDATE_SYNTHESIS_PROMPT,
    }
    rate_limiter = None if no_ai else RateLimiter(rpm)

    base_automations = load_yaml(get_input_path(config, "automations"), default=[])
    if not isinstance(base_automations, list):
        raise ValueError("automations.yaml must be a list for iterative refinement.")

    iterations_root = ensure_dir(get_home_dir(config) / "iterations")
    all_iterations: List[Dict[str, Any]] = []
    current_automations = copy.deepcopy(base_automations)
    base_iteration: Optional[Dict[str, Any]] = None

    for iter_index in range(max_iterations + 1):
        iter_dir = ensure_dir(iterations_root / f"it{iter_index}")
        current_iteration = build_iteration_artifacts(iter_dir, iter_index, current_automations, devices, channels, zones, normal_config, step2, step3, step5)
        if base_iteration is None:
            base_iteration = current_iteration

        candidates = step7.extract_pairwise_candidates(current_iteration["ustg"])
        iter_record: Dict[str, Any] = {
            "index": iter_index,
            "files": current_iteration["files"],
            "graph_summary": current_iteration["graph_summary"],
            "candidate_count": len(candidates),
            "pair_discovery_reports": [],
            "scenario_audit_rounds": [],
            "final_candidate_scenarios": [],
            "rule_update_plan": {},
            "accepted_updates": [],
            "invalid_updates": [],
            "stopped_reason": "",
        }
        all_iterations.append(iter_record)

        if iter_index == max_iterations:
            iter_record["stopped_reason"] = "iteration_limit_reached"
            break
        if not candidates:
            iter_record["stopped_reason"] = "no_terminal_candidates"
            break

        total = len(candidates)
        max_workers = 1 if no_ai else max(1, min((workers if workers is not None else min(max(1, rpm), max(1, total))), max(1, total)))
        print(f"迭代轮次 it{iter_index}：candidates={total}, workers={max_workers}, rpm={rpm}, no_ai={no_ai}")

        # Phase 1: pairwise scenario discovery
        def pair_task(idx: int, candidate: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            source_rule = next((r for r in current_iteration["tcae"].get("rules", []) or [] if r.get("rule_uid") == candidate.get("source_rule_uid")), {})
            target_rule = next((r for r in current_iteration["tcae"].get("rules", []) or [] if r.get("rule_uid") == candidate.get("target_rule_uid")), {})
            payload = build_pair_payload(candidate, source_rule, target_rule, home_context, base_iteration, current_iteration)
            print(f"[提交 {idx}/{total}] {candidate['source_rule_uid']} -> {candidate['target_rule_uid']} ({candidate['association_kind']})")
            if no_ai:
                report = {"candidate_key": candidate.get("candidate_key"), "scenario_instances": [], "needs_human_review": True}
            else:
                try:
                    if rate_limiter:
                        rate_limiter.wait()
                    raw = call_ai_json(prompts["pair_discovery"], payload, temperature=0.0)
                    report = normalize_pair_discovery_response(raw, candidate.get("candidate_key"))
                except Exception as exc:
                    report = {"candidate_key": candidate.get("candidate_key"), "scenario_instances": [], "needs_human_review": True, "error": str(exc)}
            report.update({
                "candidate_id": candidate.get("candidate_id"),
                "candidate_key": candidate.get("candidate_key"),
                "source_rule_uid": candidate.get("source_rule_uid"),
                "target_rule_uid": candidate.get("target_rule_uid"),
                "association_kind": candidate.get("association_kind"),
            })
            return idx, report

        pair_reports: Dict[int, Dict[str, Any]] = {}
        if max_workers == 1:
            for idx, candidate in enumerate(candidates, start=1):
                _, report = pair_task(idx, candidate)
                print(f"[完成 {idx}/{total}] {report['source_rule_uid']} -> {report['target_rule_uid']} ({len(report.get('scenario_instances', []))} scenarios)")
                pair_reports[idx] = report
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(pair_task, idx, candidate): idx for idx, candidate in enumerate(candidates, start=1)}
                for future in as_completed(future_map):
                    idx, report = future.result()
                    print(f"[完成 {idx}/{total}] {report['source_rule_uid']} -> {report['target_rule_uid']} ({len(report.get('scenario_instances', []))} scenarios)")
                    pair_reports[idx] = report

        ordered_pair_reports = [pair_reports[idx] for idx in sorted(pair_reports)]
        iter_record["pair_discovery_reports"] = ordered_pair_reports
        scenario_catalog = [{"candidate_key": r.get("candidate_key"), "scenario_instances": r.get("scenario_instances", [])} for r in ordered_pair_reports]

        # Phase 2: global scenario audit
        if no_ai:
            audit_result = {
                "is_complete": False,
                "reason": "AI disabled; scenario audit not performed.",
                "revised_candidate_scenarios": scenario_catalog,
            }
            iter_record["scenario_audit_rounds"].append({"round": 1, **audit_result})
        else:
            audit_complete = False
            for audit_round in range(1, max_audit_rounds + 1):
                payload = build_audit_payload(base_iteration, current_iteration, home_context, scenario_catalog, audit_round)
                try:
                    if rate_limiter:
                        rate_limiter.wait()
                    raw = call_ai_json(prompts["scenario_audit"], payload, temperature=0.0)
                    audit_result = normalize_scenario_audit_response(raw, {c.get("candidate_key") for c in scenario_catalog})
                except Exception as exc:
                    audit_result = {
                        "is_complete": False,
                        "reason": f"AI scenario audit failed: {exc}",
                        "revised_candidate_scenarios": scenario_catalog,
                    }
                iter_record["scenario_audit_rounds"].append({"round": audit_round, **audit_result})
                if audit_result.get("revised_candidate_scenarios"):
                    scenario_catalog = audit_result["revised_candidate_scenarios"]
                if audit_result.get("is_complete"):
                    audit_complete = True
                    break
            if not audit_complete and not iter_record["scenario_audit_rounds"]:
                iter_record["scenario_audit_rounds"].append({"round": 1, "is_complete": False, "reason": "scenario audit not executed", "revised_candidate_scenarios": scenario_catalog})

        iter_record["final_candidate_scenarios"] = scenario_catalog

        # Phase 3: rule update synthesis
        allowed_entity_ids = {e.get("entity_id") for e in home_context.get("entities", []) or [] if e.get("entity_id")}
        current_rule_uids = {r.get("rule_uid") for r in current_iteration["tcae"].get("rules", []) or [] if r.get("rule_uid")}
        if no_ai:
            rule_update_plan = {"candidate_reports": [], "rule_updates": [], "needs_human_review": True}
        else:
            payload = build_rule_update_payload(base_iteration, current_iteration, home_context, scenario_catalog, iter_record.get("accepted_updates", []))
            try:
                if rate_limiter:
                    rate_limiter.wait()
                raw = call_ai_json(prompts["rule_update"], payload, temperature=0.0)
                rule_update_plan = normalize_rule_update_plan_response(raw, {c.get("candidate_key") for c in scenario_catalog}, allowed_entity_ids, current_rule_uids)
            except Exception as exc:
                rule_update_plan = {
                    "candidate_reports": [],
                    "rule_updates": [],
                    "needs_human_review": True,
                    "error": str(exc),
                }

        iter_record["rule_update_plan"] = rule_update_plan
        accepted_updates, invalid_updates = choose_updates_for_application(rule_update_plan.get("rule_updates", []), allowed_entity_ids, current_rule_uids)
        iter_record["accepted_updates"] = accepted_updates
        iter_record["invalid_updates"] = invalid_updates

        if not accepted_updates:
            iter_record["stopped_reason"] = "no_valid_rule_updates"
            break

        current_automations = apply_updates_to_automations(current_automations, accepted_updates, iter_index + 1)
        iter_record["stopped_reason"] = "updates_applied_continue"

    final_iteration = all_iterations[-1]
    coverage_counts = Counter()
    total_scenarios = 0
    total_updates = 0
    total_invalid_updates = 0
    final_candidate_reports: List[Dict[str, Any]] = []

    for it in all_iterations:
        for report in (it.get("rule_update_plan", {}).get("candidate_reports", []) or []):
            coverage_counts[report.get("coverage_status", "unknown")] += 1
            final_candidate_reports.append(report)
        for pair_report in it.get("final_candidate_scenarios", []):
            total_scenarios += len(pair_report.get("scenario_instances", []))
        total_updates += len(it.get("accepted_updates", []))
        total_invalid_updates += len(it.get("invalid_updates", []))

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "graph_level_iterative_refinement" if not no_ai else "graph_level_iterative_refinement_no_ai",
        "rpm": rpm,
        "workers": workers,
        "max_iterations": max_iterations,
        "base_automations": str(get_input_path(config, "automations")),
        "iterations_root": str(iterations_root),
        "iterations": all_iterations,
        "final_iteration_index": final_iteration.get("index"),
        "final_files": final_iteration.get("files", {}),
        "final_candidate_reports": final_candidate_reports,
        "summary": {
            "iteration_count": len(all_iterations),
            "final_candidate_count": final_iteration.get("candidate_count", 0),
            "total_scenario_instance_count": total_scenarios,
            "accepted_rule_update_count": total_updates,
            "invalid_rule_update_count": total_invalid_updates,
            "coverage_status_counts": dict(sorted(coverage_counts.items())),
            "final_stopped_reason": final_iteration.get("stopped_reason"),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Graph-level iterative refinement of boundary scenarios.")
    parser.add_argument("--rpm", type=int, default=10, help="Maximum AI requests per minute across all workers. Default: 10")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent worker count. Default: min(rpm, candidate_count).")
    parser.add_argument("--max-iterations", type=int, default=3, help="Maximum iteration rounds. Default: 3")
    parser.add_argument("--max-audit-rounds", type=int, default=3, help="Maximum global scenario-audit rounds per iteration. Default: 3")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI calls and produce manual-review placeholders.")
    parser.add_argument("--keep-prompt", action="store_true", help="Do not auto-fill config extensions.")
    args = parser.parse_args()

    if args.rpm < 1:
        raise ValueError("--rpm must be >= 1")
    if args.max_iterations < 1:
        raise ValueError("--max-iterations must be >= 1")
    if args.max_audit_rounds < 1:
        raise ValueError("--max-audit-rounds must be >= 1")
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be >= 1")

    config = load_config()
    if not args.keep_prompt and ensure_config_extensions(config):
        save_config(config)
        print("已更新 config.json：添加 step6 prompts 与 iterative_refinement_plan 输出。")

    load_env()
    devices = load_json(get_output_path(config, "devices"))
    channels = load_json(get_output_path(config, "channels"))
    zones = load_json(get_output_path(config, "zones"))
    normal_config = load_json(get_output_path(config, "normal_config"))

    plan = build_iterative_refinement_plan(
        devices,
        channels,
        zones,
        normal_config,
        config,
        rpm=args.rpm,
        workers=args.workers,
        max_iterations=args.max_iterations,
        no_ai=args.no_ai,
        max_audit_rounds=args.max_audit_rounds,
    )

    iterative_plan_path = get_output_path(config, "iterative_refinement_plan")
    write_json(iterative_plan_path, plan)
    print(f"\n已生成迭代细化计划: {iterative_plan_path}")
    print(json.dumps(plan.get("summary", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
