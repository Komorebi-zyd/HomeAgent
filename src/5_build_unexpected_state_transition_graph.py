"""
Step 5: Build the Unexpected State Transition Graph (USTG).

Input:
- configurations/home/tcae.json
- configurations/home/rule_association_graph.json
- configurations/home/normal_config.json

Output:
- configurations/home/unexpected_state_transition_graph.json
- optionally images/unexpected_state_transition_graph.png when --draw is provided

This script does NOT generate normal_config.json. It assumes the entity normal
configuration has already been generated/reviewed, and uses it to prune the Rule
Association Graph into the subgraph containing paths that may lead to weak
unexpected post-states.

Design principles:
- No entity semantics are inferred from user-defined object names.
- Normality is determined only by normal_config.json.
- Action post-states are read from the platform-independent TCAE model.
- Rule associations are read from the component-level Rule Association Graph.
- Static pruning uses weak unexpected state transitions: N_e(v') = 0.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from common import get_image_dir, get_output_path, load_config, load_json, utc_now_iso, write_json


USTG_PSEUDOCODE = r"""
Algorithm BuildUnexpectedStateTransitionGraph(G_RA, TCAE, NormalConfig)
Input :
  G_RA = (V, E, ell), the Rule Association Graph
  TCAE rule set R = {r1, ..., rn}
  NormalConfig N, entity normal-state predicates
Output:
  G_UST = (V_U, E_U, ell_U), Unexpected State Transition Graph
  P_U, unexpected association paths
  Out_U, path-to-unexpected-post-state annotations

