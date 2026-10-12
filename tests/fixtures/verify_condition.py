"""Deterministic repository-contract example, invoked by runtime acceptance tests.

The host fixture stands in for an authoritative external-review reader. Its
lineage pairs stand in for Git ancestry plus a reviewed QA-only descendant
allowlist. Neither source comes from the worker ticket or closure artifact.
A live repository must implement those two reads against its actual sources.
"""

import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
policy = json.loads(Path(sys.argv[1]).read_text())
required = {
    f["url"]: f
    for f in policy["findings"]
    if f["native_severity"] in {"P0", "P1", "P2"}
}
missing = []
receipt = request["receipt"]
if required:
    if receipt is None:
        missing = sorted(required)
    else:
        closure = json.loads(receipt["artifact_content"])
        records = closure.get("closures")
        if not isinstance(records, list):
            records = []
        candidate = receipt["candidate_sha"]
        lineage_valid = (
            candidate == policy["candidate_sha"]
            or [candidate, policy["candidate_sha"]] in policy["qa_descendants"]
        )
        for identity, finding in required.items():
            rows = [
                r
                for r in records
                if isinstance(r, dict) and r.get("finding_url") == identity
            ]
            if len(rows) != 1:
                missing.append(identity)
                continue
            row = rows[0]
            valid = (
                row.get("reviewed_sha") == finding["reviewed_sha"]
                and row.get("severity") == finding["native_severity"]
                and row.get("candidate_sha") == candidate
                and (
                    finding["reviewed_sha"] == candidate
                    or [finding["reviewed_sha"], candidate] in policy["ancestry"]
                )
                and row.get("disposition") == "closed"
                and isinstance(row.get("rationale"), str)
                and bool(row["rationale"].strip())
                and lineage_valid
            )
            if not valid:
                missing.append(identity)
print(json.dumps({"passed": not missing, "missing": sorted(missing)}))
