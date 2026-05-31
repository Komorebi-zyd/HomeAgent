"""
Step 3: Build the Rule Association Graph (RAG) from TCAE models.

Input:
- configurations/home/tcae.json

Output:
- configurations/home/rule_association_graph.json
- optionally images/rule_association_graph.png when --draw is provided and
  networkx/matplotlib are available.

The implementation is intentionally generic:
- It does not infer semantics from user-defined entity object names.
- It only uses structured TCAE fields: rule components, action targets/post
  states, environment references, environment effects, zones and channels.
- It constructs a component-level graph whose nodes are trigger/condition/action
  components and zone-channel environment parameters.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common import get_image_dir, get_optional_input_path, get_output_path, load_config, load_entity_registry, load_json, entity_display_name, utc_now_iso, write_json


# ---------------------------------------------------------------------------
# Pseudocode
# ---------------------------------------------------------------------------
RAG_PSEUDOCODE = r"""
Algorithm BuildRuleAssociationGraph(TCAE)
Input : TCAE rule set R = {r1, ..., rn}
Output: Rule Association Graph G_RA = (V, E, ell)

1  V <- empty set; E <- empty list
2  for each rule r in R do
3      add trigger node v_r^T to V
4      if C_r is not empty then
5          add condition node v_r^C to V
6      add action node v_r^A to V
7      if C_r is not empty then
8          add flow edge v_r^T -> v_r^C and v_r^C -> v_r^A
9      else
10         add flow edge v_r^T -> v_r^A
11     for each environment reference rho in E_r^T union E_r^C do
12         add environment node v_(rho.zone,rho.channel)^E to V
13     for each environment effect eta in E_r^A do
14         for each z in eta.reachable_zones do
15             add environment node v_(z,eta.channel)^E to V
16             add env-association edge v_r^A -> v_(z,eta.channel)^E
17             add indirect-action edge v_(z,eta.channel)^E -> v_r^A
18 end for

19 for each ordered rule pair (ri, rj), ri != rj do
20     for each action a in A_ri and trigger t in T_rj do
21         if a may make t true then
22             add direct-trigger edge v_ri^A -> v_rj^T
23     for each action a in A_ri and condition c in C_rj do
24         if a may make c true then
25             add direct-condition-allow edge v_ri^A -> v_rj^C
26         else if a may make c false then
27             add direct-condition-disable edge v_ri^A -> v_rj^C
28     for each action a in A_ri and action b in A_rj do
29         if target(a) = target(b) then
30             add direct-action edge v_ri^A -> v_rj^A
31 end for

32 for each rule r in R do
33     for each environment trigger reference rho in E_r^T do
34         add env-trigger edge v_(rho.zone,rho.channel)^E -> v_r^T
35     for each environment condition reference rho in E_r^C do
36         if increasing (rho.zone,rho.channel) helps satisfy rho then
37             add indirect-condition-allow edge v_(rho.zone,rho.channel)^E -> v_r^C
38         else
39             add indirect-condition-disable edge v_(rho.zone,rho.channel)^E -> v_r^C
40 end for