1  ActionPost <- empty map from action node id to post-state set
2  AbnormalActions <- empty map from action node id to unexpected post-states
3  for each rule r in TCAE.rules do
4      a_node <- node id of v_r^A
5      for each action a in A_r do
6          e <- target entity of action a
7          Posts <- abstract post-state set of a
8          for each v' in Posts do
9              add (e, v') to ActionPost[a_node]
10             if e is configured in NormalConfig and N_e(v') = 0 then
11                 add (e, v') to AbnormalActions[a_node]
12 end for

13 P_U <- empty list
14 for each terminal action node vA in AbnormalActions.keys do
15     perform bounded reverse DFS in G_RA from vA
16     during DFS, keep node sequence p and edge sequence ep
17     if p contains at least one association edge and last(p)=vA then
18         if PositiveOnly is false or path_polarity(p) = +1 then
19             add p to P_U
20             Out_U[p] <- AbnormalActions[vA]
21 end for

22 V_U <- all nodes appearing in paths P_U
23 E_U <- all edges appearing in paths P_U
24 ell_U <- restriction of ell to E_U
25 return G_UST = (V_U, E_U, ell_U), P_U, Out_U
""".strip()


# ---------------------------------------------------------------------------
# Generic value normalization for comparing action post-states with normality
# ---------------------------------------------------------------------------

def is_nan_number(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def parse_numeric_string(value: str) -> Optional[float]:
    try:
        stripped = value.strip()
        if stripped == "":
            return None
        return float(stripped)
    except ValueError:
        return None


def normalize_state_like(value: Any) -> Any:
    """Normalize common platform-level state spellings.

    This is not entity-name classification. It only normalizes generic state
    values that frequently appear in Home Assistant service outputs and user
    normal-state configuration.
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


def comparable_value_key(value: Any) -> str:
    """Return a stable comparison key for normal-state membership."""
    value = normalize_state_like(value)
    if value is None:
        return "null"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if is_nan_number(value):
            return "nan"
        return f"num:{float(value):.12g}"
    if isinstance(value, str):
        numeric = parse_numeric_string(value)
        if numeric is not None:
            return f"num:{numeric:.12g}"
        return f"str:{value}"
    return "json:" + json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def is_concrete_post_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return False
    if isinstance(value, str) and value.strip().lower() in {"", "unknown", "unavailable", "none", "toggle"}:
        return False
    return True


# ---------------------------------------------------------------------------
# Normal configuration parsing
# ---------------------------------------------------------------------------

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


def first_present(record: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[str], Any]:
    for key in keys:
        if key in record:
            return key, record[key]
    return None, None


def normalize_normal_record(entity_id: str, raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize one normal entity record.

    Accepted forms:
    - "entity.id": {"normal_values": ["off"], ...}
    - "entity.id": ["off"]
    - "entity.id": "off"
    - {"entity_id": "entity.id", "normal_values": ["off"]}
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        eid = str(raw.get("entity_id") or entity_id or "").strip()
        _, values = first_present(raw, NORMAL_VALUE_KEYS)
        if values is None:
            # If the dict itself looks like a predicate-only entry, skip it. This
            # script needs explicit normal values for weak static pruning.
            return None
        normal_values = listify(values)
        extra = dict(raw)
    else:
        eid = str(entity_id or "").strip()
        normal_values = listify(raw)
        extra = {}

    if not eid:
        return None

    concrete_values = [v for v in normal_values if v is not None]
    if not concrete_values:
        return None

    return {
        "entity_id": eid,
        "normal_values": concrete_values,
        "normal_value_keys": sorted({comparable_value_key(v) for v in concrete_values}),
        "safety_level": extra.get("safety_level") or extra.get("importance") or extra.get("level"),
        "reason": extra.get("reason") or extra.get("description") or extra.get("notes"),
        "raw": extra if isinstance(raw, dict) else raw,
    }


def iter_normal_config_candidates(normal_config: Dict[str, Any]) -> Iterable[Any]:
    """Yield possible normal-entity containers from flexible schemas."""
    for key in [
        "normal_entities",
        "entities",
        "entity_normals",
        "normal_config",
        "normals",
        "normality",
        "configs",
    ]:
        if key in normal_config:
            yield normal_config[key]
    # Some users may put entity_id keys at the top level.
    yield normal_config


def load_normal_entities(normal_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return entity_id -> normalized normal record."""
    out: Dict[str, Dict[str, Any]] = {}

    for container in iter_normal_config_candidates(normal_config):
        if isinstance(container, dict):
            for key, raw in container.items():
                if key in {
                    "schema_version",
                    "generated_at",
                    "method",
                    "summary",
                    "source",
                    "pseudocode",
                    "system_prompt",
                }:
                    continue
                if isinstance(raw, dict) and raw.get("entity_id"):
                    eid = str(raw.get("entity_id"))
                else:
                    eid = str(key)
                rec = normalize_normal_record(eid, raw)
                if rec:
                    out[rec["entity_id"]] = rec
        elif isinstance(container, list):
            for raw in container:
                if isinstance(raw, dict) and raw.get("entity_id"):
                    rec = normalize_normal_record(str(raw.get("entity_id")), raw)
                    if rec:
                        out[rec["entity_id"]] = rec

    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# TCAE action post-state extraction
# ---------------------------------------------------------------------------

def action_node_id(rule_uid: str) -> str:
    return f"{rule_uid}::A"


def extract_action_posts(tcae: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Extract Post(v_r^A) from TCAE actions.

    Return:
      action_node_id -> [{entity_id, post, operation, service, rule_uid, ...}]
    """
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rule in tcae.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        rule_uid = rule.get("rule_uid")
        if not rule_uid:
            continue
        node_id = action_node_id(str(rule_uid))
        for idx, action in enumerate(rule.get("A", []) or []):
            if not isinstance(action, dict):
                continue
            entity_id = action.get("target_entity")
            if not entity_id:
                continue
            post = action.get("post")
            out[node_id].append(
                {
                    "entity_id": entity_id,
                    "post": post,
                    "post_normalized": normalize_state_like(post),
                    "operation": action.get("operation"),
                    "service": action.get("service"),
                    "rule_uid": rule_uid,
                    "rule_alias": rule.get("alias") or rule.get("display_alias"),
                    "action_index": idx,
                    "raw_action": action,
                }
            )
    return dict(out)


def find_weak_unexpected_action_posts(
    action_posts: Dict[str, List[Dict[str, Any]]],
    normal_entities: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Return abnormal action-node posts and skipped uncertain posts."""
    abnormal: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    skipped_uncertain: List[Dict[str, Any]] = []

    for node_id, posts in action_posts.items():
        for post in posts:
            entity_id = post.get("entity_id")
            if entity_id not in normal_entities:
                continue
            if not is_concrete_post_value(post.get("post")):
                skipped_uncertain.append(
                    {
                        "action_node": node_id,
                        "entity_id": entity_id,
                        "post": post.get("post"),
                        "reason": "post value is unknown/toggle/non-concrete; weak static unexpectedness is not decided",
                    }
                )
                continue
            post_key = comparable_value_key(post.get("post"))
            normal_keys = set(normal_entities[entity_id].get("normal_value_keys", []))
            if post_key not in normal_keys:
                abnormal[node_id].append(
                    {
                        **post,
                        "normal_values": normal_entities[entity_id].get("normal_values", []),
                        "normal_value_keys": sorted(normal_keys),
                        "post_key": post_key,
                        "weak_unexpected": True,
                        "safety_level": normal_entities[entity_id].get("safety_level"),
                        "normal_reason": normal_entities[entity_id].get("reason"),
                    }
                )

    return dict(abnormal), skipped_uncertain


# ---------------------------------------------------------------------------
# Graph path search and subgraph extraction
# ---------------------------------------------------------------------------

def build_edge_maps(graph: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    nodes = {n["node_id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and n.get("node_id")}
    forward: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    reverse: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        src = edge.get("source")
        tgt = edge.get("target")
        if not src or not tgt:
            continue
        forward[src].append(edge)
        reverse[tgt].append(edge)
    return nodes, dict(forward), dict(reverse)


def path_polarity(edge_seq: List[Dict[str, Any]]) -> int:
    pol = 1
    for edge in edge_seq:
        edge_pol = edge.get("polarity", 1)
        try:
            edge_pol = int(edge_pol)
        except Exception:
            edge_pol = 1
        if edge_pol not in {-1, 1}:
            edge_pol = 1
        pol *= edge_pol
    return pol


def path_has_assoc(edge_seq: List[Dict[str, Any]]) -> bool:
    return any(edge.get("edge_group") == "assoc" or edge.get("kind") != "flow" for edge in edge_seq)


def make_path_signature(node_seq: List[str], edge_seq: List[Dict[str, Any]]) -> str:
    return json.dumps(
        {
            "nodes": node_seq,
            "edges": [e.get("edge_id") for e in edge_seq],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def find_unexpected_paths_for_terminal(
    terminal_node: str,
    reverse_edges: Dict[str, List[Dict[str, Any]]],
    unexpected_posts: List[Dict[str, Any]],
    max_depth: int = 6,
    max_paths: int = 500,
    positive_only: bool = False,
) -> List[Dict[str, Any]]:
    """Bounded reverse DFS from one abnormal action node.

    The DFS records any simple path that ends at terminal_node and contains at
    least one association edge. The returned path direction is forward.
    """
    results: List[Dict[str, Any]] = []
    seen_paths: Set[str] = set()

    def dfs(current: str, rev_nodes: List[str], rev_edges: List[Dict[str, Any]]) -> None:
        if len(results) >= max_paths:
            return

        # If current path already contains association evidence, record it.
        if rev_edges and path_has_assoc(rev_edges):
            node_seq = list(reversed(rev_nodes))
            edge_seq = list(reversed(rev_edges))
            pol = path_polarity(edge_seq)
            if (not positive_only) or pol == 1:
                sig = make_path_signature(node_seq, edge_seq)
                if sig not in seen_paths:
                    seen_paths.add(sig)
                    results.append(
                        {
                            "path_id": "",  # assigned later
                            "nodes": node_seq,
                            "edges": [e.get("edge_id") for e in edge_seq],
                            "edge_kinds": [e.get("kind") for e in edge_seq],
                            "path_polarity": pol,
                            "start_node": node_seq[0],
                            "terminal_action_node": terminal_node,
                            "length": len(edge_seq),
                            "out_U": unexpected_posts,
                        }
                    )

        if len(rev_edges) >= max_depth:
            return

        for edge in reverse_edges.get(current, []) or []:
            pred = edge.get("source")
            if not pred or pred in rev_nodes:
                continue
            dfs(pred, rev_nodes + [pred], rev_edges + [edge])

    dfs(terminal_node, [terminal_node], [])
    return results


def find_unexpected_paths(
    graph: Dict[str, Any],
    abnormal_actions: Dict[str, List[Dict[str, Any]]],
    max_depth: int = 6,
    max_paths_per_outcome: int = 500,
    positive_only: bool = False,
) -> List[Dict[str, Any]]:
    _, _, reverse_edges = build_edge_maps(graph)
    all_paths: List[Dict[str, Any]] = []
    for terminal_node in sorted(abnormal_actions):
        paths = find_unexpected_paths_for_terminal(
            terminal_node,
            reverse_edges,
            abnormal_actions[terminal_node],
            max_depth=max_depth,
            max_paths=max_paths_per_outcome,
            positive_only=positive_only,
        )
        all_paths.extend(paths)

    # Stable global IDs
    all_paths = sorted(all_paths, key=lambda p: (p["terminal_action_node"], p["length"], json.dumps(p["nodes"], ensure_ascii=False), json.dumps(p["edges"], ensure_ascii=False)))
    for idx, path in enumerate(all_paths, start=1):
        path["path_id"] = f"p{idx:05d}"
    return all_paths


def extract_subgraph(graph: Dict[str, Any], paths: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    node_ids: Set[str] = set()
    edge_ids: Set[str] = set()
    for path in paths:
        node_ids.update(path.get("nodes", []) or [])
        edge_ids.update(path.get("edges", []) or [])

    nodes = [n for n in graph.get("nodes", []) or [] if n.get("node_id") in node_ids]
    edges = [e for e in graph.get("edges", []) or [] if e.get("edge_id") in edge_ids]
    nodes = sorted(nodes, key=lambda n: n.get("node_id", ""))
    edges = sorted(edges, key=lambda e: e.get("edge_id", ""))
    return nodes, edges


# ---------------------------------------------------------------------------
# USTG builder
# ---------------------------------------------------------------------------

def build_unexpected_state_transition_graph(
    tcae: Dict[str, Any],
    rule_association_graph: Dict[str, Any],
    normal_config: Dict[str, Any],
    max_depth: int = 6,
    max_paths_per_outcome: int = 500,
    positive_only: bool = False,
) -> Dict[str, Any]:
    normal_entities = load_normal_entities(normal_config)
    action_posts = extract_action_posts(tcae)
    abnormal_actions, skipped_uncertain = find_weak_unexpected_action_posts(action_posts, normal_entities)
    paths = find_unexpected_paths(
        rule_association_graph,
        abnormal_actions,
        max_depth=max_depth,
        max_paths_per_outcome=max_paths_per_outcome,
        positive_only=positive_only,
    )
    nodes, edges = extract_subgraph(rule_association_graph, paths)

    nodes_by_type = Counter(n.get("node_type", "unknown") for n in nodes)
    edges_by_kind = Counter(e.get("kind", "unknown") for e in edges)
    paths_by_terminal = Counter(p.get("terminal_action_node", "unknown") for p in paths)
    paths_by_polarity = Counter(str(p.get("path_polarity")) for p in paths)

    return {
        "schema_version": "1.0",
        "generated_at": utc_now_iso(),
        "graph_type": "Unexpected State Transition Graph",
        "abbreviation": "USTG",
        "nodes": nodes,
        "edges": edges,
        "unexpected_paths": paths,
        "action_posts": dict(sorted(action_posts.items())),
        "abnormal_actions": dict(sorted(abnormal_actions.items())),
        "normal_entities": normal_entities,
        "skipped_uncertain_posts": skipped_uncertain,
        "parameters": {
            "max_depth": max_depth,
            "max_paths_per_outcome": max_paths_per_outcome,
            "positive_only": positive_only,
        },
        "source": {
            "tcae_schema_version": tcae.get("schema_version"),
            "tcae_generated_at": tcae.get("generated_at"),
            "rule_association_graph_schema_version": rule_association_graph.get("schema_version"),
            "rule_association_graph_generated_at": rule_association_graph.get("generated_at"),
            "normal_config_schema_version": normal_config.get("schema_version"),
            "normal_config_generated_at": normal_config.get("generated_at"),
        },
        "summary": {
            "normal_entity_count": len(normal_entities),
            "action_node_count": len(action_posts),
            "abnormal_action_node_count": len(abnormal_actions),
            "unexpected_path_count": len(paths),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes_by_type": dict(sorted(nodes_by_type.items())),
            "edges_by_kind": dict(sorted(edges_by_kind.items())),
            "paths_by_terminal_action": dict(sorted(paths_by_terminal.items())),
            "paths_by_polarity": dict(sorted(paths_by_polarity.items())),
            "skipped_uncertain_post_count": len(skipped_uncertain),
        },
        "pseudocode": USTG_PSEUDOCODE,
    }


# ---------------------------------------------------------------------------
# Optional visualization
# ---------------------------------------------------------------------------

def short_label(node: Dict[str, Any]) -> str:
    node_type = node.get("node_type")
    if node_type == "environment":
        return f"E\n{node.get('zone')}\n{node.get('channel')}"
    rule_uid = str(node.get("rule_uid", ""))
    suffix = rule_uid.split("automation.")[-1]
    first = suffix.split("_")[0].upper() if suffix else "R"
    comp = node.get("component", node_type)
    return f"{first}:{comp}"


def draw_graph(graph: Dict[str, Any], output_path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt  # type: ignore
        import networkx as nx  # type: ignore
    except ImportError:
        print("未安装 networkx/matplotlib，跳过图片生成。")
        return False

    G = nx.MultiDiGraph()
    for node in graph.get("nodes", []) or []:
        G.add_node(node["node_id"], node_type=node.get("node_type"), label=node.get("label", node["node_id"]))
    for edge in graph.get("edges", []) or []:
        G.add_edge(edge["source"], edge["target"], kind=edge.get("kind"), polarity=edge.get("polarity"))

    color_map = {
        "trigger": "#b3d9ff",
        "condition": "#ffe0a3",
        "action": "#c8f7c5",
        "environment": "#e0c3fc",
    }
    node_lookup = {n["node_id"]: n for n in graph.get("nodes", []) or []}
    node_colors = [color_map.get(node_lookup[n].get("node_type"), "#dddddd") for n in G.nodes]
    labels = {n: short_label(node_lookup[n]) for n in G.nodes}

    pos = nx.spring_layout(G, seed=47, k=1.3) if len(G.nodes) else {}
    plt.figure(figsize=(18, 12))
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=900, alpha=0.92)
    nx.draw_networkx_labels(G, pos, labels=labels, font_size=7)

    pos_edges = [(e["source"], e["target"]) for e in graph.get("edges", []) if e.get("polarity") == 1]
    neg_edges = [(e["source"], e["target"]) for e in graph.get("edges", []) if e.get("polarity") == -1]
    nx.draw_networkx_edges(G, pos, edgelist=pos_edges, edge_color="#2ca02c", arrows=True, alpha=0.5, width=1.4)
    nx.draw_networkx_edges(G, pos, edgelist=neg_edges, edge_color="#d62728", arrows=True, alpha=0.5, width=1.4)
    plt.axis("off")
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=220)
    plt.close()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Unexpected State Transition Graph from RAG, TCAE and normal_config.")
    parser.add_argument("--max-depth", type=int, default=6, help="Maximum number of edges in each association path. Default: 6")
    parser.add_argument("--max-paths-per-outcome", type=int, default=500, help="Path cap for each abnormal terminal action node. Default: 500")
    parser.add_argument("--positive-only", action="store_true", help="Keep only paths with positive path polarity. Default keeps both polarities for static over-approximation.")
    parser.add_argument("--draw", action="store_true", help="Also generate images/unexpected_state_transition_graph.png if optional drawing dependencies exist.")
    parser.add_argument("--print-pseudocode", action="store_true", help="Print USTG generation pseudocode and exit.")
    args = parser.parse_args()

    if args.print_pseudocode:
        print(USTG_PSEUDOCODE)
        return

    if args.max_depth < 1:
        raise ValueError("--max-depth must be >= 1")
    if args.max_paths_per_outcome < 1:
        raise ValueError("--max-paths-per-outcome must be >= 1")

    config = load_config()
    tcae_path = get_output_path(config, "tcae")
    rag_path = get_output_path(config, "rule_association_graph")
    normal_config_path = get_output_path(config, "normal_config")
    ustg_path = get_output_path(config, "unexpected_state_transition_graph")

    if not normal_config_path.exists():
        raise FileNotFoundError(
            f"normal_config.json not found: {normal_config_path}\n"
            "请先准备实体常态配置文件。该脚本不会生成常态配置，只根据常态配置剪枝生成非预期状态转换图。"
        )

    tcae = load_json(tcae_path)
    rag = load_json(rag_path)
    normal_config = load_json(normal_config_path)

    ustg = build_unexpected_state_transition_graph(
        tcae,
        rag,
        normal_config,
        max_depth=args.max_depth,
        max_paths_per_outcome=args.max_paths_per_outcome,
        positive_only=args.positive_only,
    )
    write_json(ustg_path, ustg)

    print(f"已生成非预期状态转换图: {ustg_path}")
    print(json.dumps(ustg.get("summary", {}), ensure_ascii=False, indent=2))

    if args.draw:
        image_path = get_image_dir(config) / "unexpected_state_transition_graph.png"
        if draw_graph(ustg, image_path):
            print(f"已生成非预期状态转换图图片: {image_path}")


if __name__ == "__main__":
    main()
