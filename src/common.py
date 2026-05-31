"""
Common utilities for HomeAgent.

This module intentionally does not hard-code machine-specific variables.
It loads project paths from configurations/config.json and optional runtime
variables from .env. Secrets are never printed by these helpers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required. Please install it with: pip install pyyaml") from exc


SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
DEFAULT_CONFIG_PATH = SRC_DIR / "configurations" / "config.json"
DEFAULT_ENV_PATH = SRC_DIR / ".env"


class HomeAgentError(RuntimeError):
    """Base exception for HomeAgent utilities."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Union[str, Path]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_json(path: Union[str, Path], default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(str(p))
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Union[str, Path], data: Any) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_yaml(path: Union[str, Path], default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(str(p))
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default


def write_yaml(path: Union[str, Path], data: Any) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_config(config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    return load_json(config_path)


def save_config(config: Dict[str, Any], config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> None:
    write_json(config_path, config)


def resolve_src_path(path_value: Union[str, Path]) -> Path:
    """Resolve a path relative to src/ unless it is absolute."""
    p = Path(path_value)
    if p.is_absolute():
        return p
    return (SRC_DIR / p).resolve()


def get_home_dir(config: Dict[str, Any]) -> Path:
    return ensure_dir(resolve_src_path(config["system_path"]["home_path"]))


def get_image_dir(config: Dict[str, Any]) -> Path:
    return ensure_dir(resolve_src_path(config["system_path"]["image_path"]))


def get_input_path(config: Dict[str, Any], key: str) -> Path:
    return resolve_src_path(config["input_files"][key])


def get_optional_input_path(config: Dict[str, Any], key: str, default_relative: Optional[str] = None) -> Optional[Path]:
    """Resolve an optional input path from config.

    The project may be used with different Home Assistant exports. Therefore
    optional files, such as ``core.entity_registry``, should not be hard-coded
    as mandatory. If the key is absent and ``default_relative`` is provided, the
    default path is resolved relative to ``src/`` and returned only when it
    exists.
    """
    value = (config.get("input_files") or {}).get(key)
    if value:
        return resolve_src_path(value)
    if default_relative:
        p = resolve_src_path(default_relative)
        return p if p.exists() else None
    return None


def get_output_path(config: Dict[str, Any], key: str) -> Path:
    return get_home_dir(config) / config["output_files"][key]


def parse_env_file(env_path: Union[str, Path] = DEFAULT_ENV_PATH) -> Dict[str, str]:
    """Parse .env without third-party dependencies.

    Supports both KEY=value and KEY = value. Quoted values are unquoted.
    Existing process environment variables are not overwritten by this function;
    it only returns a dictionary.
    """
    p = Path(env_path)
    env: Dict[str, str] = {}
    if not p.exists():
        return env
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            env[key] = value
    return env


def load_env(env_path: Union[str, Path] = DEFAULT_ENV_PATH, override: bool = False) -> Dict[str, str]:
    env = parse_env_file(env_path)
    for key, value in env.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return env


def get_env_value(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def listify(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def unique_list(values: Iterable[Any]) -> List[Any]:
    seen = set()
    out = []
    for v in values:
        key = json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else str(v)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def slugify_rule_alias(alias: str, fallback: str = "rule") -> str:
    """Create a stable ASCII-ish HomeAgent rule identifier suffix.

    This is only an internal identifier. It does not assume semantic words in the
    alias and does not classify devices by name.
    """
    alias = str(alias or "").strip().lower()
    alias = re.sub(r"[^a-z0-9_\-\s]+", "_", alias)
    alias = re.sub(r"[\s\-]+", "_", alias)
    alias = re.sub(r"_+", "_", alias).strip("_")
    return alias or fallback


def make_rule_uid(automation: Dict[str, Any], index: int) -> str:
    alias = automation.get("alias") or automation.get("id") or f"rule_{index}"
    suffix = slugify_rule_alias(str(alias), fallback=f"rule_{index}")
    return f"automation.{suffix}"


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def object_id_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def looks_like_entity_id(value: Any) -> bool:
    """Conservative Home Assistant entity-id recognizer.

    It only uses the platform domain prefix and the entity-id shape. It does not
    infer semantics from user-defined object names.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or "{{" in v or "}}" in v:
        return False
    # HA entity_ids are domain.object_id. The object_id is normally slugified,
    # but this regex stays permissive enough for custom integrations.
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*\.[^\s,;:\[\]{}()]+$", v))


def normalize_entity_ids(value: Any) -> List[str]:
    ids: List[str] = []
    for item in listify(value):
        if looks_like_entity_id(item):
            ids.append(str(item).strip())
    return unique_list(ids)




def load_entity_registry(path: Optional[Union[str, Path]] = None) -> Dict[str, Dict[str, Any]]:
    """Load Home Assistant core.entity_registry as an entity_id-indexed map.

    The registry is used only for user-facing display names and additional
    metadata. It is not required for semantic classification, and missing files
    simply produce an empty mapping.
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = load_json(p)
    except Exception:
        return {}
    entities = ((data or {}).get("data") or {}).get("entities") or []
    out: Dict[str, Dict[str, Any]] = {}
    for item in entities:
        if isinstance(item, dict) and item.get("entity_id"):
            out[str(item["entity_id"])] = item
    return out


def entity_display_name(entity_id: str, registry_map: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Return a human-friendly entity name for display.

    Priority: user-customized name -> original_name -> entity_id. The returned
    text includes entity_id as a stable reference when a natural name exists.
    """
    registry_map = registry_map or {}
    rec = registry_map.get(entity_id) or {}
    name = rec.get("name") or rec.get("original_name")
    if name and str(name).strip():
        return f"{name} ({entity_id})"
    return entity_id


def enrich_entity_with_registry(entity: Dict[str, Any], registry_map: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Attach display metadata from core.entity_registry to an entity record."""
    registry_map = registry_map or {}
    eid = entity.get("entity_id")
    rec = registry_map.get(eid) or {}
    entity["display_name"] = entity_display_name(str(eid), registry_map) if eid else ""
    entity["registry"] = {
        "name": rec.get("name"),
        "original_name": rec.get("original_name"),
        "aliases": rec.get("aliases") or [],
        "area_id": rec.get("area_id"),
        "device_class": rec.get("device_class"),
        "original_device_class": rec.get("original_device_class"),
        "platform": rec.get("platform"),
        "unit_of_measurement": rec.get("unit_of_measurement"),
    }
    return entity

def infer_value_type_from_domain(domain: str) -> str:
    """Infer only from HA domain, never from user-defined object name."""
    numeric_domains = {"input_number", "number", "counter"}
    boolean_domains = {"input_boolean", "switch", "light", "binary_sensor", "lock", "cover", "fan", "climate"}
    event_domains = {"input_button", "button", "event"}
    datetime_domains = {"input_datetime", "datetime", "time", "date"}
    select_domains = {"input_select", "select"}
    if domain in numeric_domains:
        return "numeric"
    if domain in boolean_domains:
        return "state"
    if domain in event_domains:
        return "event"
    if domain in datetime_domains:
        return "datetime"
    if domain in select_domains:
        return "select"
    return "unknown"


def service_operation(service: Optional[str]) -> str:
    if not service:
        return "unknown"
    return str(service).split(".")[-1]


def service_domain(service: Optional[str]) -> str:
    if not service or "." not in str(service):
        return ""
    return str(service).split(".", 1)[0]


def post_value_from_service(service: Optional[str], data: Optional[Dict[str, Any]] = None) -> Any:
    """Map common Home Assistant service operations to abstract post states.

    This is operation-based, not object-name-based. Unknown operations are kept
    as structured values so later modules can still reason conservatively.
    """
    data = data or {}
    op = service_operation(service)
    if op in {"turn_on", "enable", "open", "open_cover", "unlock", "start"}:
        return "on"
    if op in {"turn_off", "disable", "close", "close_cover", "lock", "stop"}:
        return "off"
    if op == "toggle":
        return "toggle"
    if op in {"set_value", "set_temperature", "set_humidity", "set_position"}:
        for key in ("value", "temperature", "humidity", "position"):
            if key in data:
                return data[key]
    if op in {"select_option", "select_next", "select_previous"}:
        if "option" in data:
            return data["option"]
    return {"operation": op, "data": data}


def relation_from_numeric_node(node: Dict[str, Any], trigger: bool) -> List[Tuple[str, Any]]:
    """Return environment relations from a Home Assistant numeric_state node.

    For trigger=True, above/below are modeled as threshold-crossing relations
    upward/downward. For conditions, they are modeled as >/< predicates.
    """
    out: List[Tuple[str, Any]] = []
    if "above" in node:
        out.append(("↑" if trigger else ">", node.get("above")))
    if "below" in node:
        out.append(("↓" if trigger else "<", node.get("below")))
    return out


def extract_json_from_text(text: str) -> Any:
    """Extract JSON from an AI response.

    Accepts raw JSON, fenced JSON, or text containing a JSON object/array.
    """
    if text is None:
        raise ValueError("empty AI response")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.S | re.I)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    starts = [(s.find("{"), "{"), (s.find("["), "[")]
    starts = [(idx, ch) for idx, ch in starts if idx >= 0]
    if not starts:
        raise ValueError("No JSON object or array found in AI response")
    start, ch = min(starts, key=lambda x: x[0])
    end_ch = "}" if ch == "{" else "]"
    end = s.rfind(end_ch)
    if end < start:
        raise ValueError("Malformed JSON in AI response")
    return json.loads(s[start : end + 1])


def build_ai_messages(system_prompt: str, user_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def call_ai_chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.0,
    timeout: int = 120,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """Call an OpenAI-compatible chat/completions endpoint.

    Environment variable aliases are supported to avoid hard-coding names.
    The implementation prefers ``requests`` because some API gateways/CDN
    policies reject Python urllib traffic and return errors such as 403/1010.
    If requests is unavailable, it falls back to urllib.
    """
    load_env()
    model = (model or get_env_value("AI_MODEL", default="") or "").strip()
    api_url = (api_url or get_env_value("AI_API_URL", default="") or "").strip().rstrip("/")
    api_key = (api_key or get_env_value("AI_API_KEY", default="") or "").strip()
    if not model:
        raise HomeAgentError("AI_MODEL is missing in environment variables.")
    if not api_url:
        raise HomeAgentError("AI_API_URL is missing in environment variables.")
    if not api_key:
        raise HomeAgentError("AI_API_KEY is missing in environment variables.")

    endpoint = api_url if api_url.endswith("/chat/completions") else f"{api_url}/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    # Not every OpenAI-compatible endpoint supports response_format. When the
    # caller does not specify it, rely on the prompt to request strict JSON.
    if response_format is not None:
        payload["response_format"] = response_format

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        import requests  # type: ignore

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:  # type: ignore[attr-defined]
            raise HomeAgentError(f"AI connection error: {exc}") from exc

        if response.status_code != 200:
            detail = response.text
            raise HomeAgentError(f"AI HTTP error {response.status_code}: {detail[:1000]}")
        data = response.json()
    except ImportError:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HomeAgentError(f"AI HTTP error {exc.code}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise HomeAgentError(f"AI connection error: {exc}") from exc
        data = json.loads(body)

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HomeAgentError(f"Unexpected AI response format: {json.dumps(data, ensure_ascii=False)[:1000]}") from exc


def call_ai_json(system_prompt: str, user_payload: Dict[str, Any], **kwargs: Any) -> Any:
    messages = build_ai_messages(system_prompt, user_payload)
    text = call_ai_chat(messages, **kwargs)
    return extract_json_from_text(text)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"{question} {suffix} ").strip().lower()
    if not ans:
        return default
    return ans in {"y", "yes", "是", "确认", "1", "true"}


def prompt_int(question: str, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    while True:
        raw = input(question).strip()
        try:
            value = int(raw)
        except ValueError:
            print("请输入整数。")
            continue
        if min_value is not None and value < min_value:
            print(f"请输入不小于 {min_value} 的整数。")
            continue
        if max_value is not None and value > max_value:
            print(f"请输入不大于 {max_value} 的整数。")
            continue
        return value


def prompt_index_list(question: str, valid_indices: List[int], default: Optional[List[int]] = None) -> List[int]:
    valid = set(valid_indices)
    default = default or []
    default_text = ",".join(str(i) for i in default)
    while True:
        raw = input(f"{question}" + (f" [默认: {default_text}] " if default_text else " ")).strip()
        if not raw and default is not None:
            return default
        parts = [p.strip() for p in re.split(r"[,，\s]+", raw) if p.strip()]
        try:
            values = [int(p) for p in parts]
        except ValueError:
            print("请输入数字编号，可用逗号或空格分隔。")
            continue
        if not values:
            print("至少输入一个编号。")
            continue
        if any(v not in valid for v in values):
            print(f"编号超出范围，可选编号: {sorted(valid)}")
            continue
        return unique_list(values)
