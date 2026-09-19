"""
app.py

n70-persistence-gateway - Cloud Run HTTP layer over firestore_client.py.
Exposes four POST endpoints (/firestore/get, /set, /query, /delete) so GAS
(and, longer term, other Cloud Run services) reach tenant-isolated
Firestore data through a service-account-impersonation boundary instead
of a human OAuth token (Section 5, N70 OS Tenant Isolation plan).

Phase 3.0 changes (Tenant Isolation):
  1. Callers send engagementId ONLY. The gateway resolves the tenant itself
     from the n70-control registry (collection: engagements, document ID =
     engagementId, fields: tenantId, status). A caller-supplied tenantId is
     rejected, so a caller can never name a tenant directly.
  2. Unregistered or non-active engagements are refused (fail closed).
     Exception: an UNREGISTERED engagementId that starts with TEST_ resolves to
     the shared test tenant, so test-mode deals (TEST_DEAL_<timestamp>) work
     without registration. A registered entry always wins, and an unregistered
     TEST_ id can only ever reach the test tenant.
  3. Every call writes one audit line (JSON, no document content) that
     carries engagementId and tenantId.
  4. Responses are JSON-safe and match what the GAS wrappers expect:
     timestamps come back as ISO strings (not Flask's default RFC-822),
     query results come back as [{docId, fields}], and an empty fields
     object is a valid write.

Not in this file, by design:
  - The TEST_ prefix guard on deletes stays in the GAS wrapper.
  - Per-collection routing (per-tenant vs shared) is added in Phase 3.1.

The per-tenant data boundary itself is still enforced by
firestore_client.get_tenant_client()'s service-account impersonation.
"""

import datetime
import json
import re
import time

from flask import Flask, request, jsonify

app = Flask(__name__)

PROJECT_ID = "n70-appscript"
CONTROL_DATABASE = "n70-control"
REGISTRY_COLLECTION = "engagements"

# A suspended engagement stops being served within this many seconds.
CACHE_TTL_SECONDS = 60

# Matches the tenantId format in the Tenant Registry design (Arch 2.75).
TENANT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,18}$")

# dealIds are HubSpot Company IDs (digits) or TEST_DEAL_<timestamp>.
ENGAGEMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")

# Unregistered TEST_ engagements resolve here (and only here).
TEST_ENGAGEMENT_PREFIX = "TEST_"
TEST_TENANT_ID = "test-tenant-1"

_control_client = None
_registry_cache = {}  # engagementId -> (tenantId, expiry epoch seconds)


class RegistryError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def _get_control_client():
    global _control_client
    if _control_client is None:
        from google.cloud import firestore
        _control_client = firestore.Client(project=PROJECT_ID, database=CONTROL_DATABASE)
    return _control_client


