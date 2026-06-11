"""
Step 6: Generate resolution rules for unexpected pairwise rule associations.

Input:
- configurations/home/devices.json
- configurations/home/channels.json
- configurations/home/zones.json
- configurations/home/tcae.json
- configurations/home/normal_config.json
- configurations/home/unexpected_state_transition_graph.json

Output:
- configurations/home/resolution_rules.json

Design principles:
- No system variable, IP, API URL, API key, model, or project path is hard-coded.
  Paths come from configurations/config.json. Runtime/AI variables come from .env
  or process environment variables through common.py.
- No entity semantics are inferred from user-defined object_id substrings. Entity
  names, aliases and descriptions are sent to AI only as semantic hints.
- Resolution generation is pairwise and local: a long USTG path is reduced into
  direct pairs A_i -> T/C/A_j or indirect pairs A_i -> E(z,c) -> T/C/A_j.
- AI receives home context, two TCAE rule contexts, association evidence, the
  unexpected outcomes, and a fixed strategy-template set. It returns a strict
  JSON policy selection.
- API calls are rate-limited. Default RPM is 10.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common import (
    HomeAgentError,
    call_ai_json,
    get_output_path,
    load_config,
    load_env,
    load_json,
    save_config,
    unique_list,
    utc_now_iso,
    write_json,
)


DEFAULT_RESOLUTION_RULES_PROMPT = r"""
You are the runtime resolution-policy generation module of HomeAgent.
Your task is to select a handling strategy for one pairwise smart-home rule association that may lead to an unexpected state transition.

Important principles:
1. Do NOT rely on any specific human language or hard-coded entity-name words. Entity object names, aliases, descriptions and display names are semantic hints only.
2. Use only the supplied structured context: Home Assistant domains, services, action post-states, TCAE trigger/condition/action structures, entity-channel bindings, human-reviewed zone bindings, normal-state configuration, association evidence and unexpected outcomes.
3. Decide for the given pair of rules only. The association may be direct, such as A1 -> T2, or indirect through an environment node, such as A1 -> E(zone, channel) -> T2.
4. Prefer safety, security, emergency availability, and critical-resource continuity over comfort or convenience. Do not over-intervene when the association is benign or when the unexpected outcome is low confidence.
5. If a target rule may drive an entity away from its configured normal state, choose a strategy that prevents or compensates that target action unless the target rule has stronger emergency/safety justification.
6. If both rules are safety-critical, choose the strategy that preserves the more critical normal-state requirement, and set needs_human_review=true.
7. Select exactly one strategy from strategy_templates by strategy_id. Do not invent new strategies.
8. Return strict JSON only. Do not include markdown or commentary.

