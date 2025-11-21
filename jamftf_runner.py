"""
env vars:
  export JAMF_URL="https://<tenant>.jamfcloud.com"
  export JAMF_CLIENT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  export JAMF_CLIENT_SECRET="********************************"

usage:
  python jamftf_runner.py --config jamftf.config.json --out imports.hcl [--dump item_dump.jsonl]
"""

import os
import sys
import argparse
import re
import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Iterable

# ---------- jamfpy import + compatibility patch ----------
def import_and_patch_jamfpy():
    try:
        import jamfpy  # type: ignore
    except Exception as e:
        sys.exit(
            "Failed to import jamfpy. Install with:\n"
            '  pip install "git+https://github.com/thejoeker12/jamfpy"\n'
            f"Error: {e}"
        )

    # jamftf cals jamfpy.get_logger; some jamfpy builds only have new_logger
    if not hasattr(jamfpy, "get_logger") and hasattr(jamfpy, "new_logger"):
        def _compat_get_logger(name, level=20):
            try:
                return jamfpy.new_logger(name=name, level=level)
            except TypeError:
                return jamfpy.new_logger(name, level)
        jamfpy.get_logger = _compat_get_logger  # type: igore[attr-defined]

    try:
        from jamfpy.client import tenant as jt  # type: ignore
    except Exception as e:
        sys.exit(f"Could not import jamfpy.client.tenant: {e}")

    return jamfpy, jt

jamfpy, jt = import_and_patch_jamfpy()

# ---------- jamftf immport ----------
def import_jamftf():
    try:
        from jamftf.config_ingest import parse_config_file
        return parse_config_file
    except Exception as e:
        sys.exit(
            "Failed to import jamftf modules. "
            "Make sure you're in the project root and ran:\n"
            "    pip install -e .\n"
            f"Error: {e}"
        )

# ---------- helpers ----------
def env_values():
    url  = os.getenv("JAMF_URL")
    cid  = os.getenv("JAMF_CLIENT_ID")
    csec = os.getenv("JAMF_CLIENT_SECRET")
    if not (url and cid and csec):
        sys.exit("Set JAMF_URL, JAMF_CLIENT_ID, JAMF_CLIENT_SECRET environment variables.")
    p = urlparse(url)
    host = (p.netloc or url.replace("https://","").replace("http://","")).strip("/")
    return (host, f"https://{host}"), cid, csec

def build_client():
    fqdn_candidates, cid, csec = env_values()

    http_cfg = jt.HTTPConfig()
    try:
        http_cfg.scheme = "https"
        http_cfg.port = 443
    except Exception:
        pass

    tried = []
    for fq in fqdn_candidates:
        try:
            t = jt.Tenant(
                fqdn=fq,
                auth_method="oauth2",
                client_id=cid,
                client_secret=csec,
                http_config=http_cfg,
                token_exp_threshold_mins=0,
                safe_mode=False,
            )
            print(f"[runner] Tenant OK with fqdn='{fq}', auth_method='oauth2'")
            # otional explicit token call harmless if not needed
            for m in ("authenticate", "get_token", "login", "generate_token"):
                fn = getattr(t, m, None)
                if callable(fn):
                    try:
                        fn()
                        print(f"[runner] {m} OK")
                        break
                    except Exception as e:
                        print(f"[runner] {m} failed: {e}")
            return t
        except Exception as e:
            tried.append((fq, str(e)))

    msg = "Could not instantiate Tenant with any fqdn form. Tried:\n"
    for fq, err in tried:
        msg += f"  - fqdn='{fq}': {err}\n"
    msg += f"VALID_AUTH_METHODS: {getattr(jt, 'VALID_AUTH_METHODS', None)}\n"
    sys.exit(msg)