def resolve_tenant(engagement_id):
    """Looks up the tenant for an engagement. Raises RegistryError (fail closed)."""
    now = time.time()
    cached = _registry_cache.get(engagement_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        snap = _get_control_client().collection(REGISTRY_COLLECTION).document(engagement_id).get()
    except Exception as e:
        print(json.dumps({"event": "registry_error", "engagementId": engagement_id, "error": str(e)[:300]}), flush=True)
        raise RegistryError("registry unavailable", 503)

    if not snap.exists:
        if engagement_id.startswith(TEST_ENGAGEMENT_PREFIX):
            _registry_cache[engagement_id] = (TEST_TENANT_ID, now + CACHE_TTL_SECONDS)
            return TEST_TENANT_ID
        raise RegistryError("engagement not registered or not active", 403)

    data = snap.to_dict() or {}
    if data.get("status") != "active":
        raise RegistryError("engagement not registered or not active", 403)

    tenant_id = data.get("tenantId")
    if not isinstance(tenant_id, str) or not TENANT_ID_PATTERN.match(tenant_id):
        raise RegistryError("registry entry invalid", 500)

    _registry_cache[engagement_id] = (tenant_id, now + CACHE_TTL_SECONDS)
    return tenant_id


def _audit(op, engagement_id, tenant_id, collection, doc_id, outcome):
    """One JSON line per call. Never includes document content."""
    print(json.dumps({
        "event": "persistence_call",
        "op": op,
        "engagementId": engagement_id,
        "tenantId": tenant_id,
        "collection": collection,
        "docId": doc_id,
        "outcome": outcome,
    }), flush=True)


def _iso(value):
    """ISO-8601 UTC with milliseconds and a trailing Z - same shape as JS toISOString()."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(value.microsecond // 1000)


def _jsonable(value):
    if isinstance(value, datetime.datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _error(message, status):
    return jsonify({"status": "error", "message": message}), status


def _begin(op, required):
    """
    Common front door for every endpoint. Returns (payload, tenant_id, None) on
    success, or (None, None, error_response) on any refusal.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, None, _error("request body must be a JSON object", 400)

    if "tenantId" in payload:
        _audit(op, payload.get("engagementId"), None, payload.get("collection"), payload.get("docId"), "rejected_tenantId_supplied")
        return None, None, _error("tenantId is not accepted - send engagementId", 400)

    for field in ["engagementId"] + list(required):
        if not payload.get(field):
            return None, None, _error("missing required field: " + field, 400)

    engagement_id = payload["engagementId"]
    if not isinstance(engagement_id, str) or not ENGAGEMENT_ID_PATTERN.match(engagement_id):
        return None, None, _error("engagementId format invalid", 400)

    try:
        tenant_id = resolve_tenant(engagement_id)
    except RegistryError as e:
        _audit(op, engagement_id, None, payload.get("collection"), payload.get("docId"), "refused: " + e.message)
        return None, None, _error(e.message, e.status)

    return payload, tenant_id, None


@app.route("/", methods=["GET"])
def health_check():
    """
    Cloud Run pings this on startup to confirm the container is alive.
    Must respond fast and without touching Firestore or any tenant identity.
    """
    return jsonify({"status": "ok", "service": "n70-persistence-gateway"}), 200


@app.route("/firestore/get", methods=["POST"])
def firestore_get():
    payload, tenant_id, err = _begin("get", ["collection", "docId"])
    if err:
        return err
    from firestore_client import get_document
    try:
        result = get_document(tenant_id, payload["collection"], payload["docId"])
        _audit("get", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "ok")
        return jsonify({"status": "complete", "result": _jsonable(result)}), 200
    except Exception as e:
        _audit("get", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "error")
        return _error(str(e), 500)


@app.route("/firestore/set", methods=["POST"])
def firestore_set():
    payload, tenant_id, err = _begin("set", ["collection", "docId"])
    if err:
        return err
    if "fields" not in payload or not isinstance(payload["fields"], dict):
        return _error("fields must be a JSON object", 400)
    from firestore_client import set_document
    try:
        set_document(
            tenant_id,
            payload["collection"],
            payload["docId"],
            payload["fields"],
            merge=bool(payload.get("merge", False)),
        )
        _audit("set", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "ok")
        return jsonify({"status": "complete"}), 200
    except Exception as e:
        _audit("set", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "error")
        return _error(str(e), 500)


@app.route("/firestore/query", methods=["POST"])
def firestore_query():
    payload, tenant_id, err = _begin("query", ["collection"])
    if err:
        return err

    raw_filters = payload.get("filters")
    filters = None
    if raw_filters is not None:
        if not isinstance(raw_filters, list):
            return _error("filters must be a list of [field, op, value] triples", 400)
        try:
            filters = [tuple(f) for f in raw_filters]
        except TypeError:
            return _error("each filter must be a [field, op, value] triple", 400)
        if any(len(f) != 3 for f in filters):
            return _error("each filter must be a [field, op, value] triple", 400)

    from firestore_client import query_collection
    try:
        rows = query_collection(
            tenant_id,
            payload["collection"],
            filters=filters,
            limit=payload.get("limit"),
        )
        results = []
        for row in rows:
            doc_id = row.pop("_id", None)
            results.append({"docId": doc_id, "fields": _jsonable(row)})
        _audit("query", payload["engagementId"], tenant_id, payload["collection"], None, "ok")
        return jsonify({"status": "complete", "result": results}), 200
    except Exception as e:
        _audit("query", payload["engagementId"], tenant_id, payload["collection"], None, "error")
        return _error(str(e), 500)


@app.route("/firestore/delete", methods=["POST"])
def firestore_delete():
    payload, tenant_id, err = _begin("delete", ["collection", "docId"])
    if err:
        return err
    from firestore_client import delete_document
    try:
        delete_document(tenant_id, payload["collection"], payload["docId"])
        _audit("delete", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "ok")
        return jsonify({"status": "complete"}), 200
    except Exception as e:
        _audit("delete", payload["engagementId"], tenant_id, payload["collection"], payload["docId"], "error")
        return _error(str(e), 500)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
