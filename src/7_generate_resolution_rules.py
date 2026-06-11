"""
Step 6: Graph-level iterative refinement of unexpected-state boundary scenarios.

This step performs true *iteration over rule sets*:
- start from the current automations.yaml,
- build TCAE / RAG / USTG for iteration it0,
- ask AI to discover still-missing dangerous boundary scenarios,
- ask AI to propose rule completions or rule modifications using only existing
  entities,
- write a new automations-itX.yaml,
- rebuild TCAE / RAG / USTG for the next iteration,
- repeat until no new valid rule updates are produced or the iteration limit is
  reached.

Outputs:
- configurations/home/iterative_refinement_plan.json
- configurations/home/iterations/itX/automations-itX.yaml
- configurations/home/iterations/itX/tcae-itX.json
- configurations/home/iterations/itX/rule_association_graph-itX.json
- configurations/home/iterations/itX/unexpected_state_transition_graph-itX.json
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import threading
import time
from collections import Counter, defaultdict
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


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


DEFAULT_ITERATIVE_REFINEMENT_PROMPT = r"""
You are the graph-level iterative unexpected-scenario refinement module of HomeAgent.
Your task is to analyze ONE pairwise rule association candidate in the CURRENT iteration graph, then:
1. identify dangerous boundary scenarios under which this association really leads to an unexpected state,
2. distinguish those dangerous scenarios from normal scenarios,
3. propose rule updates that improve this distinction,
4. use ONLY existing entities/services,
5. indicate whether the current rule set for this candidate still needs another iteration.

Important principles:
1. Do NOT rely on any specific human language or hard-coded entity-name words. Entity names, aliases and descriptions are semantic hints only.
2. Use only the supplied structured context: current automations, current TCAE, current pairwise association evidence, current normal-state configuration, current graph summary, and already accepted rule updates from previous iterations.
3. A rule association does NOT always imply an unexpected state. Focus on scenario boundaries: timing boundaries, stale virtual modes, missing reset rules, context persistence, lack of exit rules, and virtual/real-world mismatch risks.
4. Rule update types:
   - rule_completion: add a new rule;
   - rule_modification: modify an existing rule.
5. Proposed rule updates MUST reuse ONLY the listed existing entity_ids. You may use Home Assistant native notify services as platform capability, but you must not invent new devices, helpers, sensors, or switches.
6. Prefer context distinction, timeout reset, auto-restore, guard conditions, and notification guards.
7. Return strict JSON only. Do not include markdown or commentary.

