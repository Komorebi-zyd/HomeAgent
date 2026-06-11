"""
Step 7: Runtime graph-walk checker for HomeAgent.

This script runs as the HomeAgent-side runtime detection server. Home Assistant,
after source replacement, connects to this server at the two check points:

- before_condition
- after_condition

The server uses:
- the Unexpected State Transition Graph (USTG),
- pairwise resolution rules,
- a lazy runtime store,
- on-demand entity-state queries back to Home Assistant,

to decide whether the current rule activation is caused by a local rule
association that may lead to an unexpected result, and if so, which handling
commands should be returned.

Design principles:
- No system variables are hard-coded. IP/port are read from .env through
  common.py.
- No semantic classification is done from user-defined entity object names.
  All rule reasoning uses structured TCAE / USTG / resolution-rules data.
- Only nearest upstream local paths are checked:
    A_i -> current node
    A_i -> E(z,c) -> current node
  Remote paths crossing another rule's T/C/A flow are ignored at runtime.
- Lazy refresh only happens when a check event arrives. No full-device polling.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from common import (
    get_env_value,
    get_home_dir,
    get_output_path,
    load_config,
    load_env,
    load_json,
    utc_now_iso,
    write_json,
)


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

BUFFER_SIZE = 1048576
DEFAULT_HISTORY_LIMIT = 100
DEFAULT_TRIGGER_DELTA_SEC = 1.0
DEFAULT_CONDITION_DELTA_SEC = 1.0
DEFAULT_ACTION_DELTA_SEC = 1.0


# ---------------------------------------------------------------------------
# Generic helpers
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



def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None



def normalize_state_like(value: Any) -> Any:
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
            "disable": "off",
            "disabled": "off",
            "enable": "on",
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



def compare_value(current: Any, expected: Any) -> bool:
    cur = normalize_state_like(current)
    exp = normalize_state_like(expected)
    if cur is None or exp is None:
        return False
    cur_f = safe_float(cur)
    exp_f = safe_float(exp)
    if cur_f is not None and exp_f is not None:
        return abs(cur_f - exp_f) < 1e-6
    return str(cur) == str(exp)



def parse_iso_to_orderable(value: Optional[str]) -> float:
    """Use a stable lexical fallback if timestamp parsing is unnecessary.

    The Home Assistant side already sends ISO strings. For coarse runtime checks,
    lexical comparison on ISO strings is sufficient, but delta checks are only
    used when both sides provide parseable numeric ordering.
    """
    if not value:
        return 0.0
    # Keep simple and dependency-free.
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0



def time_diff_seconds(t1: Optional[str], t2: Optional[str]) -> Optional[float]:
    if not t1 or not t2:
        return None
    a = parse_iso_to_orderable(t1)
    b = parse_iso_to_orderable(t2)
    if a == 0.0 or b == 0.0:
        return None
    return abs(a - b)



def node_type_from_id(node_id: str) -> str:
    if node_id.startswith("env::"):
        return "environment"
    if node_id.endswith("::T"):
        return "trigger"
    if node_id.endswith("::C"):
        return "condition"
    if node_id.endswith("::A"):
        return "action"
    return "unknown"



def rule_uid_from_node(node_id: str) -> Optional[str]:
    if "::" not in node_id or node_id.startswith("env::"):
        return None
    return node_id.rsplit("::", 1)[0]



def component_from_node(node_id: str) -> Optional[str]:
    if "::" not in node_id:
        return None
    return node_id.rsplit("::", 1)[1]



def t_node(rule_uid: str) -> str:
    return f"{rule_uid}::T"



def c_node(rule_uid: str) -> str:
    return f"{rule_uid}::C"



def a_node(rule_uid: str) -> str:
    return f"{rule_uid}::A"


# ---------------------------------------------------------------------------
# HA socket session
# ---------------------------------------------------------------------------


class HASession:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self._entities_cache: Optional[List[str]] = None

    def _recv(self) -> Optional[Dict[str, Any]]:
        data = self.conn.recv(BUFFER_SIZE)
        if not data:
            return None
        return json.loads(data.decode("utf-8"))

    def _send(self, message: Dict[str, Any]) -> None:
        self.conn.sendall(json.dumps(message).encode("utf-8"))

    def get_entities(self) -> List[str]:
        if self._entities_cache is not None:
            return self._entities_cache
        self._send({"type": 0})
        data = self._recv() or {}
        self._entities_cache = list(data.get("entities", []) or [])
        return self._entities_cache

    def get_entity_state(self, entity_id: str) -> Dict[str, Any]:
        self._send({"type": 1, "entity_id": entity_id})
        return self._recv() or {"entity_id": entity_id, "state": None, "last_changed": None, "last_triggered": None}

    def time_now(self) -> str:
        self._send({"type": 2})
        data = self._recv() or {}
        return str(data.get("time") or utc_now_iso())

    def send_commands(self, cmds: List[Dict[str, Any]]) -> None:
        self._send({"type": 3, "cmds": cmds})
        _ = self._recv()

    def finish(self) -> None:
        self._send({"type": -1})
        _ = self._recv()


# ---------------------------------------------------------------------------
# Runtime store
# ---------------------------------------------------------------------------


class RuntimeStore:
    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self.history_limit = max(1, int(history_limit))
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _default_record(self, node_id: str) -> Dict[str, Any]:
        return {
            "node_id": node_id,
            "node_type": node_type_from_id(node_id),
            "state": 0,
            "time": None,
            "conditions": [],
            "actions": [],
            "effects": [],
            "history": [],
        }

    def get(self, node_id: str) -> Dict[str, Any]:
        with self._lock:
            if node_id not in self.nodes:
                self.nodes[node_id] = self._default_record(node_id)
            return self.nodes[node_id]

    def set_current(self, node_id: str, **updates: Any) -> Dict[str, Any]:
        with self._lock:
            rec = self.get(node_id)
            for k, v in updates.items():
                rec[k] = v
            snapshot = {
                "time": rec.get("time"),
                "state": rec.get("state"),
                "conditions": list(rec.get("conditions", []) or []),
                "actions": list(rec.get("actions", []) or []),
                "effects": list(rec.get("effects", []) or []),
            }
            history: List[Dict[str, Any]] = rec.setdefault("history", [])
            history.append(snapshot)
            if len(history) > self.history_limit:
                del history[:-self.history_limit]
            return rec

    def append_action(self, node_id: str, action_item: Dict[str, Any], when: str) -> None:
        rec = self.get(node_id)
        actions = list(rec.get("actions", []) or [])
        actions.append(action_item)
        self.set_current(node_id, state=1 if actions else 0, time=when, actions=actions)

    def append_effect(self, node_id: str, effect_item: Dict[str, Any], when: str) -> None:
        rec = self.get(node_id)
        effects = list(rec.get("effects", []) or [])
        effects.append(effect_item)
        self.set_current(node_id, state=1 if effects else 0, time=when, effects=effects)

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "history_limit": self.history_limit,
                "nodes": self.nodes,
            }


# ---------------------------------------------------------------------------
# Graph walker core
# ---------------------------------------------------------------------------


@dataclass
class LocalPath:
    source_action_node: str
    target_node: str
    association_kind: str
    target_component: str
    direct_edge: Optional[Dict[str, Any]]
    env_node: Optional[str]
    env_edge: Optional[Dict[str, Any]]
    env_assoc_edge: Optional[Dict[str, Any]]
    candidate_key: str


class RuntimeGraphWalker:
    def __init__(
        self,
        config: Dict[str, Any],
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        trigger_delta_sec: float = DEFAULT_TRIGGER_DELTA_SEC,
        condition_delta_sec: float = DEFAULT_CONDITION_DELTA_SEC,
        action_delta_sec: float = DEFAULT_ACTION_DELTA_SEC,
    ) -> None:
        self.config = config
        self.history_limit = history_limit
        self.trigger_delta_sec = trigger_delta_sec
        self.condition_delta_sec = condition_delta_sec
        self.action_delta_sec = action_delta_sec

        self.tcae = load_json(get_output_path(config, "tcae"))
        self.ustg = load_json(get_output_path(config, "unexpected_state_transition_graph"))
        self.resolution_rules_doc = load_json(get_output_path(config, "resolution_rules"))
        self.channels = load_json(get_output_path(config, "channels"))
        self.zones = load_json(get_output_path(config, "zones"))

        self.rule_map = {r.get("rule_uid"): r for r in self.tcae.get("rules", []) or [] if isinstance(r, dict) and r.get("rule_uid")}
        self.node_map = {n.get("node_id"): n for n in self.ustg.get("nodes", []) or [] if isinstance(n, dict) and n.get("node_id")}
        self.edge_map = {e.get("edge_id"): e for e in self.ustg.get("edges", []) or [] if isinstance(e, dict) and e.get("edge_id")}
        self.in_edges: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.out_edges: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for edge in self.ustg.get("edges", []) or []:
            if not isinstance(edge, dict):
                continue
            src = edge.get("source")
            tgt = edge.get("target")
            if src and tgt:
                self.in_edges[tgt].append(edge)
                self.out_edges[src].append(edge)

        self.resolution_map = {
            r.get("candidate_key"): r
            for r in self.resolution_rules_doc.get("resolution_rules", []) or []
            if isinstance(r, dict) and r.get("candidate_key")
        }

        self.store = RuntimeStore(history_limit=history_limit)
        home_dir = get_home_dir(config)
        self.runtime_store_path = home_dir / "runtime_store.json"
        self.runtime_event_log_path = home_dir / "runtime_events.jsonl"

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def persist_store(self) -> None:
        write_json(self.runtime_store_path, self.store.dump())

    def append_event_log(self, item: Dict[str, Any]) -> None:
        self.runtime_event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.runtime_event_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Candidate-key reconstruction
    # ------------------------------------------------------------------
    def make_candidate_key(
        self,
        source_rule_uid: str,
        target_rule_uid: str,
        target_component: str,
        association_kind: str,
        via_environment: Optional[Dict[str, Any]] = None,
    ) -> str:
        via = ""
        if via_environment:
            via = f"|{via_environment.get('zone','')}|{via_environment.get('channel','')}"
        return f"{source_rule_uid}->{target_rule_uid}|{target_component}|{association_kind}{via}"

    # ------------------------------------------------------------------
    # Condition evaluation
    # ------------------------------------------------------------------
    def evaluate_single_condition(self, cond: Dict[str, Any], session: HASession, now_time: str) -> Dict[str, Any]:
        ctype = cond.get("type")
        result = {
            "type": ctype,
            "entity_id": cond.get("entity_id"),
            "raw": cond,
            "satisfied": False,
            "current_state": None,
        }
        if ctype == "state":
            entity_id = cond.get("entity_id")
            expected = cond.get("state")
            state = session.get_entity_state(entity_id).get("state") if entity_id else None
            result["current_state"] = state
            result["satisfied"] = compare_value(state, expected)
            return result
        if ctype == "numeric_state":
            entity_id = cond.get("entity_id")
            state = session.get_entity_state(entity_id).get("state") if entity_id else None
            result["current_state"] = state
            value = safe_float(normalize_state_like(state))
            ok = value is not None
            if ok and cond.get("above") is not None:
                above = safe_float(cond.get("above"))
                ok = ok and above is not None and value > above
            if ok and cond.get("below") is not None:
                below = safe_float(cond.get("below"))
                ok = ok and below is not None and value < below
            result["satisfied"] = bool(ok)
            return result
        if ctype == "time":
            # after_condition means HA already passed the check; before_condition
            # here uses only a conservative best-effort evaluation.
            result["current_state"] = now_time
            result["satisfied"] = True
            return result
        result["satisfied"] = False
        return result

    def evaluate_conditions(self, rule_uid: str, session: HASession, now_time: str) -> Tuple[bool, List[Dict[str, Any]]]:
        rule = self.rule_map.get(rule_uid) or {}
        conditions = list(rule.get("C", []) or [])
        if not conditions:
            return True, []
        details = [self.evaluate_single_condition(c, session, now_time) for c in conditions]
        overall = all(bool(d.get("satisfied")) for d in details)
        return overall, details

    # ------------------------------------------------------------------
    # Runtime-state activation / lazy refresh
    # ------------------------------------------------------------------
    def clear_rule_trigger_condition_state(self, rule_uid: str, when: str) -> None:
        self.store.set_current(t_node(rule_uid), state=0, time=when)
        if c_node(rule_uid) in self.node_map:
            self.store.set_current(c_node(rule_uid), state=0, time=when, conditions=[])

    def deactivate_action_node(self, action_node_id: str, when: str) -> None:
        rec = self.store.get(action_node_id)
        source_rule_uid = rule_uid_from_node(action_node_id)
        rec_actions = list(rec.get("actions", []) or [])
        rec["actions"] = []
        self.store.set_current(action_node_id, state=0, time=when, actions=[])

        # Important: action-state change must also update the environment nodes
        # pointed to by this action node, not just the rule's T/C nodes.
        for edge in self.out_edges.get(action_node_id, []) or []:
            if edge.get("kind") != "env-association":
                continue
            env_node_id = edge.get("target")
            if not env_node_id:
                continue
            env_rec = self.store.get(env_node_id)
            kept_effects = [
                eff for eff in list(env_rec.get("effects", []) or [])
                if eff.get("source_action_node") != action_node_id
            ]
            self.store.set_current(env_node_id, state=1 if kept_effects else 0, time=when, effects=kept_effects)

        if source_rule_uid:
            self.clear_rule_trigger_condition_state(source_rule_uid, when)

    def lazy_refresh_action_node(self, action_node_id: str, session: HASession, now_time: str) -> None:
        rec = self.store.get(action_node_id)
        actions = list(rec.get("actions", []) or [])
        if not actions:
            self.store.set_current(action_node_id, state=0, time=now_time, actions=[])
            return
        kept: List[Dict[str, Any]] = []
        for item in actions:
            entity_id = item.get("entity_id")
            expected_post = item.get("expected_post")
            if not entity_id:
                continue
            current = session.get_entity_state(entity_id).get("state")
            if compare_value(current, expected_post):
                kept.append(item)
        if kept:
            self.store.set_current(action_node_id, state=1, time=now_time, actions=kept)
        else:
            self.deactivate_action_node(action_node_id, now_time)

    def lazy_refresh_environment_node(self, env_node_id: str, session: HASession, now_time: str) -> None:
        env_rec = self.store.get(env_node_id)
        effects = list(env_rec.get("effects", []) or [])
        if not effects:
            self.store.set_current(env_node_id, state=0, time=now_time, effects=[])
            return
        kept: List[Dict[str, Any]] = []
        for eff in effects:
            source_action_node = eff.get("source_action_node")
            if not source_action_node:
                continue
            action_rec = self.store.get(source_action_node)
            if int(action_rec.get("state", 0)) != 1:
                continue
            # Re-check source action entity to ensure support still exists.
            entity_id = eff.get("entity_id")
            expected_post = eff.get("expected_post")
            current = session.get_entity_state(entity_id).get("state") if entity_id else None
            if entity_id and compare_value(current, expected_post):
                kept.append(eff)
        self.store.set_current(env_node_id, state=1 if kept else 0, time=now_time, effects=kept)

    def activate_current_action_node(self, rule_uid: str, session: HASession, now_time: str) -> None:
        rule = self.rule_map.get(rule_uid) or {}
        action_node_id = a_node(rule_uid)
        action_entries: List[Dict[str, Any]] = []
        env_entries_by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for action in list(rule.get("A", []) or []):
            entity_id = action.get("target_entity")
            if not entity_id:
                continue
            state_before = session.get_entity_state(entity_id).get("state")
            action_entry = {
                "entity_id": entity_id,
                "operation": action.get("operation"),
                "service": action.get("service"),
                "expected_post": action.get("post"),
                "previous_state": state_before,
                "time": now_time,
            }
            action_entries.append(action_entry)

        self.store.set_current(action_node_id, state=1 if action_entries else 0, time=now_time, actions=action_entries)
        self.store.set_current(t_node(rule_uid), state=1, time=now_time)
        if c_node(rule_uid) in self.node_map:
            cond_ok, cond_list = self.evaluate_conditions(rule_uid, session, now_time)
            self.store.set_current(c_node(rule_uid), state=1 if cond_ok else 0, time=now_time, conditions=cond_list)

        for edge in self.out_edges.get(action_node_id, []) or []:
            if edge.get("kind") != "env-association":
                continue
            env_node_id = edge.get("target")
            meta = edge.get("metadata", {}) or {}
            for action_entry in action_entries:
                if action_entry.get("entity_id") != meta.get("effect_entity_id"):
                    continue
                env_effect = {
                    "entity_id": action_entry.get("entity_id"),
                    "operation": action_entry.get("operation"),
                    "detail": f"{action_entry.get('operation')}->{action_entry.get('expected_post')}",
                    "polarity": edge.get("polarity"),
                    "source_action_node": action_node_id,
                    "expected_post": action_entry.get("expected_post"),
                    "time": now_time,
                }
                env_entries_by_node[env_node_id].append(env_effect)

        for env_node_id, env_effects in env_entries_by_node.items():
            env_rec = self.store.get(env_node_id)
            merged = list(env_rec.get("effects", []) or []) + env_effects
            self.store.set_current(env_node_id, state=1 if merged else 0, time=now_time, effects=merged)

    # ------------------------------------------------------------------
    # Local-path extraction
    # ------------------------------------------------------------------
    def build_local_paths_for_node(self, target_node_id: str, allowed_kinds: Iterable[str]) -> List[LocalPath]:
        allowed = set(allowed_kinds)
        paths: List[LocalPath] = []

        # Direct incoming from action
        for edge in self.in_edges.get(target_node_id, []) or []:
            if edge.get("kind") not in allowed:
                continue
            src = edge.get("source")
            if src and node_type_from_id(src) == "action":
                srule = rule_uid_from_node(src) or ""
                trule = rule_uid_from_node(target_node_id) or ""
                key = self.make_candidate_key(srule, trule, self._component_name(target_node_id), str(edge.get("kind")), None)
                paths.append(
                    LocalPath(
                        source_action_node=src,
                        target_node=target_node_id,
                        association_kind=str(edge.get("kind")),
                        target_component=self._component_name(target_node_id),
                        direct_edge=edge,
                        env_node=None,
                        env_edge=None,
                        env_assoc_edge=None,
                        candidate_key=key,
                    )
                )

        # Indirect incoming via environment
        for env_edge in self.in_edges.get(target_node_id, []) or []:
            if env_edge.get("kind") not in allowed:
                continue
            env_node_id = env_edge.get("source")
            if not env_node_id or node_type_from_id(env_node_id) != "environment":
                continue
            for assoc_edge in self.in_edges.get(env_node_id, []) or []:
                if assoc_edge.get("kind") != "env-association":
                    continue
                src = assoc_edge.get("source")
                if not src or node_type_from_id(src) != "action":
                    continue
                srule = rule_uid_from_node(src) or ""
                trule = rule_uid_from_node(target_node_id) or ""
                env_node = self.node_map.get(env_node_id, {})
                via = {"zone": env_node.get("zone"), "channel": env_node.get("channel")}
                key = self.make_candidate_key(srule, trule, self._component_name(target_node_id), str(env_edge.get("kind")), via)
                paths.append(
                    LocalPath(
                        source_action_node=src,
                        target_node=target_node_id,
                        association_kind=str(env_edge.get("kind")),
                        target_component=self._component_name(target_node_id),
                        direct_edge=None,
                        env_node=env_node_id,
                        env_edge=env_edge,
                        env_assoc_edge=assoc_edge,
                        candidate_key=key,
                    )
                )
        return paths

    def _component_name(self, node_id: str) -> str:
        comp = component_from_node(node_id)
        if comp == "T":
            return "trigger"
        if comp == "C":
            return "condition"
        if comp == "A":
            return "action"
        return "unknown"

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------
    def _path_source_active(self, path: LocalPath) -> bool:
        return int(self.store.get(path.source_action_node).get("state", 0)) == 1

    def _path_env_active(self, path: LocalPath) -> bool:
        if not path.env_node:
            return True
        return int(self.store.get(path.env_node).get("state", 0)) == 1

    def match_trigger_association(self, path: LocalPath, rule_uid: str, now_time: str) -> bool:
        if not self._path_source_active(path):
            return False
        if path.env_node and not self._path_env_active(path):
            return False
        src_time = self.store.get(path.source_action_node).get("time")
        dt = time_diff_seconds(src_time, now_time)
        if dt is not None and dt > self.trigger_delta_sec:
            return False
        return True

    def match_condition_disable_association(self, path: LocalPath, prev_state: Optional[int], current_state: int) -> bool:
        if not self._path_source_active(path):
            return False
        if path.env_node and not self._path_env_active(path):
            return False
        return prev_state == 1 and current_state == 0

    def match_condition_allow_association(self, path: LocalPath, prev_state: Optional[int], current_state: int) -> bool:
        if not self._path_source_active(path):
            return False
        if path.env_node and not self._path_env_active(path):
            return False
        return prev_state == 0 and current_state == 1

    def match_action_association(self, path: LocalPath, now_time: str) -> bool:
        if not self._path_source_active(path):
            return False
        if path.env_node and not self._path_env_active(path):
            return False
        src_time = self.store.get(path.source_action_node).get("time")
        dt = time_diff_seconds(src_time, now_time)
        if dt is not None and dt > self.action_delta_sec:
            # Action association does not require very short delay, but if the
            # source action is too old and no longer valid it should have been
            # lazy-refreshed out. Keep only a soft bound here.
            return int(self.store.get(path.source_action_node).get("state", 0)) == 1
        return True

    # ------------------------------------------------------------------
    # Resolution-rule selection
    # ------------------------------------------------------------------
    def severity_score(self, resolution_rule: Dict[str, Any]) -> Tuple[int, int]:
        level_map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        outcomes = list(resolution_rule.get("unexpected_outcomes", []) or [])
        max_level = max((level_map.get(str(o.get("safety_level") or "low"), 1) for o in outcomes), default=1)
        occ = len(list(resolution_rule.get("path_ids", []) or []))
        return max_level, occ

    def find_resolution_rule_for_path(self, path: LocalPath) -> Optional[Dict[str, Any]]:
        return self.resolution_map.get(path.candidate_key)

    def choose_best_match(self, matches: List[Tuple[LocalPath, Dict[str, Any]]]) -> Optional[Tuple[LocalPath, Dict[str, Any]]]:
        if not matches:
            return None
        ranked = sorted(matches, key=lambda item: self.severity_score(item[1]), reverse=True)
        return ranked[0]

    # ------------------------------------------------------------------
    # Policy to command mapping
    # ------------------------------------------------------------------
    def build_cancel_source_commands(self, source_rule_uid: str) -> List[Dict[str, Any]]:
        rec = self.store.get(a_node(source_rule_uid))
        actions = list(rec.get("actions", []) or [])
        if not actions:
            return []
        restore: Dict[str, Any] = {}
        for item in actions:
            entity_id = item.get("entity_id")
            previous_state = item.get("previous_state")
            if entity_id is not None and previous_state is not None:
                restore[entity_id] = previous_state
        if not restore:
            return []
        return [{"type": "cancel", "entity_id": source_rule_uid, "entities": restore}]

    def policy_allows_current_execution(self, strategy_name: str, current_rule_uid: str, source_rule_uid: str) -> bool:
        if strategy_name == "default":
            return True
        if strategy_name == "only_first_triggered":
            return False
        if strategy_name == "only_later_triggered":
            return True
        if strategy_name == "cancel_both":
            return False
        if strategy_name == "force_lexicographic_first":
            return current_rule_uid < source_rule_uid
        if strategy_name == "force_lexicographic_second":
            return current_rule_uid > source_rule_uid
        if strategy_name == "both_end_with_lexicographic_first":
            return current_rule_uid < source_rule_uid
        if strategy_name == "both_end_with_lexicographic_second":
            return current_rule_uid > source_rule_uid
        return True

    def policy_requires_cancel_source(self, strategy_name: str, current_rule_uid: str, source_rule_uid: str) -> bool:
        if strategy_name in {"only_later_triggered", "cancel_both"}:
            return True
        if strategy_name == "force_lexicographic_first" and current_rule_uid < source_rule_uid:
            return True
        if strategy_name == "force_lexicographic_second" and current_rule_uid > source_rule_uid:
            return True
        if strategy_name == "both_end_with_lexicographic_first" and current_rule_uid < source_rule_uid:
            return True
        if strategy_name == "both_end_with_lexicographic_second" and current_rule_uid > source_rule_uid:
            return True
        return False

    def execute_policy(
        self,
        resolution_rule: Dict[str, Any],
        current_rule_uid: str,
        source_rule_uid: str,
        current_stage: str,
        now_time: str,
    ) -> List[Dict[str, Any]]:
        policy = resolution_rule.get("policy", {}) or {}
        strategy_name = str(policy.get("strategy_name") or "default")
        cmds: List[Dict[str, Any]] = []

        if self.policy_requires_cancel_source(strategy_name, current_rule_uid, source_rule_uid):
            cmds.extend(self.build_cancel_source_commands(source_rule_uid))
            self.deactivate_action_node(a_node(source_rule_uid), now_time)

        allow_current = self.policy_allows_current_execution(strategy_name, current_rule_uid, source_rule_uid)
        if allow_current:
            cmds.append({"type": "default"})
        else:
            cmds.append({"type": "stop"})
            self.clear_rule_trigger_condition_state(current_rule_uid, now_time)
        return cmds

    # ------------------------------------------------------------------
    # Check handlers
    # ------------------------------------------------------------------
    def handle_before_condition(self, rule_uid: str, session: HASession, now_time: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        trigger_node_id = t_node(rule_uid)
        cond_node_id = c_node(rule_uid)
        self.store.set_current(trigger_node_id, state=1, time=now_time)

        matches: List[Tuple[LocalPath, Dict[str, Any]]] = []

        trigger_paths = self.build_local_paths_for_node(trigger_node_id, {"direct-trigger", "env-trigger"})
        for path in trigger_paths:
            self.lazy_refresh_action_node(path.source_action_node, session, now_time)
            if path.env_node:
                self.lazy_refresh_environment_node(path.env_node, session, now_time)
            if self.match_trigger_association(path, rule_uid, now_time):
                rr = self.find_resolution_rule_for_path(path)
                if rr:
                    matches.append((path, rr))

        if cond_node_id in self.node_map:
            prev_cond_state = self.store.get(cond_node_id).get("state")
            cond_ok, cond_list = self.evaluate_conditions(rule_uid, session, now_time)
            current_cond_state = 1 if cond_ok else 0
            self.store.set_current(cond_node_id, state=current_cond_state, time=now_time, conditions=cond_list)

            disable_paths = self.build_local_paths_for_node(cond_node_id, {"direct-condition-disable", "indirect-condition-disable"})
            for path in disable_paths:
                self.lazy_refresh_action_node(path.source_action_node, session, now_time)
                if path.env_node:
                    self.lazy_refresh_environment_node(path.env_node, session, now_time)
                if self.match_condition_disable_association(path, prev_cond_state, current_cond_state):
                    rr = self.find_resolution_rule_for_path(path)
                    if rr:
                        matches.append((path, rr))

        selected = self.choose_best_match(matches)
        if selected:
            path, rr = selected
            cmds = self.execute_policy(rr, rule_uid, rule_uid_from_node(path.source_action_node) or "", "before_condition", now_time)
            report = {
                "detected": True,
                "stage": "before_condition",
                "rule_uid": rule_uid,
                "candidate_key": rr.get("candidate_key"),
                "source_rule_uid": rr.get("source_rule_uid"),
                "target_rule_uid": rr.get("target_rule_uid"),
                "association_kind": rr.get("association_kind"),
                "commands": cmds,
            }
            return cmds, report
        return [{"type": "default"}], {"detected": False, "stage": "before_condition", "rule_uid": rule_uid}

    def handle_after_condition(self, rule_uid: str, session: HASession, now_time: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        cond_node_id = c_node(rule_uid)
        action_node_id = a_node(rule_uid)
        matches: List[Tuple[LocalPath, Dict[str, Any]]] = []

        prev_cond_state = self.store.get(cond_node_id).get("state") if cond_node_id in self.node_map else None
        cond_ok, cond_list = self.evaluate_conditions(rule_uid, session, now_time)
        if cond_node_id in self.node_map:
            # after_condition means HA has already accepted the condition.
            self.store.set_current(cond_node_id, state=1, time=now_time, conditions=cond_list)

            allow_paths = self.build_local_paths_for_node(cond_node_id, {"direct-condition-allow", "indirect-condition-allow"})
            for path in allow_paths:
                self.lazy_refresh_action_node(path.source_action_node, session, now_time)
                if path.env_node:
                    self.lazy_refresh_environment_node(path.env_node, session, now_time)
                if self.match_condition_allow_association(path, prev_cond_state, 1):
                    rr = self.find_resolution_rule_for_path(path)
                    if rr:
                        matches.append((path, rr))

        action_paths = self.build_local_paths_for_node(action_node_id, {"direct-action", "indirect-action"})
        for path in action_paths:
            self.lazy_refresh_action_node(path.source_action_node, session, now_time)
            if path.env_node:
                self.lazy_refresh_environment_node(path.env_node, session, now_time)
            if self.match_action_association(path, now_time):
                rr = self.find_resolution_rule_for_path(path)
                if rr:
                    matches.append((path, rr))

        selected = self.choose_best_match(matches)
        if selected:
            path, rr = selected
            source_rule_uid = rule_uid_from_node(path.source_action_node) or ""
            cmds = self.execute_policy(rr, rule_uid, source_rule_uid, "after_condition", now_time)
            if any(cmd.get("type") == "default" for cmd in cmds):
                self.activate_current_action_node(rule_uid, session, now_time)
            report = {
                "detected": True,
                "stage": "after_condition",
                "rule_uid": rule_uid,
                "candidate_key": rr.get("candidate_key"),
                "source_rule_uid": rr.get("source_rule_uid"),
                "target_rule_uid": rr.get("target_rule_uid"),
                "association_kind": rr.get("association_kind"),
                "commands": cmds,
            }
            return cmds, report

        # No unexpected local association detected: allow execution and tentatively
        # activate the current action node. Lazy refresh will later remove it if
        # the real device state no longer supports it.
        self.activate_current_action_node(rule_uid, session, now_time)
        return [{"type": "default"}], {"detected": False, "stage": "after_condition", "rule_uid": rule_uid}

    # ------------------------------------------------------------------
    # Entry point for one runtime event
    # ------------------------------------------------------------------
    def handle_check_event(self, event: Dict[str, Any], session: HASession) -> List[Dict[str, Any]]:
        rule_uid = str(event.get("entity_id") or "")
        location = str(event.get("location") or "")
        now_time = session.time_now()

        if rule_uid not in self.rule_map:
            report = {
                "time": now_time,
                "event": event,
                "detected": False,
                "reason": "rule_uid not found in TCAE",
            }
            self.append_event_log(report)
            self.persist_store()
            return [{"type": "default"}]

        if location == "before_condition":
            cmds, report = self.handle_before_condition(rule_uid, session, now_time)
        elif location == "after_condition":
            cmds, report = self.handle_after_condition(rule_uid, session, now_time)
        else:
            cmds, report = [{"type": "default"}], {"detected": False, "stage": location, "rule_uid": rule_uid, "reason": "unknown check stage"}

        self.persist_store()
        self.append_event_log({"time": now_time, "event": event, **report})
        return cmds


# ---------------------------------------------------------------------------
# Runtime server
# ---------------------------------------------------------------------------


class RuntimeServer:
    def __init__(self, walker: RuntimeGraphWalker, host: str, port: int) -> None:
        self.walker = walker
        self.host = host
        self.port = int(port)
        self._sock: Optional[socket.socket] = None

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen()
        self._sock = sock
        print(f"HomeAgent Runtime Server listening on {self.host}:{self.port}")
        while True:
            conn, addr = sock.accept()
            t = threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True)
            t.start()

    def _handle_conn(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        with conn:
            try:
                raw = conn.recv(BUFFER_SIZE)
                if not raw:
                    return
                event = json.loads(raw.decode("utf-8"))
                print(f"[RuntimeCheck] from={addr} event={event}")
                session = HASession(conn)
                cmds = self.walker.handle_check_event(event, session)
                session.send_commands(cmds)
                session.finish()
            except Exception as exc:
                print(f"[RuntimeCheck][ERROR] {exc}")
                try:
                    session = HASession(conn)
                    session.send_commands([{"type": "default"}])
                    session.finish()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Run HomeAgent runtime graph-walk checker server.")
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT, help="Runtime history queue length per node. Default: 100")
    parser.add_argument("--trigger-delta", type=float, default=DEFAULT_TRIGGER_DELTA_SEC, help="Max seconds for direct trigger causality. Default: 1.0")
    parser.add_argument("--condition-delta", type=float, default=DEFAULT_CONDITION_DELTA_SEC, help="Reserved max seconds for condition-causality checks. Default: 1.0")
    parser.add_argument("--action-delta", type=float, default=DEFAULT_ACTION_DELTA_SEC, help="Soft seconds bound for action-association checks. Default: 1.0")
    args = parser.parse_args()

    load_env()
    config = load_config()

    host = get_env_value("HomeAgent_IP", default="127.0.0.1") or "127.0.0.1"
    port_text = get_env_value("HomeAgent_PORT", default="8081") or "8081"
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"Invalid HomeAgent_PORT in environment: {port_text}")

    walker = RuntimeGraphWalker(
        config=config,
        history_limit=args.history_limit,
        trigger_delta_sec=args.trigger_delta,
        condition_delta_sec=args.condition_delta,
        action_delta_sec=args.action_delta,
    )
    server = RuntimeServer(walker, host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
