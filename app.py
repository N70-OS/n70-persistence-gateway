"""
app.py

n70-persistence-gateway - Cloud Run HTTP layer over firestore_client.py.
Exposes four endpoints (POST /firestore/get, /set, /query, /delete) so
GAS (and, longer term, other Cloud Run services) reach tenant-isolated
Firestore data through a service-account-impersonation boundary instead
of a human OAuth token (Section 5, N70 OS Tenant Isolation plan).

This module's own responsibility is request validation only -- the
caller's identity is already established by Cloud Run's IAM-authenticated
invocation (private service, same pattern as n70-stage2-gateway/
n70-stage3-gateway), and the actual per-tenant data boundary is enforced
by firestore_client.get_tenant_client()'s own impersonation, not by any
check in this file.
"""

import os
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health_check():
    """
    Cloud Run pings this on startup to confirm the container is alive.
    Must respond fast and without touching Firestore or any tenant identity.
    """
    return jsonify({"status": "ok", "service": "n70-persistence-gateway"}), 200


def _require(payload, *fields):
    """Returns the first missing/blank required field name, or None if all present."""
    for f in fields:
        if not payload.get(f):
            return f
    return None


@app.route("/firestore/get", methods=["POST"])
def firestore_get():
    from firestore_client import get_document
    payload = request.get_json(silent=True) or {}
    missing = _require(payload, "tenantId", "collection", "docId")
    if missing:
        return jsonify({"status": "error", "message": f"missing required field: {missing}"}), 400
    try:
        result = get_document(payload["tenantId"], payload["collection"], payload["docId"])
        return jsonify({"status": "complete", "result": result}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/firestore/set", methods=["POST"])
def firestore_set():
    from firestore_client import set_document
    payload = request.get_json(silent=True) or {}
    missing = _require(payload, "tenantId", "collection", "docId", "fields")
    if missing:
        return jsonify({"status": "error", "message": f"missing required field: {missing}"}), 400
    if not isinstance(payload["fields"], dict):
        return jsonify({"status": "error", "message": "fields must be a JSON object"}), 400
    try:
        set_document(
            payload["tenantId"],
            payload["collection"],
            payload["docId"],
            payload["fields"],
            merge=bool(payload.get("merge", False)),
        )
        return jsonify({"status": "complete"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/firestore/query", methods=["POST"])
def firestore_query():
    from firestore_client import query_collection
    payload = request.get_json(silent=True) or {}
    missing = _require(payload, "tenantId", "collection")
    if missing:
        return jsonify({"status": "error", "message": f"missing required field: {missing}"}), 400
    raw_filters = payload.get("filters")
    filters = None
    if raw_filters is not None:
        if not isinstance(raw_filters, list):
            return jsonify({"status": "error", "message": "filters must be a list of [field, op, value] triples"}), 400
        try:
            filters = [tuple(f) for f in raw_filters]
        except TypeError:
            return jsonify({"status": "error", "message": "each filter must be a [field, op, value] triple"}), 400
    try:
        results = query_collection(
            payload["tenantId"],
            payload["collection"],
            filters=filters,
            limit=payload.get("limit"),
        )
        return jsonify({"status": "complete", "result": results}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/firestore/delete", methods=["POST"])
def firestore_delete():
    from firestore_client import delete_document
    payload = request.get_json(silent=True) or {}
    missing = _require(payload, "tenantId", "collection", "docId")
    if missing:
        return jsonify({"status": "error", "message": f"missing required field: {missing}"}), 400
    try:
        delete_document(payload["tenantId"], payload["collection"], payload["docId"])
        return jsonify({"status": "complete"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