Output schema:
{
  "strategy_id": 0,
  "strategy_name": "one strategy_name from strategy_templates",
  "confidence": 0.0,
  "reason": "brief but concrete reason grounded in the supplied context",
  "runtime_notes": "brief notes about when/how to apply the policy at runtime",
  "needs_human_review": true
}
""".strip()


RESOLUTION_STRATEGY_TEMPLATES: List[Dict[str, Any]] = [
    {
        "strategy_id": 0,
        "strategy_name": "default",
        "description": "Default execution without intervention.",
        "runtime_effect": "Let both rules execute according to Home Assistant's default order.",
    },
    {
        "strategy_id": 1,
        "strategy_name": "only_first_triggered",
        "description": "Only execute the rule that triggered first and block the later rule's action.",
        "runtime_effect": "If the source rule has already executed and the target rule is about to execute, stop the target rule. If the target rule executed first, stop or compensate the source according to runtime ordering.",
    },
    {
        "strategy_id": 2,
        "strategy_name": "only_later_triggered",
        "description": "Only execute the rule that triggered later and compensate or roll back the earlier rule's action if necessary.",
        "runtime_effect": "Allow the later rule to proceed and cancel/restore the earlier rule's affected entities when runtime evidence supports the association.",
    },
    {
        "strategy_id": 3,
        "strategy_name": "force_lexicographic_first",
        "description": "Force the rule with lexicographically smaller rule_uid to prevail, regardless of runtime trigger order.",
        "runtime_effect": "Preserve the lexicographic-first rule's action effects and block or compensate the lexicographic-second rule.",
    },
    {
        "strategy_id": 4,
        "strategy_name": "force_lexicographic_second",
        "description": "Force the rule with lexicographically larger rule_uid to prevail, regardless of runtime trigger order.",
        "runtime_effect": "Preserve the lexicographic-second rule's action effects and block or compensate the lexicographic-first rule.",
    },
    {
        "strategy_id": 5,
        "strategy_name": "both_end_with_lexicographic_second",
        "description": "Allow both rules to execute but make the lexicographically larger rule the final state contributor.",
        "runtime_effect": "If execution order is unfavorable, re-run or restore the lexicographic-second rule's action after the other rule.",
    },
    {
        "strategy_id": 6,
        "strategy_name": "both_end_with_lexicographic_first",
        "description": "Allow both rules to execute but make the lexicographically smaller rule the final state contributor.",
        "runtime_effect": "If execution order is unfavorable, re-run or restore the lexicographic-first rule's action after the other rule.",
    },
    {
        "strategy_id": 7,
        "strategy_name": "cancel_both",
        "description": "Cancel both rules when the association is highly unsafe and neither action should proceed automatically.",
        "runtime_effect": "Stop the current rule and compensate any previously executed associated rule action when possible.",
    },
]


DEFAULT_RESOLUTION_RULES_PSEUDOCODE = r"""
Algorithm GenerateResolutionRules(G_UST, TCAE, Devices, Channels, Zones, NormalConfig, Templates)
Input :
  G_UST        = (V_U, E_U, ell_U), unexpected state transition graph
  TCAE         = TCAE rule set R = {r1, ..., rn}
  Devices      = extracted Home Assistant entity information
  Channels     = entity-channel bindings
  Zones        = human-reviewed entity-zone bindings
  NormalConfig = entity normal-state predicates
  Templates    = predefined resolution strategy templates
Output:
  ResolutionRules, AI-selected handling policies for pairwise rule associations

1  EdgeMap <- map edge_id to edge in G_UST
2  NodeMap <- map node_id to node in G_UST
3  PairCandidates <- empty map

4  for each unexpected path p in G_UST.unexpected_paths do
5      EdgeSeq <- edges of p according to p.edges
6      NodeSeq <- nodes of p according to p.nodes

7      for each edge e in EdgeSeq do
8          if e.kind in {direct-trigger, direct-condition-allow,
                         direct-condition-disable, direct-action} then
9              if source(e) is action node v_ri^A and target(e) is component node v_rj^{T/C/A} then
10                 if ri != rj then
11                     key <- MakeCandidateKey(ri, rj, target_component, e.kind)
12                     merge edge e, path p, and p.out_U into PairCandidates[key]
13                 end if
14             end if
15         end if
16     end for

17     for each adjacent edge pair (e1, e2) in EdgeSeq do
18         if e1.kind = env-association and source(e1) is action node v_ri^A
19            and target(e1) is environment node v_{z,c}^E
20            and source(e2) = target(e1)
21            and e2.kind in {indirect-trigger, indirect-condition-allow,
                             indirect-condition-disable, indirect-action}
22            and target(e2) is component node v_rj^{T/C/A} then
23                if ri != rj then
24                    key <- MakeCandidateKey(ri, rj, target_component, e2.kind, z, c)
25                    merge edge pair (e1,e2), path p, and p.out_U into PairCandidates[key]
26                end if
27         end if
28     end for
29 end for

30 ResolutionRules <- empty list
31 HomeContext <- BuildHomeContext(Devices, Channels, Zones, NormalConfig)

32 for each candidate q in PairCandidates do
33     ri <- q.source_rule_uid
34     rj <- q.target_rule_uid
35     RuleContext <- {
36         source_rule: TCAE[ri],
37         target_rule: TCAE[rj],
38         association: q,
39         unexpected_outcomes: q.out_U,
40         strategy_templates: Templates,
41         home_context: HomeContext
42     }
43     response <- QueryAI(RuleContext) subject to RPM limit
44     policy <- ValidateAndNormalize(response, Templates)
45     add (q, policy) to ResolutionRules
46 end for