def sanitize_name(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = s.strip("_")
    if not s or s[0].isdigit():
        s = f"r_{s}" if s else "r"
    return s

def _json_try_parse(s: Any):
    if isinstance(s, (str, bytes)):
        try:
            return json.loads(s)
        except Exception:
            return None
    return None

def obj_to_dict_deep(obj: Any, max_depth: int = 3) -> dict | None:
    """
    Convert jamfpy SingleItem-like wrappers into dicts by probing lots of shapes, recursively.
    """
    seen = set()

    def _walk(o, depth):
        if o is None:
            return None
        oid = id(o)
        if oid in seen or depth < 0:
            return None
        seen.add(oid)

        # immediate dict
        if isinstance(o, dict):
            return o

        # mapping-like
        if hasattr(o, "keys") and hasattr(o, "get"):
            try:
                return {k: o.get(k) for k in o.keys()}  # type: ignore[attr-defined]
            except Exception:
                pass

        # explicit known containers
        for attr in ("data", "_data", "payload", "_payload", "raw", "_raw", "value", "_value", "item", "obj", "object", "response", "record", "entity", "document", "body"):
            v = getattr(o, attr, None)
            if isinstance(v, dict):
                return v
            if v is not None:
                d = _walk(v, depth - 1)
                if isinstance(d, dict):
                    return d

        # common conversion methods
        for meth in ("to_dict", "dict", "model_dump"):
            fn = getattr(o, meth, None)
            if callable(fn):
                try:
                    v = fn()
                    if isinstance(v, dict):
                        return v
                    d = _walk(v, depth - 1)
                    if isinstance(d, dict):
                        return d
                except Exception:
                    pass

        # json() often returns a json string
        fn = getattr(o, "json", None)
        if callable(fn):
            try:
                s = fn()
                d = _json_try_parse(s)
                if isinstance(d, dict):
                    return d
            except Exception:
                pass

        # try vars()
        try:
            v = vars(o)
            if isinstance(v, dict) and v:
                # unwrap one level if possible
                # sometimes the real payload sits under a single key inside __dict__
                if len(v) == 1 and isinstance(next(iter(v.values())), (dict, object)):
                    d = _walk(next(iter(v.values())), depth - 1)
                    if isinstance(d, dict):
                        return d
                return v
        except Exception:
            pass

        return None

    return _walk(obj, max_depth)

def extract_id_any(item: Any) -> str | None:
    d = obj_to_dict_deep(item, max_depth=3)
    # common ID fields (now includes 'jpro_id')
    if isinstance(d, dict):
        for k in ("jpro_id", "id", "Id", "ID", "profileId", "profileID", "uuid", "uid"):
            if k in d and d[k] is not None and d[k] != "":
                return str(d[k])
        # common nested areas
        for k in ("general", "profile", "configurationProfile", "payloadContent", "data"):
            sub = d.get(k)
            if isinstance(sub, dict):
                for ik in ("jpro_id", "id", "uuid", "profileId"):
                    if sub.get(ik):
                        return str(sub[ik])

    # tttribute fallbacks
    for k in ("jpro_id", "id", "Id", "ID", "profileId", "profileID", "uuid", "uid"):
        v = getattr(item, k, None)
        if v:
            return str(v)
    # mapping style get
    get = getattr(item, "get", None)
    if callable(get):
        for k in ("jpro_id", "id", "uuid", "profileId"):
            try:
                v = get(k)
                if v:
                    return str(v)
            except Exception:
                pass
    # Indexing
    try:
        v = item["jpro_id"]  # typee: ignore[index]
        if v:
            return str(v)
    except Exception:
        pass
    try:
        v = item["id"]  # type: ignore[index]
        if v:
            return str(v)
    except Exception:
        pass

    return None
    d = obj_to_dict_deep(item, max_depth=3)
    # common ID fields
    if isinstance(d, dict):
        for k in ("id", "Id", "ID", "profileId", "profileID", "uuid", "uid"):
            if k in d and d[k]:
                return str(d[k])
        # common nested areas
        for k in ("general", "profile", "configurationProfile", "payloadContent", "data"):
            sub = d.get(k)
            if isinstance(sub, dict):
                for ik in ("id", "uuid", "profileId"):
                    if sub.get(ik):
                        return str(sub[ik])

    # attribute fallbacks
    for k in ("id", "Id", "ID", "profileId", "profileID", "uuid", "uid"):
        v = getattr(item, k, None)
        if v:
            return str(v)
    # mapping-style get
    get = getattr(item, "get", None)
    if callable(get):
        for k in ("id", "uuid", "profileId"):
            try:
                v = get(k)
                if v:
                    return str(v)
            except Exception:
                pass
    # Indexing
    try:
        v = item["id"]  # type:: ignore[index]
        if v:
            return str(v)
    except Exception:
        pass

    return None

def extract_name_any(item: Any, fallback_id: str) -> str:
    d = obj_to_dict_deep(item, max_depth=3)
    if isinstance(d, dict):
        for k in ("name", "displayName", "profileName", "computer_group_name", "generalName"):
            if d.get(k):
                return sanitize_name(d[k])
        for k in ("general", "profile", "configurationProfile", "payloadContent", "data"):
            sub = d.get(k)
            if isinstance(sub, dict) and sub.get("name"):
                return sanitize_name(sub["name"])
    # attribute fallback
    for k in ("name", "displayName", "profileName"):
        v = getattr(item, k, None)
        if v:
            return sanitize_name(v)
    return sanitize_name(f"id_{fallback_id}")

# map jamftf resource tag to terraform resource type
TERRAFORM_TYPE_MAP = {
    "MACOS_CONFIG_PROFILE": "jamfpro_macos_configuration_profile_plist",
    "SCRIPT": "jamfpro_script",
    "CATEGORY": "jamfpro_category",
    "POLICY": "jamfpro_policy",
    "STATIC_COMPUTER_GROUP": "jamfpro_static_computer_group",
    "SMART_COMPUTER_GROUP": "jamfpro_smart_computer_group",
    "ADVANCED_COMPUTER_SEARCH": "jamfpro_advanced_computer_search",
    "COMPUTER_EXTENSION_ATTRIBUTE": "jamfpro_computer_extension_attribute",
}

def iter_items_from_resource(res) -> list[Any]:
    """
    Pull the fetched dataset from a jamftf resource instance.
    Accepts lists, iterables, pagers, etc.
    """
    cand_attrs = ("data", "_data", "dataset", "items", "all")
    for attr in cand_attrs:
        obj = getattr(res, attr, None)
        # method returning list/iterable
        if callable(obj):
            try:
                val = obj()
                if isinstance(val, list):
                    return val
                if isinstance(val, Iterable):
                    return list(val)
            except Exception:
                pass
        # direct list/iterable
        if isinstance(obj, list):
            return obj
        if obj is not None and hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, dict)):
            try:
                return list(obj)
            except Exception:
                pass

    # try common getter methods
    for meth in ("get_all", "to_list", "list", "all"):
        fn = getattr(res, meth, None)
        if callable(fn):
            try:
                val = fn()
                if isinstance(val, list):
                    return val
                if isinstance(val, Iterable):
                    return list(val)
            except Exception:
                pass

    return []

