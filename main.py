import os
import logging
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BASE_URL      = os.getenv("BASE_URL")
CLIENT_ID     = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

REQUIRED_FIELDS = [
    "PolicyName", "PolicyOwner", "ViolationOwner", "Description", "State",
    "Left_CriteriaName", "Left_Application", "Left_Entitlements",
    "Right_CriteriaName", "Right_Application", "Right_Entitlements",
]
VALID_STATES        = {"ENFORCED", "INACTIVE"}
TOKEN_REFRESH_EVERY = 50
REQUEST_TIMEOUT     = 30

RUN_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ── Logging setup ──────────────────────────────────────────────────────────────
# migration.log → full detail: every API call, every resolve, every result
# summary.log   → one block per run: policy-level pass/fail/skip + totals
detail_handler = logging.FileHandler("logs/migration.log", encoding="utf-8")
detail_handler.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
detail_handler.setFormatter(fmt)
console_handler.setFormatter(fmt)

log = logging.getLogger("migration")
log.setLevel(logging.DEBUG)
log.addHandler(detail_handler)
log.addHandler(console_handler)

summary_log = logging.getLogger("summary")
summary_log.setLevel(logging.INFO)
summary_handler = logging.FileHandler("logs/summary.log", encoding="utf-8")
summary_handler.setFormatter(logging.Formatter("%(message)s"))
summary_log.addHandler(summary_handler)
# ──────────────────────────────────────────────────────────────────────────────

_identity_cache    = {}
_gov_group_cache   = {}
_source_cache      = {}
_entitlement_cache = {}


def _is_blank(val):
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    return str(val).strip() == ""