47 return ResolutionRules
""".strip()


# ---------------------------------------------------------------------------
# Config and prompt
# ---------------------------------------------------------------------------


def ensure_system_prompts(config: Dict[str, Any]) -> bool:
    prompts = config.setdefault("system_prompts", {})
    if not prompts.get("resolution_rules"):
        prompts["resolution_rules"] = DEFAULT_RESOLUTION_RULES_PROMPT
        return True
    return False


# ---------------------------------------------------------------------------
# Generic helpers
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


def is_action_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::A")


def is_trigger_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::T")


def is_condition_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.endswith("::C")


def is_env_node(node_id: str) -> bool:
    return isinstance(node_id, str) and node_id.startswith("env::")


def rule_uid_from_component_node(node_id: str) -> Optional[str]:
    if not isinstance(node_id, str) or "::" not in node_id:
        return None
    if node_id.startswith("env::"):
        return None
    return node_id.rsplit("::", 1)[0]


def component_from_node(node_id: str) -> Optional[str]:
    if not isinstance(node_id, str) or "::" not in node_id:
        return None
    return node_id.rsplit("::", 1)[1]


def compact_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """Return AI-facing TCAE rule context without the full raw YAML blob."""
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


def node_brief(node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not node:
        return {}
    return {
        "node_id": node.get("node_id"),
        "node_type": node.get("node_type"),
        "label": node.get("label"),
        "rule_uid": node.get("rule_uid"),
        "rule_alias": node.get("rule_alias"),
        "component": node.get("component"),
        "zone": node.get("zone"),
        "channel": node.get("channel"),
    }


def dedupe_dict_list(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        key = stable_json(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Home context construction
# ---------------------------------------------------------------------------


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


def build_home_context(
    devices: Dict[str, Any],
    channels: Dict[str, Any],
    zones: Dict[str, Any],
    normal_config: Dict[str, Any],
) -> Dict[str, Any]:
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
# Candidate extraction from USTG
# ---------------------------------------------------------------------------

DIRECT_TARGET_KINDS = {
    "direct-trigger",
    "direct-condition-allow",
    "direct-condition-disable",
    "direct-action",
}

INDIRECT_TARGET_KINDS = {
    "indirect-trigger",
    "indirect-condition-allow",
    "indirect-condition-disable",
    "indirect-action",
}


def target_component_type(node_id: str) -> str:
    comp = component_from_node(node_id)
    if comp == "T":
        return "trigger"
    if comp == "C":
        return "condition"
    if comp == "A":
        return "action"
    return "unknown"


def make_candidate_key(
    source_rule: str,
    target_rule: str,
    target_component: str,
    association_kind: str,
    via_env: Optional[Dict[str, Any]] = None,
) -> str:
    via = ""
    if via_env:
        via = f"|{via_env.get('zone','')}|{via_env.get('channel','')}"
    return f"{source_rule}->{target_rule}|{target_component}|{association_kind}{via}"


def empty_candidate(
    key: str,
    source_rule: str,
    target_rule: str,
    target_component: str,
    association_kind: str,
    association_mode: str,
    via_environment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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


def merge_candidate_evidence(
    candidate: Dict[str, Any],
    edges: List[Dict[str, Any]],
    path: Dict[str, Any],
) -> None:
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


def add_candidate(
    candidates: Dict[str, Dict[str, Any]],
    source_rule: str,
    target_rule: str,
    target_component: str,
    association_kind: str,
    association_mode: str,
    edges: List[Dict[str, Any]],
    path: Dict[str, Any],
    via_environment: Optional[Dict[str, Any]] = None,
) -> None:
    if not source_rule or not target_rule or source_rule == target_rule:
        return
    key = make_candidate_key(source_rule, target_rule, target_component, association_kind, via_environment)
    if key not in candidates:
        candidates[key] = empty_candidate(
            key,
            source_rule,
            target_rule,
            target_component,
            association_kind,
            association_mode,
            via_environment,
        )
    merge_candidate_evidence(candidates[key], edges, path)


def extract_pairwise_candidates(ustg: Dict[str, Any]) -> List[Dict[str, Any]]:
    edge_map = {e.get("edge_id"): e for e in ustg.get("edges", []) or [] if isinstance(e, dict) and e.get("edge_id")}
    node_map = {n.get("node_id"): n for n in ustg.get("nodes", []) or [] if isinstance(n, dict) and n.get("node_id")}
    candidates: Dict[str, Dict[str, Any]] = {}

    for path in ustg.get("unexpected_paths", []) or []:
        if not isinstance(path, dict):
            continue
        edge_seq = [edge_map[eid] for eid in path.get("edges", []) if eid in edge_map]

        # Direct local associations: A_i -> T/C/A_j
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
            add_candidate(
                candidates,
                source_rule or "",
                target_rule or "",
                target_component_type(tgt),
                str(kind),
                "direct",
                [edge],
                path,
            )

        # Indirect local associations: A_i -> E(z,c) -> T/C/A_j
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
            env_node_data = node_map.get(env, {})
            via = {
                "node_id": env,
                "zone": env_node_data.get("zone") or (e1.get("metadata") or {}).get("zone") or (e2.get("metadata") or {}).get("zone"),
                "channel": env_node_data.get("channel") or (e1.get("metadata") or {}).get("channel") or (e2.get("metadata") or {}).get("channel"),
            }
            add_candidate(
                candidates,
                source_rule or "",
                target_rule or "",
                target_component_type(tgt),
                str(e2.get("kind")),
                "indirect",
                [e1, e2],
                path,
                via_environment=via,
            )

    out = list(candidates.values())
    for cand in out:
        cand["edge_ids"] = sorted(unique_list(cand.get("edge_ids", [])))
        cand["path_ids"] = sorted(unique_list(cand.get("path_ids", [])))
        cand["unexpected_outcomes"] = dedupe_dict_list(cand.get("unexpected_outcomes", []))
        cand["path_polarities"] = sorted(unique_list(cand.get("path_polarities", [])), key=lambda x: str(x))
    out = sorted(out, key=lambda c: (c["source_rule_uid"], c["target_rule_uid"], c["target_component"], c["association_kind"], stable_json(c.get("via_environment"))))
    for idx, cand in enumerate(out, start=1):
        cand["candidate_id"] = f"ra{idx:05d}"
    return out


# ---------------------------------------------------------------------------
# AI policy generation and validation
# ---------------------------------------------------------------------------


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
        # Try strategy_name lookup.
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
        "needs_human_review": bool(ai_data.get("needs_human_review", confidence < 0.75)),
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


def build_ai_payload(
    candidate: Dict[str, Any],
    source_rule: Dict[str, Any],
    target_rule: Dict[str, Any],
    home_context: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "task": "Choose one runtime resolution strategy for a pairwise smart-home rule association.",
        "home_context": home_context,
        "source_rule": compact_rule(source_rule),
        "target_rule": compact_rule(target_rule),
        "association_candidate": candidate,
        "strategy_templates": RESOLUTION_STRATEGY_TEMPLATES,
        "constraints": {
            "select_exactly_one_strategy": True,
            "strategy_id_must_be_one_of": [t["strategy_id"] for t in RESOLUTION_STRATEGY_TEMPLATES],
            "return_strict_json_only": True,
        },
    }


class RateLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, int(rpm))
        self.interval = 60.0 / self.rpm
        self.last_call_at = 0.0

    def wait(self) -> None:
        now = time.time()
        elapsed = now - self.last_call_at
        if self.last_call_at > 0 and elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call_at = time.time()


def generate_policy_for_candidate(
    candidate: Dict[str, Any],
    tcae_rule_map: Dict[str, Dict[str, Any]],
    home_context: Dict[str, Any],
    system_prompt: str,
    no_ai: bool = False,
    rate_limiter: Optional[RateLimiter] = None,
) -> Dict[str, Any]:
    source_rule = tcae_rule_map.get(candidate.get("source_rule_uid"))
    target_rule = tcae_rule_map.get(candidate.get("target_rule_uid"))
    if not source_rule or not target_rule:
        return fallback_policy("Missing source or target rule context; human review required.")

    if no_ai:
        return fallback_policy("AI disabled by --no-ai. Default strategy requires human review.")

    payload = build_ai_payload(candidate, source_rule, target_rule, home_context)
    try:
        if rate_limiter:
            rate_limiter.wait()
        ai_data = call_ai_json(system_prompt, payload, temperature=0.0)
        return normalize_ai_policy(ai_data)
    except Exception as exc:
        return fallback_policy(f"AI resolution-policy generation failed: {exc}")


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_resolution_rules(
    devices: Dict[str, Any],
    channels: Dict[str, Any],
    zones: Dict[str, Any],
    tcae: Dict[str, Any],
    normal_config: Dict[str, Any],
    ustg: Dict[str, Any],
    config: Dict[str, Any],
    rpm: int = 10,
    no_ai: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    candidates = extract_pairwise_candidates(ustg)
    if limit is not None:
        candidates = candidates[: max(0, limit)]

    home_context = build_home_context(devices, channels, zones, normal_config)
    tcae_rule_map = {r.get("rule_uid"): r for r in tcae.get("rules", []) or [] if isinstance(r, dict) and r.get("rule_uid")}
    prompt = (config.get("system_prompts") or {}).get("resolution_rules") or DEFAULT_RESOLUTION_RULES_PROMPT
    rate_limiter = RateLimiter(rpm)

    resolution_rules: List[Dict[str, Any]] = []
    total = len(candidates)
    for idx, cand in enumerate(candidates, start=1):
        print(f"[{idx}/{total}] 生成处理策略: {cand['source_rule_uid']} -> {cand['target_rule_uid']} ({cand['association_kind']})")
        policy = generate_policy_for_candidate(
            cand,
            tcae_rule_map,
            home_context,
            prompt,
            no_ai=no_ai,
            rate_limiter=rate_limiter,
        )
        resolution_rules.append(
            {
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
                "policy": policy,
            }
        )

    strategy_counts = Counter(r["policy"]["strategy_name"] for r in resolution_rules)
    review_count = sum(1 for r in resolution_rules if r["policy"].get("needs_human_review"))
    assoc_counts = Counter(c["association_kind"] for c in candidates)

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "method": "ai_pairwise_local_association" if not no_ai else "fallback_no_ai_pairwise_local_association",
        "rpm": rpm,
        "strategy_templates": RESOLUTION_STRATEGY_TEMPLATES,
        "candidate_associations": candidates,
        "resolution_rules": resolution_rules,
        "source": {
            "devices_generated_at": devices.get("generated_at"),
            "channels_generated_at": channels.get("generated_at"),
            "zones_generated_at": zones.get("generated_at"),
            "tcae_generated_at": tcae.get("generated_at"),
            "normal_config_generated_at": normal_config.get("generated_at"),
            "ustg_generated_at": ustg.get("generated_at"),
        },
        "summary": {
            "candidate_association_count": len(candidates),
            "resolution_rule_count": len(resolution_rules),
            "association_kinds": dict(sorted(assoc_counts.items())),
            "strategy_counts": dict(sorted(strategy_counts.items())),
            "needs_human_review_count": review_count,
        },
        "pseudocode": DEFAULT_RESOLUTION_RULES_PSEUDOCODE,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AI-selected resolution rules for USTG pairwise rule associations.")
    parser.add_argument("--rpm", type=int, default=10, help="Maximum AI requests per minute. Default: 10")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI calls and write fallback default policies requiring human review.")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of candidates to process, useful for debugging.")
    parser.add_argument("--keep-prompt", action="store_true", help="Do not auto-fill system_prompts.resolution_rules in config.json.")
    parser.add_argument("--print-pseudocode", action="store_true", help="Print the resolution generation pseudocode and exit.")
    args = parser.parse_args()

    if args.print_pseudocode:
        print(DEFAULT_RESOLUTION_RULES_PSEUDOCODE)
        return
    if args.rpm < 1:
        raise ValueError("--rpm must be >= 1")

    config = load_config()
    if not args.keep_prompt and ensure_system_prompts(config):
        save_config(config)
        print("已向 config.json 写入默认 resolution_rules system prompt。")

    load_env()

    devices_path = get_output_path(config, "devices")
    channels_path = get_output_path(config, "channels")
    zones_path = get_output_path(config, "zones")
    tcae_path = get_output_path(config, "tcae")
    normal_config_path = get_output_path(config, "normal_config")
    ustg_path = get_output_path(config, "unexpected_state_transition_graph")
    resolution_rules_path = get_output_path(config, "resolution_rules")

    devices = load_json(devices_path)
    channels = load_json(channels_path)
    zones = load_json(zones_path)
    tcae = load_json(tcae_path)
    normal_config = load_json(normal_config_path)
    ustg = load_json(ustg_path)

    result = build_resolution_rules(
        devices,
        channels,
        zones,
        tcae,
        normal_config,
        ustg,
        config,
        rpm=args.rpm,
        no_ai=args.no_ai,
        limit=args.limit,
    )

    write_json(resolution_rules_path, result)
    print(f"\n已生成非预期状态处理策略: {resolution_rules_path}")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