def detect_tag(res) -> str:
    tag = getattr(res, "resource_type", None)
    if tag is None:
        tag = getattr(res, "provider_tag", None)
    tag_str = str(tag)
    m = re.search(r"(MACOS_CONFIG_PROFILE|SCRIPT|CATEGORY|POLICY|STATIC_COMPUTER_GROUP|SMART_COMPUTER_GROUP|ADVANCED_COMPUTER_SEARCH|COMPUTER_EXTENSION_ATTRIBUTE)", tag_str)
    return m.group(1) if m else tag_str

def compose_import_hcl(resources, dump_path: str | None = None) -> str:
    blocks = []
    dumper = None
    dumped = 0
    dump_cap = 20  # dump a few more now that we're close
    if dump_path:
        dumper = open(dump_path, "w", encoding="utf-8")

    for res in resources:
        tag = detect_tag(res)
        tf_type = None
        for key, val in TERRAFORM_TYPE_MAP.items():
            if key in tag:
                tf_type = val
                break
        if not tf_type:
            blocks.append(f"# Skipping unsupported resource tag: {tag}")
            continue

        items = iter_items_from_resource(res)
        if not items:
            blocks.append(f"# No items found for {tag}")
            continue

        for item in items:
            if dumper and dumped < dump_cap:
                try:
                    d = obj_to_dict_deep(item, max_depth=3)
                    dumper.write(json.dumps(d if d is not None else {"repr": repr(item)}, ensure_ascii=False) + "\n")
                    dumped += 1
                except Exception:
                    pass

            rid = extract_id_any(item)
            if not rid:
                blocks.append(f"# Could not extract id for item in {tag}: {repr(item)}")
                continue
            local = extract_name_any(item, rid)
            block = f"""import {{
  id = "{rid}"
  to = {tf_type}.{local}
}}"""
            blocks.append(block)

    if dumper:
        dumper.close()
        print(f"[runner] Wrote sample dump to {dump_path}")

    return "\n\n".join(blocks) + ("\n" if blocks else "")

# ---------- main ----------
def main():
    parser = argparse.ArgumentParser(description="Generate Terraform import blocks for Jamf Pro resources.")
    parser.add_argument("--config", required=True, help="Path to jamftf JSON config (e.g., jamftf.config.json).")
    parser.add_argument("--out", default="imports.hcl", help="Output HCL file path (default: imports.hcl).")
    parser.add_argument("--dump", default=None, help="Optional path to write JSONL of a few raw items.")
    args = parser.parse_args()

    parse_config_file = import_jamftf()
    client = build_client()

    resources = parse_config_file(args.config)
    if not resources:
        print("No resources selected in the config; nothing to do.")
        return 0

    # fetch
    for r in resources:
        r.set_client(client)
        r.refresh_data()

    # compose hcl ourselves
    hcl = compose_import_hcl(resources, dump_path=args.dump)

    out = Path(args.out)
    out.write_text(hcl, encoding="utf-8")
    print(f"Wrote {out} with {len(hcl.splitlines())} lines.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