Output schema:
{
  "coverage_status": "needs_more_iteration|covered_with_rules|notify_only|manual_review_only",
  "scenario_instances": [
    {
      "scenario_id": "s1",
      "title": "short title",
      "scenario_type": "boundary_timing|stale_virtual_state|missing_reset|presence_context_gap|resource_conflict|other",
      "normal_situation": "when this association is acceptable",
      "dangerous_situation": "when this association becomes dangerous",
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
      "expected_effect": "how this update improves distinction between normal and dangerous contexts",
      "confidence": 0.0,
      "reason": "brief reason"
    }
  ],
  "remaining_context_gaps": [
    {
      "scenario_id": "s1",
      "reason": "what still cannot be cleanly distinguished using existing entities"
    }
  ],
  "notify_fallbacks": [
    {
      "when": "brief condition or scenario title",
      "message": "what should be sent to the user",
      "reason": "why notification is the fallback"
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
    if not prompts.get("iterative_refinement"):
        prompts["iterative_refinement"] = DEFAULT_ITERATIVE_REFINEMENT_PROMPT
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


def load_step_modules() -> Tuple[Any, Any, Any]:
    current_dir = Path(__file__).resolve().parent
    step2 = import_module_from_file("step2_bind_zones", current_dir / "2_bind_zones_and_build_tcae.py")
    step3 = import_module_from_file("step3_rag", current_dir / "3_build_rule_association_graph.py")
    step5 = import_module_from_file("step5_ustg", current_dir / "5_build_unexpected_state_transition_graph.py")
    return step2, step3, step5


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


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


ALLOWED_COVERAGE_STATUS = {
    "needs_more_iteration",
    "covered_with_rules",
    "notify_only",
    "manual_review_only",
}
ALLOWED_SCENARIO_TYPES = {
    "boundary_timing",
    "stale_virtual_state",
    "missing_reset",
    "presence_context_gap",
    "resource_conflict",
    "other",
}
ALLOWED_UPDATE_TYPES = {"rule_completion", "rule_modification"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def ensure_system_prompts(config: Dict[str, Any]) -> bool:
    changed = False
    prompts = config.setdefault("system_prompts", {})
    if not prompts.get("resolution_rules"):
        prompts["resolution_rules"] = DEFAULT_RESOLUTION_RULES_PROMPT
        changed = True
    return changed



# ---------------------------------------------------------------------------
# Candidate extraction (terminal-only) and home context
# ---------------------------------------------------------------------------


def is_action_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::A")


def is_trigger_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::T")


def is_condition_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::C")


def is_env_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.startswith("env::")


def rule_uid_from_component_node(node_id: str) -> Optional[str]:
    if not isinstance(node_id, str) or "::" not in node_id or node_id.startswith("env::"):
        return None
    return node_id.rsplit("::", 1)[0]


def component_from_node(node_id: str) -> Optional[str]:
    if not isinstance(node_id, str) or "::" not in node_id:
        return None
    return node_id.rsplit("::", 1)[1]


def target_component_type(node_id: str) -> str:
    comp = component_from_node(node_id)
    if comp == "T":
        return "trigger"
    if comp == "C":
        return "condition"
    if comp == "A":
        return "action"
    return "unknown"


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


DIRECT_TARGET_KINDS = {
    "direct-trigger",
    "direct-condition-allow",
    "direct-condition-disable",
    "direct-action",
}
INDIRECT_TARGET_KINDS = {
    "env-trigger",
    "indirect-condition-allow",
    "indirect-condition-disable",
    "indirect-action",
}


def make_candidate_key(source_rule: str, target_rule: str, target_component: str, association_kind: str, via_env: Optional[Dict[str, Any]] = None) -> str:
    via = ""
    if via_env:
        via = f"|{via_env.get('zone','')}|{via_env.get('channel','')}"
    return f"{source_rule}->{target_rule}|{target_component}|{association_kind}{via}"


def empty_candidate(key: str, source_rule: str, target_rule: str, target_component: str, association_kind: str, association_mode: str, via_environment: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "candidate_id": "",
        "candidate_key": key,
        "source_rule_uid": source_rule,
        "target_rule_uid": target_rule,
        "target_component": target_component,
        "association_kind": association_kind,
        "association_mode": association_mode,
        "via_environment": via_environment,
        "edge_ids": [],
        "edge_evidence": [],
        "path_ids": [],
        "path_evidence": [],
        "unexpected_outcomes": [],
        "path_polarities": [],
        "occurrence_count": 0,
    }


def merge_candidate_evidence(candidate: Dict[str, Any], edges: List[Dict[str, Any]], path: Dict[str, Any]) -> None:
    for edge in edges:
        eid = edge.get("edge_id")
        if eid and eid not in candidate["edge_ids"]:
            candidate["edge_ids"].append(eid)
            candidate["edge_evidence"].append(edge_brief(edge))
    pid = path.get("path_id")
    if pid and pid not in candidate["path_ids"]:
        candidate["path_ids"].append(pid)
        candidate["path_evidence"].append(
            {
                "path_id": pid,
                "nodes": path.get("nodes", []),
                "edges": path.get("edges", []),
                "edge_kinds": path.get("edge_kinds", []),
                "path_polarity": path.get("path_polarity"),
                "terminal_action_node": path.get("terminal_action_node"),
            }
        )
    for item in path.get("out_U", []) or []:
        if isinstance(item, dict):
            candidate["unexpected_outcomes"].append(item)
    pol = path.get("path_polarity")
    if pol not in candidate["path_polarities"]:
        candidate["path_polarities"].append(pol)
    candidate["occurrence_count"] += 1


def add_candidate(candidates: Dict[str, Dict[str, Any]], source_rule: str, target_rule: str, target_component: str, association_kind: str, association_mode: str, edges: List[Dict[str, Any]], path: Dict[str, Any], via_environment: Optional[Dict[str, Any]] = None) -> None:
    if not source_rule or not target_rule or source_rule == target_rule:
        return
    key = make_candidate_key(source_rule, target_rule, target_component, association_kind, via_environment)
    if key not in candidates:
        candidates[key] = empty_candidate(key, source_rule, target_rule, target_component, association_kind, association_mode, via_environment)
    merge_candidate_evidence(candidates[key], edges, path)


def extract_pairwise_candidates(ustg: Dict[str, Any]) -> List[Dict[str, Any]]:
    edge_map = {e.get("edge_id"): e for e in ustg.get("edges", []) or [] if isinstance(e, dict) and e.get("edge_id")}
    node_map = {n.get("node_id"): n for n in ustg.get("nodes", []) or [] if isinstance(n, dict) and n.get("node_id")}
    candidates: Dict[str, Dict[str, Any]] = {}
    for path in ustg.get("unexpected_paths", []) or []:
        if not isinstance(path, dict):
            continue
        terminal_rule = rule_uid_from_component_node(path.get("terminal_action_node", ""))
        edge_seq = [edge_map[eid] for eid in path.get("edges", []) if eid in edge_map]
        for edge in edge_seq:
            kind = edge.get("kind")
            if kind not in DIRECT_TARGET_KINDS:
                continue
            src = edge.get("source")
            tgt = edge.get("target")
            if not is_action_node(src) or not (is_trigger_node(tgt) or is_condition_node(tgt) or is_action_node(tgt)):
                continue
            source_rule = rule_uid_from_component_node(src)
            target_rule = rule_uid_from_component_node(tgt)
            if terminal_rule and target_rule != terminal_rule:
                continue
            add_candidate(candidates, source_rule or "", target_rule or "", target_component_type(tgt), str(kind), "direct", [edge], path)
        for idx in range(len(edge_seq) - 1):
            e1 = edge_seq[idx]
            e2 = edge_seq[idx + 1]
            if e1.get("kind") != "env-association":
                continue
            if e2.get("kind") not in INDIRECT_TARGET_KINDS:
                continue
            if e1.get("target") != e2.get("source"):
                continue
            src = e1.get("source")
            env = e1.get("target")
            tgt = e2.get("target")
            if not is_action_node(src) or not is_env_node(env) or not (is_trigger_node(tgt) or is_condition_node(tgt) or is_action_node(tgt)):
                continue
            source_rule = rule_uid_from_component_node(src)
            target_rule = rule_uid_from_component_node(tgt)
            if terminal_rule and target_rule != terminal_rule:
                continue
            env_node_data = node_map.get(env, {})
            via = {
                "node_id": env,
                "zone": env_node_data.get("zone") or (e1.get("metadata") or {}).get("zone") or (e2.get("metadata") or {}).get("zone"),
                "channel": env_node_data.get("channel") or (e1.get("metadata") or {}).get("channel") or (e2.get("metadata") or {}).get("channel"),
            }
            add_candidate(candidates, source_rule or "", target_rule or "", target_component_type(tgt), str(e2.get("kind")), "indirect", [e1, e2], path, via_environment=via)
    out = list(candidates.values())
    for cand in out:
        cand["edge_ids"] = sorted(unique_list(cand.get("edge_ids", [])))
        cand["path_ids"] = sorted(unique_list(cand.get("path_ids", [])))
        cand["unexpected_outcomes"] = unique_list(cand.get("unexpected_outcomes", []))
        cand["path_polarities"] = sorted(unique_list(cand.get("path_polarities", [])), key=lambda x: str(x))
    out = sorted(out, key=lambda c: (c["source_rule_uid"], c["target_rule_uid"], c["target_component"], c["association_kind"], stable_json(c.get("via_environment"))))
    for idx, cand in enumerate(out, start=1):
        cand["candidate_id"] = f"ra{idx:05d}"
    return out


# ---------------------------------------------------------------------------
# Iterative normalization and prompt payload
# ---------------------------------------------------------------------------


def build_all_entities_compact(home_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    compact = []
    for e in home_context.get("entities", []) or []:
        compact.append(
            {
                "entity_id": e.get("entity_id"),
                "display_name": e.get("display_name"),
                "domain": e.get("domain"),
                "source_zones": (e.get("zone_binding") or {}).get("source_zones", []),
                "reachable_zones": (e.get("zone_binding") or {}).get("reachable_zones", []),
                "observes": ((e.get("channel_binding") or {}).get("observes") or []),
                "effects": ((e.get("channel_binding") or {}).get("effects") or []),
                "effects_by_operation": ((e.get("channel_binding") or {}).get("effects_by_operation") or {}),
                "normal_config": e.get("normal_config", {}),
            }
        )
    return compact


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


def relevant_entity_ids_for_candidate(candidate: Dict[str, Any], source_rule: Dict[str, Any], target_rule: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()
    for rule in [source_rule, target_rule]:
        for section in [rule.get("T", []), rule.get("C", []), rule.get("A", [])]:
            for node in section or []:
                if isinstance(node, dict):
                    if node.get("entity_id"):
                        ids.add(str(node["entity_id"]))
                    if node.get("target_entity"):
                        ids.add(str(node["target_entity"]))
        for env_key in ["E_T", "E_C", "E_A"]:
            for node in ((rule.get("E") or {}).get(env_key) or []):
                if isinstance(node, dict) and node.get("entity_id"):
                    ids.add(str(node["entity_id"]))
    for item in candidate.get("unexpected_outcomes", []) or []:
        if isinstance(item, dict) and item.get("entity_id"):
            ids.add(str(item["entity_id"]))
    for edge in candidate.get("edge_evidence", []) or []:
        meta = edge.get("metadata") or {}
        for key in ["entity_id", "target_entity", "effect_entity_id"]:
            if meta.get(key):
                ids.add(str(meta[key]))
    return ids


def filter_relevant_entities(home_context: Dict[str, Any], entity_ids: Set[str]) -> List[Dict[str, Any]]:
    return [e for e in home_context.get("entities", []) or [] if e.get("entity_id") in entity_ids]


def compact_iteration_refinement_report(report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    return {
        "coverage_status": report.get("coverage_status"),
        "scenario_instances": report.get("scenario_instances", []),
        "accepted_rule_updates": report.get("accepted_rule_updates", []),
        "remaining_context_gaps": report.get("remaining_context_gaps", []),
        "notify_fallbacks": report.get("notify_fallbacks", []),
        "iteration_count": report.get("iteration_count"),
    }


def template_map() -> Dict[int, Dict[str, Any]]:
    return {int(t["strategy_id"]): t for t in RESOLUTION_STRATEGY_TEMPLATES}


def normalize_strategy_id(value: Any) -> Optional[int]:
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return None
    return sid if sid in template_map() else None


def normalize_ai_policy(ai_data: Any, fallback_strategy: int = 0) -> Dict[str, Any]:
    templates = template_map()
    if not isinstance(ai_data, dict):
        ai_data = {}
    sid = normalize_strategy_id(ai_data.get("strategy_id"))
    if sid is None:
        name = str(ai_data.get("strategy_name") or "").strip()
        name_to_id = {t["strategy_name"]: t["strategy_id"] for t in RESOLUTION_STRATEGY_TEMPLATES}
        sid = int(name_to_id.get(name, fallback_strategy))
    template = templates.get(sid, templates[fallback_strategy])
    try:
        confidence = float(ai_data.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return {
        "strategy_id": sid,
        "strategy_name": template["strategy_name"],
        "confidence": confidence,
        "reason": str(ai_data.get("reason") or "No AI reason provided."),
        "runtime_notes": str(ai_data.get("runtime_notes") or template.get("runtime_effect") or ""),
        "needs_human_review": bool(ai_data.get("needs_human_review", False)) or confidence < 0.75,
        "template": template,
        "source": "ai",
    }


def fallback_policy(reason: str, strategy_id: int = 0) -> Dict[str, Any]:
    template = template_map()[strategy_id]
    return {
        "strategy_id": strategy_id,
        "strategy_name": template["strategy_name"],
        "confidence": 0.0,
        "reason": reason,
        "runtime_notes": template.get("runtime_effect", ""),
        "needs_human_review": True,
        "template": template,
        "source": "fallback",
    }


def build_runtime_payload(candidate: Dict[str, Any], source_rule: Dict[str, Any], target_rule: Dict[str, Any], home_context: Dict[str, Any], refinement_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    relevant_ids = relevant_entity_ids_for_candidate(candidate, source_rule, target_rule)
    return {
        "task": "Choose one runtime residual-risk handling strategy for a pairwise rule association.",
        "home_context": home_context.get("summary", {}),
        "zones": home_context.get("zones", []),
        "all_entities_compact": build_all_entities_compact(home_context),
        "relevant_entities": filter_relevant_entities(home_context, relevant_ids),
        "source_rule": compact_rule(source_rule),
        "target_rule": compact_rule(target_rule),
        "association_candidate": candidate,
        "iterative_refinement_report": compact_iteration_refinement_report(refinement_report),
        "strategy_templates": RESOLUTION_STRATEGY_TEMPLATES,
        "constraints": {
            "select_exactly_one_strategy": True,
            "strategy_id_must_be_one_of": [t["strategy_id"] for t in RESOLUTION_STRATEGY_TEMPLATES],
            "return_strict_json_only": True,
        },
    }


def iterative_report_map(plan: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(plan, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for item in plan.get("candidate_reports", []) or []:
        if isinstance(item, dict) and item.get("candidate_key"):
            out[str(item["candidate_key"])] = item
    return out


# ---------------------------------------------------------------------------
# Runtime resolution generation
# ---------------------------------------------------------------------------


def generate_policy_for_candidate(candidate: Dict[str, Any], refinement_report: Optional[Dict[str, Any]], tcae_rule_map: Dict[str, Dict[str, Any]], home_context: Dict[str, Any], system_prompt: str, no_ai: bool = False, rate_limiter: Optional[RateLimiter] = None) -> Dict[str, Any]:
    source_rule = tcae_rule_map.get(candidate.get("source_rule_uid"))
    target_rule = tcae_rule_map.get(candidate.get("target_rule_uid"))
    if not source_rule or not target_rule:
        return fallback_policy("Missing source or target rule context; human review required.")

    if refinement_report and refinement_report.get("coverage_status") == "covered_with_rules" and not refinement_report.get("remaining_context_gaps"):
        return {
            "strategy_id": 0,
            "strategy_name": "default",
            "confidence": 0.95,
            "reason": "Iterative refinement reports that current rule updates already distinguish the dangerous scenarios and no residual runtime gap remains.",
            "runtime_notes": "No runtime intervention needed if the accepted rule updates have actually been deployed and the graph has been rebuilt.",
            "needs_human_review": True,
            "template": template_map()[0],
            "source": "derived_from_iterative_refinement",
        }

    if no_ai:
        return fallback_policy("AI disabled by --no-ai. Runtime policy requires human review.")

    payload = build_runtime_payload(candidate, source_rule, target_rule, home_context, refinement_report)
    try:
        if rate_limiter:
            rate_limiter.wait()
        ai_data = call_ai_json(system_prompt, payload, temperature=0.0)
        return normalize_ai_policy(ai_data)
    except Exception as exc:
        return fallback_policy(f"AI runtime resolution generation failed: {exc}")


def build_runtime_resolution_rules(devices: Dict[str, Any], channels: Dict[str, Any], zones: Dict[str, Any], tcae: Dict[str, Any], normal_config: Dict[str, Any], ustg: Dict[str, Any], iterative_plan: Optional[Dict[str, Any]], config: Dict[str, Any], rpm: int = 10, no_ai: bool = False, limit: Optional[int] = None, workers: Optional[int] = None) -> Dict[str, Any]:
    candidates = extract_pairwise_candidates(ustg)
    if limit is not None:
        candidates = candidates[: max(0, limit)]

    home_context = build_home_context(devices, channels, zones, normal_config)
    tcae_rule_map = {r.get("rule_uid"): r for r in tcae.get("rules", []) or [] if isinstance(r, dict) and r.get("rule_uid")}
    prompt = (config.get("system_prompts") or {}).get("resolution_rules") or DEFAULT_RESOLUTION_RULES_PROMPT
    report_map = iterative_report_map(iterative_plan)
    total = len(candidates)
    max_workers = 1 if no_ai else max(1, min((workers if workers is not None else min(max(1, rpm), max(1, total))), max(1, total)))
    rate_limiter = None if no_ai else RateLimiter(rpm)

    def build_record(idx: int, cand: Dict[str, Any], policy: Dict[str, Any], refinement: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "resolution_rule_id": f"rr{idx:05d}",
            "candidate_id": cand.get("candidate_id"),
            "candidate_key": cand.get("candidate_key"),
            "source_rule_uid": cand.get("source_rule_uid"),
            "target_rule_uid": cand.get("target_rule_uid"),
            "target_component": cand.get("target_component"),
            "association_kind": cand.get("association_kind"),
            "association_mode": cand.get("association_mode"),
            "via_environment": cand.get("via_environment"),
            "unexpected_outcomes": cand.get("unexpected_outcomes", []),
            "path_ids": cand.get("path_ids", []),
            "edge_ids": cand.get("edge_ids", []),
            "iterative_refinement_used": bool(refinement),
            "coverage_status": (refinement or {}).get("coverage_status"),
            "remaining_context_gaps": (refinement or {}).get("remaining_context_gaps", []),
            "policy": policy,
        }

    results: Dict[int, Dict[str, Any]] = {}
    if total == 0:
        pass
    elif max_workers == 1:
        for idx, cand in enumerate(candidates, start=1):
            print(f"[{idx}/{total}] 生成运行时处理策略: {cand['source_rule_uid']} -> {cand['target_rule_uid']} ({cand['association_kind']})")
            refinement = report_map.get(cand.get("candidate_key"))
            policy = generate_policy_for_candidate(cand, refinement, tcae_rule_map, home_context, prompt, no_ai=no_ai, rate_limiter=rate_limiter)
            results[idx] = build_record(idx, cand, policy, refinement)
    else:
        print(f"并发生成运行时策略：workers={max_workers}, rpm={rpm}, candidates={total}")

        def task(idx: int, cand: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            print(f"[提交 {idx}/{total}] {cand['source_rule_uid']} -> {cand['target_rule_uid']} ({cand['association_kind']})")
            refinement = report_map.get(cand.get("candidate_key"))
            policy = generate_policy_for_candidate(cand, refinement, tcae_rule_map, home_context, prompt, no_ai=False, rate_limiter=rate_limiter)
            return idx, build_record(idx, cand, policy, refinement)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(task, idx, cand): idx for idx, cand in enumerate(candidates, start=1)}
            for future in as_completed(future_map):
                idx, record = future.result()
                print(f"[完成 {idx}/{total}] {record['source_rule_uid']} -> {record['target_rule_uid']} ({record['policy']['strategy_name']})")
                results[idx] = record

    resolution_rules = [results[idx] for idx in sorted(results)]
    strategy_counts = Counter(r["policy"]["strategy_name"] for r in resolution_rules)
    review_count = sum(1 for r in resolution_rules if r["policy"].get("needs_human_review"))
    assoc_counts = Counter(c["association_kind"] for c in candidates)
    coverage_counts = Counter((report_map.get(c.get("candidate_key")) or {}).get("coverage_status", "no_iterative_report") for c in candidates)

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "runtime_resolution_after_iterative_refinement" if not no_ai else "runtime_resolution_no_ai",
        "rpm": rpm,
        "workers": max_workers,
        "candidate_extraction": {
            "mode": "terminal_only",
            "description": "Only associations targeting the terminal unexpected-action rule are converted into runtime-resolution candidates."
        },
        "strategy_templates": RESOLUTION_STRATEGY_TEMPLATES,
        "resolution_rules": resolution_rules,
        "source": {
            "devices_generated_at": devices.get("generated_at"),
            "channels_generated_at": channels.get("generated_at"),
            "zones_generated_at": zones.get("generated_at"),
            "tcae_generated_at": tcae.get("generated_at"),
            "normal_config_generated_at": normal_config.get("generated_at"),
            "ustg_generated_at": ustg.get("generated_at"),
            "iterative_refinement_plan_generated_at": (iterative_plan or {}).get("generated_at"),
        },
        "summary": {
            "candidate_association_count": len(candidates),
            "resolution_rule_count": len(resolution_rules),
            "association_kinds": dict(sorted(assoc_counts.items())),
            "coverage_status_counts": dict(sorted(coverage_counts.items())),
            "strategy_counts": dict(sorted(strategy_counts.items())),
            "needs_human_review_count": review_count,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate runtime resolution rules for residual unexpected scenarios.")
    parser.add_argument("--rpm", type=int, default=10, help="Maximum AI requests per minute. Default: 10")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent worker count. Default: min(rpm, candidate_count).")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of candidates to process.")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI calls and write fallback default policies requiring human review.")
    parser.add_argument("--keep-prompt", action="store_true", help="Do not auto-fill system_prompts.resolution_rules in config.json.")
    args = parser.parse_args()

    if args.rpm < 1:
        raise ValueError("--rpm must be >= 1")
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be >= 1")

    config = load_config()
    if not args.keep_prompt and ensure_system_prompts(config):
        save_config(config)
        print("已向 config.json 写入/补齐 resolution_rules system prompt。")

    load_env()

    devices = load_json(get_output_path(config, "devices"))
    channels = load_json(get_output_path(config, "channels"))
    zones = load_json(get_output_path(config, "zones"))
    tcae = load_json(get_output_path(config, "tcae"))
    normal_config = load_json(get_output_path(config, "normal_config"))
    ustg = load_json(get_output_path(config, "unexpected_state_transition_graph"))
    iterative_plan_path = get_output_path(config, "iterative_refinement_plan")
    iterative_plan = load_json(iterative_plan_path) if iterative_plan_path.exists() else None

    result = build_runtime_resolution_rules(
        devices,
        channels,
        zones,
        tcae,
        normal_config,
        ustg,
        iterative_plan,
        config,
        rpm=args.rpm,
        workers=args.workers,
        limit=args.limit,
        no_ai=args.no_ai,
    )

    resolution_rules_path = get_output_path(config, "resolution_rules")
    write_json(resolution_rules_path, result)
    print(f"\n已生成最终运行时处理策略: {resolution_rules_path}")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