def get_token():
    resp = requests.post(
        f"{BASE_URL}/oauth/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def experimental_headers(token):
    return {**auth_headers(token), "X-SailPoint-Experimental": "true"}


# POST /v3/search — displayName lookup; public-identities does not support name filter
def get_identity(token, name):
    if name in _identity_cache:
        return _identity_cache[name]
    resp = requests.post(
        f"{BASE_URL}/v3/search",
        headers=auth_headers(token),
        json={"indices": ["identities"], "query": {"query": f'displayName:"{name}"'}, "limit": 1},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise ValueError(f"Identity not found: '{name}'")
    ref = {"type": "IDENTITY", "id": items[0]["id"], "name": items[0]["name"]}
    _identity_cache[name] = ref
    return ref


# GET /beta/workgroups — X-SailPoint-Experimental required; name eq filter
def get_governance_group(token, name):
    if name in _gov_group_cache:
        return _gov_group_cache[name]
    resp = requests.get(
        f"{BASE_URL}/beta/workgroups",
        headers=experimental_headers(token),
        params={"filters": f'name eq "{name}"', "limit": 1},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise ValueError(f"Governance group not found: '{name}'")
    ref = {"type": "GOVERNANCE_GROUP", "id": items[0]["id"], "name": items[0]["name"]}
    _gov_group_cache[name] = ref
    return ref


# GET /v3/sources — resolve source name to ID; beta/entitlements only supports source.id
def get_source_id(token, source_name):
    if source_name in _source_cache:
        return _source_cache[source_name]
    resp = requests.get(
        f"{BASE_URL}/v3/sources",
        headers=auth_headers(token),
        params={"filters": f'name eq "{source_name}"', "limit": 1},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise ValueError(f"Source not found: '{source_name}'")
    _source_cache[source_name] = items[0]["id"]
    return items[0]["id"]


# GET /beta/entitlements — falls back to /v3/entitlements if beta returns 404 (tenant-dependent availability)
# X-SailPoint-Experimental required for beta; filter on source.id not source.name
def get_entitlement(token, app_name, entitlement_name):
    cache_key = (app_name, entitlement_name)
    if cache_key in _entitlement_cache:
        return _entitlement_cache[cache_key]
    source_id = get_source_id(token, app_name)
    params = {"filters": f'name eq "{entitlement_name}" and source.id eq "{source_id}"', "limit": 1}

    resp = requests.get(
        f"{BASE_URL}/beta/entitlements",
        headers=experimental_headers(token),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code == 404:
        log.debug(f"  /beta/entitlements returned 404 — falling back to /v3/entitlements for '{entitlement_name}'")
        resp = requests.get(
            f"{BASE_URL}/v3/entitlements",
            headers=auth_headers(token),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    resp.raise_for_status()
    items = resp.json()
    if not items:
        raise ValueError(f"Entitlement not found: '{entitlement_name}' in source '{app_name}'")
    ref = {"type": "ENTITLEMENT", "id": items[0]["id"]}
    _entitlement_cache[cache_key] = ref
    return ref


def resolve_violation_owner(token, name):
    # assignmentRule must always be STATIC — GOVERNANCE_GROUP as assignmentRule is silently ignored by ISC
    try:
        ref = get_governance_group(token, name)
        log.info(f"  Violation owner resolved as GOVERNANCE_GROUP: '{name}' (id={ref['id']})")
        return "STATIC", ref
    except (ValueError, requests.HTTPError) as e:
        log.warning(
            f"  Governance group '{name}' not found ({e}). "
            f"Falling back to IDENTITY — verify this is intentional."
        )
    ref = get_identity(token, name)
    log.info(f"  Violation owner resolved as IDENTITY (fallback): '{ref['name']}' (id={ref['id']})")
    return "STATIC", ref


def validate_row(row):
    errors = []
    for field in REQUIRED_FIELDS:
        if _is_blank(row.get(field)):
            errors.append(f"missing/empty field '{field}'")
    state = str(row.get("State", "")).strip().upper()
    if state and state not in VALID_STATES:
        errors.append(f"invalid State '{row.get('State')}' — must be one of: {', '.join(sorted(VALID_STATES))}")
    return errors


def build_criteria(token, app_name, criteria_name, entitlements_cell):
    names = [e.strip() for e in str(entitlements_cell).split("\n") if e.strip()]
    if not names:
        raise ValueError(f"No entitlements parsed for criteria '{criteria_name}'")
    log.debug(f"  Resolving {len(names)} entitlement(s) for '{criteria_name}': {names}")
    criteria_list = [get_entitlement(token, app_name, name) for name in names]
    return {"name": criteria_name, "criteriaList": criteria_list}


def has_changes(existing, policy_body):
    """Compare only the fields we own. Returns True if anything differs."""
    def entitlement_ids(criteria):
        return sorted(e["id"] for e in criteria.get("criteriaList", []))

    checks = [
        existing.get("description")  != policy_body.get("description"),
        existing.get("state")         != policy_body.get("state"),
        existing.get("ownerRef", {}).get("id") != policy_body.get("ownerRef", {}).get("id"),
        existing.get("violationOwnerAssignmentConfig", {}).get("ownerRef", {}).get("id")
            != policy_body.get("violationOwnerAssignmentConfig", {}).get("ownerRef", {}).get("id"),
        existing.get("conflictingAccessCriteria", {}).get("leftCriteria",  {}).get("name")
            != policy_body.get("conflictingAccessCriteria", {}).get("leftCriteria",  {}).get("name"),
        existing.get("conflictingAccessCriteria", {}).get("rightCriteria", {}).get("name")
            != policy_body.get("conflictingAccessCriteria", {}).get("rightCriteria", {}).get("name"),
        entitlement_ids(existing.get("conflictingAccessCriteria", {}).get("leftCriteria",  {}))
            != entitlement_ids(policy_body.get("conflictingAccessCriteria", {}).get("leftCriteria",  {})),
        entitlement_ids(existing.get("conflictingAccessCriteria", {}).get("rightCriteria", {}))
            != entitlement_ids(policy_body.get("conflictingAccessCriteria", {}).get("rightCriteria", {})),
    ]
    return any(checks)


# GET /v3/sod-policies — check if a policy with this name already exists; returns full policy object or None
def get_existing_policy(token, policy_name):
    resp = requests.get(
        f"{BASE_URL}/v3/sod-policies",
        headers=auth_headers(token),
        params={"filters": f'name eq "{policy_name}"', "limit": 1},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json()
    if not items:
        return None
    # Fetch full policy object so PUT can merge on top without dropping ISC-managed fields
    full = requests.get(
        f"{BASE_URL}/v3/sod-policies/{items[0]['id']}",
        headers=auth_headers(token),
        timeout=REQUEST_TIMEOUT,
    )
    full.raise_for_status()
    return full.json()


def post_policy(token, policy_body):
    return requests.post(
        f"{BASE_URL}/v3/sod-policies",
        headers=auth_headers(token),
        json=policy_body,
        timeout=REQUEST_TIMEOUT,
    )


def put_policy(token, policy_id, policy_body):
    return requests.put(
        f"{BASE_URL}/v3/sod-policies/{policy_id}",
        headers=auth_headers(token),
        json=policy_body,
        timeout=REQUEST_TIMEOUT,
    )


def write_summary(results):
    total   = len(results)
    success = sum(1 for r in results if r["Status"] == "SUCCESS")
    updated = sum(1 for r in results if r["Status"] == "UPDATED")
    skipped = sum(1 for r in results if r["Status"] == "SKIPPED")
    failed  = total - success - updated - skipped

    lines = [
        "",
        "=" * 100,
        f"Run     : {RUN_TS}",
        f"Total   : {total}  |  Created: {success}  |  Updated: {updated}  |  Failed: {failed}  |  Skipped: {skipped}",
        "-" * 100,
        f"{'Policy Name':<45} {'Status':<10} {'Policy ID':<38} Error",
        "-" * 100,
    ]
    for r in results:
        lines.append(f"{r['PolicyName']:<45} {r['Status']:<10} {r['PolicyId']:<38} {r['Error']}")
    lines.append("=" * 100)

    for line in lines:
        summary_log.info(line)


def migrate_policies():
    # ── Startup checks ─────────────────────────────────────────────────────────
    missing_env = [v for v in ("BASE_URL", "CLIENT_ID", "CLIENT_SECRET") if not os.getenv(v)]
    if missing_env:
        log.critical(f"Missing required environment variables: {', '.join(missing_env)}. Aborting.")
        return

    input_path = "input/sod_policies.csv"
    if not os.path.exists(input_path):
        log.critical(f"Input file not found: '{input_path}'. Aborting.")
        return

    try:
        df = pd.read_csv(input_path)
    except Exception as e:
        log.critical(f"Failed to read input CSV: {e}. Aborting.")
        return

    if df.empty:
        log.warning("Input CSV is empty — nothing to process.")
        return

    missing_cols = [c for c in REQUIRED_FIELDS if c not in df.columns]
    if missing_cols:
        log.critical(f"Input CSV missing required columns: {', '.join(missing_cols)}. Aborting.")
        return

    log.info(f"Loaded {len(df)} polic{'y' if len(df) == 1 else 'ies'} from '{input_path}'.")

    # ── Initial authentication ─────────────────────────────────────────────────
    log.info("Authenticating with ISC...")
    try:
        token = get_token()
    except Exception as e:
        log.critical(f"Authentication failed: {e}. Aborting.")
        return
    log.info("Authentication successful.")

    results                = []
    policies_since_refresh = 0

    for idx, row in df.iterrows():
        row_num     = idx + 2  # 1-based + header row
        policy_name = str(row.get("PolicyName", f"<Row {row_num}>")).strip()

        log.info(f"--- [{idx + 1}/{len(df)}] Processing policy: '{policy_name}' ---")
        record = {"PolicyName": policy_name, "Status": None, "PolicyId": "", "Error": ""}

        # ── Token refresh every N policies ─────────────────────────────────────
        if policies_since_refresh >= TOKEN_REFRESH_EVERY:
            log.info("Refreshing access token (interval reached)...")
            try:
                token = get_token()
                policies_since_refresh = 0
                log.info("Token refreshed successfully.")
            except Exception as e:
                log.error(f"Token refresh failed: {e} — continuing with existing token.")

        # ── Row validation ─────────────────────────────────────────────────────
        validation_errors = validate_row(row)
        if validation_errors:
            msg = "; ".join(validation_errors)
            log.warning(f"  SKIPPED (row {row_num}) — {msg}")
            record.update({"Status": "SKIPPED", "Error": msg})
            results.append(record)
            policies_since_refresh += 1
            continue

        try:
            # Policy owner
            owner_ref = get_identity(token, row["PolicyOwner"].strip())
            log.info(f"  Policy owner: '{owner_ref['name']}' (id={owner_ref['id']})")

            # Violation owner — governance group with identity fallback
            assignment_rule, violation_ref = resolve_violation_owner(token, row["ViolationOwner"].strip())

            # Criteria + entitlements
            left_criteria  = build_criteria(token, row["Left_Application"].strip(),  row["Left_CriteriaName"].strip(),  row["Left_Entitlements"])
            right_criteria = build_criteria(token, row["Right_Application"].strip(), row["Right_CriteriaName"].strip(), row["Right_Entitlements"])
            log.info(f"  Left  criteria: '{left_criteria['name']}' — {len(left_criteria['criteriaList'])} entitlement(s)")
            log.info(f"  Right criteria: '{right_criteria['name']}' — {len(right_criteria['criteriaList'])} entitlement(s)")

            policy_body = {
                "name":        policy_name,
                "description": str(row["Description"]).strip(),
                "state":       str(row["State"]).strip().upper(),
                "type":        "CONFLICTING_ACCESS_BASED",
                "ownerRef":    owner_ref,
                "violationOwnerAssignmentConfig": {
                    "assignmentRule": assignment_rule,
                    "ownerRef":       violation_ref,
                },
                "conflictingAccessCriteria": {
                    "leftCriteria":  left_criteria,
                    "rightCriteria": right_criteria,
                },
            }

            # ── Existence check — create or update ─────────────────────────────
            existing = get_existing_policy(token, policy_name)

            if existing:
                existing_id = existing["id"]
                if not has_changes(existing, policy_body):
                    log.info(f"  SKIPPED — no changes detected (id={existing_id})")
                    record.update({"Status": "SKIPPED", "PolicyId": existing_id})
                else:
                    log.info(f"  Policy already exists (id={existing_id}) — changes detected, updating via PUT.")
                    # Merge our changes on top of the full stored object so ISC-managed fields are preserved
                    merged_body = {**existing, **policy_body, "id": existing_id}
                    resp = put_policy(token, existing_id, merged_body)

                    if resp.status_code == 401:
                        log.warning("  401 Unauthorized — refreshing token and retrying PUT...")
                        token = get_token()
                        policies_since_refresh = 0
                        resp = put_policy(token, existing_id, merged_body)

                    if resp.status_code in (200, 201):
                        log.info(f"  UPDATED — policy updated (id={existing_id})")
                        record.update({"Status": "UPDATED", "PolicyId": existing_id})
                    else:
                        log.error(f"  FAILED — PUT HTTP {resp.status_code}: {resp.text}")
                        record.update({"Status": "FAILED", "Error": f"PUT HTTP {resp.status_code}: {resp.text}"})
            else:
                log.info(f"  Policy does not exist — creating via POST.")
                resp = post_policy(token, policy_body)

                if resp.status_code == 401:
                    log.warning("  401 Unauthorized — refreshing token and retrying POST...")
                    token = get_token()
                    policies_since_refresh = 0
                    resp = post_policy(token, policy_body)

                if resp.status_code in (200, 201):
                    policy_id = resp.json().get("id", "")
                    log.info(f"  SUCCESS — policy created (id={policy_id})")
                    record.update({"Status": "SUCCESS", "PolicyId": policy_id})
                else:
                    log.error(f"  FAILED — POST HTTP {resp.status_code}: {resp.text}")
                    record.update({"Status": "FAILED", "Error": f"POST HTTP {resp.status_code}: {resp.text}"})

        except requests.Timeout:
            msg = f"Request timed out after {REQUEST_TIMEOUT}s"
            log.error(f"  ERROR — {msg}")
            record.update({"Status": "ERROR", "Error": msg})
        except requests.ConnectionError as e:
            msg = f"Connection error: {e}"
            log.error(f"  ERROR — {msg}")
            record.update({"Status": "ERROR", "Error": msg})
        except requests.HTTPError as e:
            msg = f"API error: {e}"
            log.error(f"  ERROR — {msg}")
            record.update({"Status": "ERROR", "Error": msg})
        except ValueError as e:
            log.error(f"  ERROR — {e}")
            record.update({"Status": "ERROR", "Error": str(e)})
        except Exception as e:
            log.error(f"  ERROR — Unexpected: {e}", exc_info=True)
            record.update({"Status": "ERROR", "Error": f"Unexpected: {e}"})

        results.append(record)
        policies_since_refresh += 1

    # ── Output ─────────────────────────────────────────────────────────────────
    out_df = pd.DataFrame(results)
    out_df.to_csv("output/migration_results.csv", index=False)

    success   = (out_df["Status"] == "SUCCESS").sum()
    updated   = (out_df["Status"] == "UPDATED").sum()
    skipped   = (out_df["Status"] == "SKIPPED").sum()
    failed    = out_df["Status"].isin(["FAILED", "ERROR"]).sum()

    log.info("=" * 60)
    log.info(f"Done — {success} created, {updated} updated, {skipped} skipped (no changes), {failed} failed out of {len(results)} total.")
    log.info("Details  → logs/migration.log")
    log.info("Summary  → logs/summary.log")
    log.info("Results  → output/migration_results.csv")

    write_summary(results)


if __name__ == "__main__":
    migrate_policies()