41 remove duplicate edges with identical source, target, kind, polarity and core metadata
42 assign stable edge identifiers
43 return G_RA
"""


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def safe_node_part(value: Any) -> str:
    """Sanitize a value for stable node ids without changing display labels."""
    text = str(value if value is not None else "unknown")
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^A-Za-z0-9_.:\-]+", "_", text)
    return text or "unknown"


def t_node(rule_uid: str) -> str:
    return f"{rule_uid}::T"


def c_node(rule_uid: str) -> str:
    return f"{rule_uid}::C"


def a_node(rule_uid: str) -> str:
    return f"{rule_uid}::A"


def env_node(zone: str, channel: str) -> str:
    return f"env::{safe_node_part(zone)}::{safe_node_part(channel)}"


def is_unknown_value(value: Any) -> bool:
    return value is None or value == "" or value == "unknown"


def as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(float(value)) if isinstance(value, float) else False:
            return None
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def normalize_state(value: Any) -> Any:
    """Normalize common HA-like state spellings, keeping unknown structured values."""
    if isinstance(value, str):
        v = value.strip().lower()
        mapping = {
            "true": "on",
            "false": "off",
            "open": "on",
            "closed": "off",
            "close": "off",
            "locked": "off",
            "lock": "off",
            "unlocked": "on",
            "unlock": "on",
            "enabled": "on",
            "disabled": "off",
        }
        return mapping.get(v, v)
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def unique(values: Iterable[Any]) -> List[Any]:
    seen = set()
    out: List[Any] = []
    for v in values:
        key = stable_json(v)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Predicate / relation reasoning
# ---------------------------------------------------------------------------

def direction_to_polarity(direction: Any) -> Optional[int]:
    d = str(direction).strip()
    if d in {"+1", "+", "1", "up", "increase", "↑"}:
        return +1
    if d in {"-1", "-", "down", "decrease", "↓"}:
        return -1
    return None


def relation_polarity(relation: Any) -> Optional[int]:
    """Return how an increasing environment parameter affects a reference.

    +1 means increase helps/facilitates the reference; -1 means increase
    suppresses/weakens the reference. For downward references, the edge polarity
    is -1, so a preceding decreasing env-association edge forms a positive path.
    """
    r = str(relation).strip()
    if r in {">", ">=", "↑", "above"}:
        return +1
    if r in {"<", "<=", "↓", "below"}:
        return -1
    return None


def numeric_condition_satisfied(post: Any, node: Dict[str, Any]) -> Optional[bool]:
    value = as_float(post)
    if value is None:
        return None
    ok = True
    if node.get("above") is not None:
        above = as_float(node.get("above"))
        if above is None:
            return None
        ok = ok and value > above
    if node.get("below") is not None:
        below = as_float(node.get("below"))
        if below is None:
            return None
        ok = ok and value < below
    return ok


def state_trigger_matches_action(trigger: Dict[str, Any], action: Dict[str, Any]) -> bool:
    if trigger.get("type") != "state":
        return False
    if trigger.get("entity_id") != action.get("target_entity"):
        return False
    post = normalize_state(action.get("post"))
    # A state trigger without a concrete 'to' value means a state-change/event
    # trigger. Any concrete action on the same entity may activate it.
    to_value = trigger.get("to")
    if to_value is None or to_value == "":
        return not isinstance(post, dict) and post != "toggle"
    return normalize_state(to_value) == post


def numeric_trigger_matches_action(trigger: Dict[str, Any], action: Dict[str, Any]) -> bool:
    if trigger.get("type") != "numeric_state":
        return False
    if trigger.get("entity_id") != action.get("target_entity"):
        return False
    # Static stage only checks whether the post value satisfies the threshold;
    # real threshold crossing must be verified at runtime.
    return numeric_condition_satisfied(action.get("post"), trigger) is True


def time_trigger_matches_action(trigger: Dict[str, Any], action: Dict[str, Any]) -> bool:
    if trigger.get("type") != "time":
        return False
    at = trigger.get("at")
    return isinstance(at, str) and at == action.get("target_entity")


def trigger_matches_action(trigger: Dict[str, Any], action: Dict[str, Any]) -> bool:
    return (
        state_trigger_matches_action(trigger, action)
        or numeric_trigger_matches_action(trigger, action)
        or time_trigger_matches_action(trigger, action)
    )


def state_condition_effect(action: Dict[str, Any], condition: Dict[str, Any]) -> Optional[int]:
    """Return +1 if action allows condition, -1 if it disables condition."""
    if condition.get("type") != "state":
        return None
    if condition.get("entity_id") != action.get("target_entity"):
        return None
    post = normalize_state(action.get("post"))
    required = normalize_state(condition.get("state"))
    if isinstance(post, dict) or post == "toggle" or is_unknown_value(post) or is_unknown_value(required):
        return None
    return +1 if post == required else -1


def numeric_condition_effect(action: Dict[str, Any], condition: Dict[str, Any]) -> Optional[int]:
    if condition.get("type") != "numeric_state":
        return None
    if condition.get("entity_id") != action.get("target_entity"):
        return None
    sat = numeric_condition_satisfied(action.get("post"), condition)
    if sat is None:
        return None
    return +1 if sat else -1


def condition_effect(action: Dict[str, Any], condition: Dict[str, Any]) -> Optional[int]:
    return state_condition_effect(action, condition) or numeric_condition_effect(action, condition)


def action_relation(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
    """Return polarity for direct-action association on the same target."""
    if not a.get("target_entity") or not b.get("target_entity"):
        return None
    if a.get("target_entity") != b.get("target_entity"):
        return None
    pa = normalize_state(a.get("post"))
    pb = normalize_state(b.get("post"))
    if pa == "toggle" or pb == "toggle" or isinstance(pa, dict) or isinstance(pb, dict):
        # Same target with uncertain post states is conservatively treated as
        # potentially divergent.
        return -1
    return +1 if pa == pb else -1


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

class GraphBuilder:
    def __init__(self, entity_display: Optional[Dict[str, str]] = None) -> None:
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.entity_display = entity_display or {}
        self.edges: List[Dict[str, Any]] = []
        self._edge_signatures: set[str] = set()

    def add_node(self, node_id: str, node_type: str, label: str, **attrs: Any) -> None:
        if node_id in self.nodes:
            # Merge lightweight metadata without overwriting existing values.
            existing = self.nodes[node_id]
            for k, v in attrs.items():
                if k not in existing or existing[k] in (None, "", [], {}):
                    existing[k] = v
            return
        self.nodes[node_id] = {
            "node_id": node_id,
            "node_type": node_type,
            "label": label,
            **attrs,
        }

    def add_edge(
        self,
        source: str,
        target: str,
        kind: str,
        polarity: int,
        edge_group: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        metadata = metadata or {}
        if polarity not in {-1, +1}:
            return
        # Core metadata captures the relation while avoiding duplicate edges that
        # only differ in AI explanation strings.
        core_metadata = {
            k: v
            for k, v in metadata.items()
            if k
            in {
                "rule_from",
                "rule_to",
                "entity_id",
                "target_entity",
                "channel",
                "zone",
                "relation",
                "threshold",
                "operation",
                "post",
                "effect_entity_id",
                "effect_operation",
            }
        }
        sig = stable_json(
            {
                "source": source,
                "target": target,
                "kind": kind,
                "polarity": polarity,
                "edge_group": edge_group,
                "metadata": core_metadata,
            }
        )
        if sig in self._edge_signatures:
            return
        self._edge_signatures.add(sig)
        self.edges.append(
            {
                "edge_id": "",  # filled in finalize
                "source": source,
                "target": target,
                "edge_group": edge_group,
                "kind": kind,
                "polarity": polarity,
                "metadata": metadata,
            }
        )

    def finalize(self) -> Dict[str, Any]:
        nodes = sorted(self.nodes.values(), key=lambda x: x["node_id"])
        edges = sorted(self.edges, key=lambda e: (e["source"], e["target"], e["kind"], e["polarity"], stable_json(e["metadata"])))
        for idx, edge in enumerate(edges, start=1):
            edge["edge_id"] = f"e{idx:05d}"
        nodes_by_type = Counter(n["node_type"] for n in nodes)
        edges_by_kind = Counter(e["kind"] for e in edges)
        edges_by_group = Counter(e["edge_group"] for e in edges)
        return {
            "schema_version": "1.0",
            "generated_at": utc_now_iso(),
            "graph_type": "Rule Association Graph",
            "abbreviation": "RAG",
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes_by_type": dict(sorted(nodes_by_type.items())),
                "edges_by_kind": dict(sorted(edges_by_kind.items())),
                "edges_by_group": dict(sorted(edges_by_group.items())),
            },
            "pseudocode": RAG_PSEUDOCODE.strip(),
        }


# ---------------------------------------------------------------------------
# Build nodes and edges
# ---------------------------------------------------------------------------

def effect_projections(effect: Dict[str, Any]) -> List[Dict[str, Any]]:
    channel = effect.get("channel")
    pol = direction_to_polarity(effect.get("direction"))
    if not channel or pol is None:
        return []
    projections: List[Dict[str, Any]] = []
    reachable = effect.get("reachable_zones") or effect.get("source_zones") or []
    for zone in reachable:
        if zone is None or zone == "":
            continue
        projections.append(
            {
                "zone": str(zone),
                "channel": str(channel),
                "polarity": pol,
                "effect": effect,
            }
        )
    return projections


def collect_env_refs(rule: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    return list((rule.get("E") or {}).get(key, []) or [])


def collect_env_effects(rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((rule.get("E") or {}).get("E_A", []) or [])


def has_conditions(rule: Dict[str, Any]) -> bool:
    return bool(rule.get("C"))


def add_component_nodes_and_flow(g: GraphBuilder, rules: List[Dict[str, Any]]) -> None:
    for rule in rules:
        rid = rule["rule_uid"]
        alias = rule.get("alias") or rid
        g.add_node(t_node(rid), "trigger", f"{alias} / Trigger", rule_uid=rid, rule_alias=alias, component="T")
        if has_conditions(rule):
            g.add_node(c_node(rid), "condition", f"{alias} / Condition", rule_uid=rid, rule_alias=alias, component="C")
        g.add_node(a_node(rid), "action", f"{alias} / Action", rule_uid=rid, rule_alias=alias, component="A")

        if has_conditions(rule):
            g.add_edge(
                t_node(rid),
                c_node(rid),
                kind="flow",
                polarity=+1,
                edge_group="intra",
                metadata={"rule_uid": rid, "description": "trigger-to-condition internal flow"},
            )
            g.add_edge(
                c_node(rid),
                a_node(rid),
                kind="flow",
                polarity=+1,
                edge_group="intra",
                metadata={"rule_uid": rid, "description": "condition-to-action internal flow"},
            )
        else:
            g.add_edge(
                t_node(rid),
                a_node(rid),
                kind="flow",
                polarity=+1,
                edge_group="intra",
                metadata={"rule_uid": rid, "description": "trigger-to-action internal flow"},
            )


def add_environment_nodes(g: GraphBuilder, rules: List[Dict[str, Any]]) -> None:
    for rule in rules:
        for ref in collect_env_refs(rule, "E_T") + collect_env_refs(rule, "E_C"):
            zone = ref.get("zone")
            channel = ref.get("channel")
            if zone and channel:
                g.add_node(
                    env_node(str(zone), str(channel)),
                    "environment",
                    f"{zone} / {channel}",
                    zone=str(zone),
                    channel=str(channel),
                )
        for eff in collect_env_effects(rule):
            for proj in effect_projections(eff):
                g.add_node(
                    env_node(proj["zone"], proj["channel"]),
                    "environment",
                    f"{proj['zone']} / {proj['channel']}",
                    zone=proj["zone"],
                    channel=proj["channel"],
                )


def add_action_environment_edges(g: GraphBuilder, rules: List[Dict[str, Any]]) -> None:
    """Add action-env and env-action association edges.

    The pair A -> E and E -> A makes it possible to represent indirect paths:
    v_ri^A -> v_(z,c)^E -> v_rj^T/C/A.
    """
    for rule in rules:
        rid = rule["rule_uid"]
        for eff in collect_env_effects(rule):
            for proj in effect_projections(eff):
                eid = env_node(proj["zone"], proj["channel"])
                metadata = {
                    "rule_from": rid,
                    "effect_entity_id": eff.get("entity_id"),
                    "effect_operation": eff.get("operation"),
                    "zone": proj["zone"],
                    "channel": proj["channel"],
                    "direction": eff.get("direction"),
                    "mu": eff.get("mu"),
                    "lambda": eff.get("lambda"),
                    "duration": eff.get("duration"),
                    "confidence": eff.get("confidence"),
                    "reason": eff.get("reason"),
                }
                g.add_edge(a_node(rid), eid, "env-association", proj["polarity"], "assoc", metadata)
                g.add_edge(eid, a_node(rid), "indirect-action", proj["polarity"], "assoc", metadata)


def add_environment_reference_edges(g: GraphBuilder, rules: List[Dict[str, Any]]) -> None:
    for rule in rules:
        rid = rule["rule_uid"]
        # Environment -> Trigger
        for ref in collect_env_refs(rule, "E_T"):
            zone = ref.get("zone")
            channel = ref.get("channel")
            pol = relation_polarity(ref.get("relation"))
            if not zone or not channel or pol is None:
                continue
            g.add_edge(
                env_node(str(zone), str(channel)),
                t_node(rid),
                "env-trigger",
                pol,
                "assoc",
                {
                    "rule_to": rid,
                    "entity_id": ref.get("entity_id"),
                    "zone": zone,
                    "channel": channel,
                    "relation": ref.get("relation"),
                    "threshold": ref.get("threshold"),
                    "confidence": ref.get("confidence"),
                },
            )

        # Environment -> Condition
        if has_conditions(rule):
            for ref in collect_env_refs(rule, "E_C"):
                zone = ref.get("zone")
                channel = ref.get("channel")
                pol = relation_polarity(ref.get("relation"))
                if not zone or not channel or pol is None:
                    continue
                kind = "indirect-condition-allow" if pol == +1 else "indirect-condition-disable"
                g.add_edge(
                    env_node(str(zone), str(channel)),
                    c_node(rid),
                    kind,
                    pol,
                    "assoc",
                    {
                        "rule_to": rid,
                        "entity_id": ref.get("entity_id"),
                        "zone": zone,
                        "channel": channel,
                        "relation": ref.get("relation"),
                        "threshold": ref.get("threshold"),
                        "confidence": ref.get("confidence"),
                    },
                )


def add_direct_rule_association_edges(g: GraphBuilder, rules: List[Dict[str, Any]]) -> None:
    for ri in rules:
        rid_i = ri["rule_uid"]
        for rj in rules:
            rid_j = rj["rule_uid"]
            if rid_i == rid_j:
                continue

            # Action -> Trigger
            for action in ri.get("A", []) or []:
                for trigger in rj.get("T", []) or []:
                    if trigger_matches_action(trigger, action):
                        g.add_edge(
                            a_node(rid_i),
                            t_node(rid_j),
                            "direct-trigger",
                            +1,
                            "assoc",
                            {
                                "rule_from": rid_i,
                                "rule_to": rid_j,
                                "target_entity": action.get("target_entity"),
                                "operation": action.get("operation"),
                                "post": action.get("post"),
                                "trigger": trigger,
                            },
                        )

            # Action -> Condition
            if has_conditions(rj):
                for action in ri.get("A", []) or []:
                    for condition in rj.get("C", []) or []:
                        pol = condition_effect(action, condition)
                        if pol is None:
                            continue
                        kind = "direct-condition-allow" if pol == +1 else "direct-condition-disable"
                        g.add_edge(
                            a_node(rid_i),
                            c_node(rid_j),
                            kind,
                            pol,
                            "assoc",
                            {
                                "rule_from": rid_i,
                                "rule_to": rid_j,
                                "target_entity": action.get("target_entity"),
                                "operation": action.get("operation"),
                                "post": action.get("post"),
                                "condition": condition,
                            },
                        )

            # Action -> Action
            for action_i in ri.get("A", []) or []:
                for action_j in rj.get("A", []) or []:
                    pol = action_relation(action_i, action_j)
                    if pol is None:
                        continue
                    g.add_edge(
                        a_node(rid_i),
                        a_node(rid_j),
                        "direct-action",
                        pol,
                        "assoc",
                        {
                            "rule_from": rid_i,
                            "rule_to": rid_j,
                            "target_entity": action_i.get("target_entity"),
                            "operation_from": action_i.get("operation"),
                            "operation_to": action_j.get("operation"),
                            "post_from": action_i.get("post"),
                            "post_to": action_j.get("post"),
                        },
                    )


def collect_entity_display_from_tcae(tcae: Dict[str, Any]) -> Dict[str, str]:
    display: Dict[str, str] = {}
    for rule in tcae.get("rules", []) or []:
        for eid, name in (rule.get("entity_display") or {}).items():
            if eid and name:
                display[str(eid)] = str(name)
    return display


def add_entity_display_to_edge_metadata(edge: Dict[str, Any], entity_display: Dict[str, str]) -> None:
    meta = edge.get("metadata") or {}
    for key in ["entity_id", "target_entity", "effect_entity_id"]:
        eid = meta.get(key)
        if isinstance(eid, str) and eid in entity_display:
            meta[f"{key}_display"] = entity_display[eid]


def build_rule_association_graph(tcae: Dict[str, Any], entity_display: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    rules = list(tcae.get("rules", []) or [])
    entity_display = entity_display or collect_entity_display_from_tcae(tcae)
    g = GraphBuilder(entity_display=entity_display)
    add_component_nodes_and_flow(g, rules)
    add_environment_nodes(g, rules)
    add_action_environment_edges(g, rules)
    add_environment_reference_edges(g, rules)
    add_direct_rule_association_edges(g, rules)
    graph = g.finalize()
    graph["source"] = {
        "model": tcae.get("model"),
        "schema_version": tcae.get("schema_version"),
        "generated_at": tcae.get("generated_at"),
        "rule_count": len(rules),
    }
    graph["summary"]["rule_count"] = len(rules)
    graph["entity_display"] = entity_display
    for edge in graph.get("edges", []):
        add_entity_display_to_edge_metadata(edge, entity_display)
    return graph


# ---------------------------------------------------------------------------
# Optional visualization
# ---------------------------------------------------------------------------

def draw_graph(graph: Dict[str, Any], output_path: Path) -> bool:
    """Draw a compact graph when optional dependencies are available."""
    try:
        import matplotlib.pyplot as plt  # type: ignore
        import networkx as nx  # type: ignore
    except ImportError:
        print("未安装 networkx/matplotlib，跳过图片生成。")
        return False

    G = nx.MultiDiGraph()
    for node in graph.get("nodes", []):
        G.add_node(node["node_id"], label=node.get("label", node["node_id"]), node_type=node.get("node_type"))
    for edge in graph.get("edges", []):
        G.add_edge(edge["source"], edge["target"], kind=edge.get("kind"), polarity=edge.get("polarity"))

    color_map = {
        "trigger": "#b3d9ff",
        "condition": "#ffe0a3",
        "action": "#c8f7c5",
        "environment": "#e0c3fc",
    }
    node_colors = [color_map.get(graph_node.get("node_type"), "#dddddd") for graph_node in graph.get("nodes", [])]

    # spring_layout is generic; this image is for quick inspection, not for paper figures.
    pos = nx.spring_layout(G, seed=42, k=1.2)
    plt.figure(figsize=(18, 12))
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=900, alpha=0.9)
    labels = {n["node_id"]: short_label(n) for n in graph.get("nodes", [])}
    nx.draw_networkx_labels(G, pos, labels=labels, font_size=7)

    pos_edges = [(e["source"], e["target"]) for e in graph.get("edges", []) if e.get("polarity") == 1]
    neg_edges = [(e["source"], e["target"]) for e in graph.get("edges", []) if e.get("polarity") == -1]
    nx.draw_networkx_edges(G, pos, edgelist=pos_edges, edge_color="#2ca02c", arrows=True, alpha=0.45, width=1.3)
    nx.draw_networkx_edges(G, pos, edgelist=neg_edges, edge_color="#d62728", arrows=True, alpha=0.45, width=1.3)
    plt.axis("off")
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=220)
    plt.close()
    return True


def short_label(node: Dict[str, Any]) -> str:
    ntype = node.get("node_type")
    if ntype == "environment":
        return f"E\n{node.get('zone')}\n{node.get('channel')}"
    rule_uid = str(node.get("rule_uid", ""))
    suffix = rule_uid.split("automation.")[-1]
    first = suffix.split("_")[0].upper() if suffix else "R"
    comp = node.get("component", ntype)
    return f"{first}:{comp}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Rule Association Graph from TCAE models.")
    parser.add_argument("--draw", action="store_true", help="Also generate images/rule_association_graph.png if optional drawing dependencies exist.")
    parser.add_argument("--print-pseudocode", action="store_true", help="Print the graph generation pseudocode and exit.")
    args = parser.parse_args()

    if args.print_pseudocode:
        print(RAG_PSEUDOCODE.strip())
        return

    config = load_config()
    tcae_path = get_output_path(config, "tcae")
    rag_path = get_output_path(config, "rule_association_graph")

    tcae = load_json(tcae_path)
    graph = build_rule_association_graph(tcae)
    write_json(rag_path, graph)

    print(f"已生成规则关联图: {rag_path}")
    print(json.dumps(graph.get("summary", {}), ensure_ascii=False, indent=2))

    if args.draw:
        image_path = get_image_dir(config) / "rule_association_graph.png"
        if draw_graph(graph, image_path):
            print(f"已生成规则关联图图片: {image_path}")


if __name__ == "__main__":
    main()
