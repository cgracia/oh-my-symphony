"""Versioned, transactional schema migrations for `.symphony/state.db`.

The registry used to keep its DDL inline as `CREATE TABLE IF NOT EXISTS`
plus a hand-rolled `PRAGMA table_info` diff. That is fine for one table but
gives no way to tell "this database predates feature X" from "this database
already has feature X", and no place to hang a pre-upgrade backup.

Migrations are an ordered, append-only list. Each runs exactly once under
`BEGIN IMMEDIATE`, and the applied version is committed in the same
transaction as its DDL, so a crash mid-upgrade leaves the database at the
last fully applied version rather than half-migrated.

Version 1 is the pre-migration baseline. It is written to be idempotent so
databases created by older Symphony builds — which have the tables but no
`schema_migrations` row — adopt version 1 without any data movement.

Adding a migration: append to `MIGRATIONS` with the next version number.
Never edit or reorder an existing entry; released databases have already
recorded it as applied.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# Schema version that first carries the (now inert) flow-engine tables.
# Upgrading a populated database across this line takes a backup first.
# The flow engine itself was removed; the constant survives because the
# backup rule is keyed to the version, not to the feature.
FIRST_LEGACY_FLOW_TABLE_VERSION = 2
# Back-compat alias for anything still importing the old name.
FIRST_GOVERNED_WORKFLOW_VERSION = FIRST_LEGACY_FLOW_TABLE_VERSION
RELEASE_PROVENANCE_VERSION = 5
RELEASE_CYCLE_AUTHORITY_VERSION = 6
RUN_EXPLORER_VERSION = 7
DURABLE_CONTINUATION_VERSION = 8


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """`ALTER TABLE ADD COLUMN` guarded by a column-presence check.

    The duplicate-column rescue keeps this safe when two processes race the
    same migration; SQLite reports that as an `OperationalError`, not a
    constraint violation, so it cannot be caught more narrowly.
    """
    if column in _table_columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_001_baseline(conn: sqlite3.Connection) -> None:
    """Dispatch leases and issue flags — the schema as of Symphony 0.16."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL,
            identifier TEXT NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            attempt INTEGER,
            attempt_kind TEXT NOT NULL,
            agent_kind TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            lease_expires_at TEXT,
            last_progress_at TEXT,
            completed_at TEXT,
            owner_pid INTEGER,
            owner_boot_id TEXT,
            backend_agent_pid INTEGER
        )
        """
    )
    # Pre-owner databases: NULL owners read as "unknown, presumed dead" in
    # reclaim_dead_owner_leases.
    _add_column_if_missing(conn, "runs", "owner_pid", "INTEGER")
    _add_column_if_missing(conn, "runs", "owner_boot_id", "TEXT")
    _add_column_if_missing(conn, "runs", "backend_agent_pid", "INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_issue_status_lease
        ON runs(issue_id, status, lease_expires_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_flags (
            issue_id TEXT PRIMARY KEY,
            retry_attempt INTEGER,
            budget_exhausted INTEGER NOT NULL DEFAULT 0,
            paused INTEGER NOT NULL DEFAULT 0,
            pause_reason TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    _add_column_if_missing(conn, "issue_flags", "pause_reason", "TEXT")


def _migrate_002_legacy_flow_tables(conn: sqlite3.Connection) -> None:
    """INERT: node-level execution ledger for the removed flow engine.

    The governed flow engine (`symphony/flow/**`) is gone and nothing in
    `src/` reads `workflow_snapshots`, `node_runs`, `approvals`, `artifacts`
    or the three extra `runs` columns any more. The migration stays exactly
    as it was so an existing `.symphony/state.db` keeps reporting schema
    version 2 and the upgrade chain stays valid; a future migration may drop
    the tables once every deployed database has crossed this line.

    Everything here is additive. `runs.status` keeps its lease/history
    meaning; the legacy execution state lives in the separate
    `execution_status` column, so an older binary reading this database
    still sees valid lease rows.
    """
    _add_column_if_missing(conn, "runs", "execution_mode", "TEXT")
    _add_column_if_missing(conn, "runs", "execution_status", "TEXT")
    _add_column_if_missing(conn, "runs", "attention_reason", "TEXT")
    _add_column_if_missing(conn, "runs", "workflow_name", "TEXT")
    _add_column_if_missing(conn, "runs", "workflow_version", "INTEGER")
    _add_column_if_missing(conn, "runs", "workflow_hash", "TEXT")
    _add_column_if_missing(conn, "runs", "ticket_snapshot_json", "TEXT")
    _add_column_if_missing(conn, "runs", "terminal_reason", "TEXT")
    _add_column_if_missing(conn, "runs", "input_tokens", "INTEGER")
    _add_column_if_missing(conn, "runs", "output_tokens", "INTEGER")
    _add_column_if_missing(conn, "runs", "cost_usd", "REAL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_execution_status
        ON runs(execution_status, updated_at)
        """
    )

    # Content-addressed definitions: identical workflow YAML across many runs
    # is stored once instead of copying up to 1 MiB of JSON per run row.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_snapshots (
            workflow_hash TEXT PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            normalized_json TEXT NOT NULL,
            source_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # "This issue already has a nonterminal governed run" — distinct from
    # "this process owns a live worker lease", which stays in `runs`.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_fences (
            issue_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_runs (
            node_run_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            node_type TEXT NOT NULL,
            status TEXT NOT NULL,
            backend_kind TEXT,
            workspace_access TEXT,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error_class TEXT,
            error_code TEXT,
            error_message_redacted TEXT,
            output_preview TEXT,
            output_sha256 TEXT,
            session_id TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL,
            head_before TEXT,
            head_after TEXT,
            diffstat_json TEXT,
            external_operation_key TEXT,
            UNIQUE(run_id, node_id, attempt)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_node_runs_run_status
        ON node_runs(run_id, status)
        """
    )

    # Append-only ledger. `seq` is allocated inside the same transaction as
    # the state transition it describes, so the stream can never show an
    # event for a transition that rolled back.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            node_id TEXT,
            type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json_redacted TEXT NOT NULL,
            PRIMARY KEY(run_id, seq)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            scope TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_expired_at TEXT,
            metadata_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_artifacts_run_node
        ON artifacts(run_id, node_id)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_attempt INTEGER NOT NULL,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            instructions TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            resolved_at TEXT,
            decision TEXT,
            actor TEXT,
            source TEXT,
            comment TEXT,
            UNIQUE(run_id, node_id, node_attempt)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_approvals_status
        ON approvals(status, requested_at)
        """
    )


def _migrate_003_release_gates(conn: sqlite3.Connection) -> None:
    """Host-owned authority for pending and approved application releases.

    Board labels and ticket bodies are worker-editable, so they remain useful
    routing/audit signals but cannot authorize finalizer dispatch.  The single
    row per finalizer is written as ``pending`` before verifier dispatch or
    finalizer relinking, then upgraded atomically only after GREEN validation.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS release_gates (
            finalizer_identifier TEXT PRIMARY KEY,
            verifier_issue_id TEXT NOT NULL,
            verifier_identifier TEXT NOT NULL,
            expected_contract_sha256 TEXT NOT NULL,
            cycle_fingerprint TEXT NOT NULL,
            approved_fingerprint TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending', 'approved')),
            target_branch TEXT,
            approved_target_sha TEXT,
            verifier_run_id TEXT,
            finalizer_run_id TEXT,
            generation TEXT NOT NULL DEFAULT '',
            finalizer_completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migrate_004_release_finalizer_run_binding(conn: sqlite3.Connection) -> None:
    """Bind terminal finalizer state to one host-authorized run."""
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(release_gates)")
    }
    if "finalizer_run_id" not in columns:
        conn.execute(
            "ALTER TABLE release_gates ADD COLUMN finalizer_run_id TEXT"
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_release_gates_verifier
        ON release_gates(verifier_identifier)
        """
    )


def _migrate_005_release_provenance(conn: sqlite3.Connection) -> None:
    """Durable cycle generations, finalizer proof, and evidence identity."""
    _add_column_if_missing(
        conn, "release_gates", "generation", "TEXT NOT NULL DEFAULT ''"
    )
    _add_column_if_missing(
        conn, "release_gates", "finalizer_completed_at", "TEXT"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS release_evidence_issues (
            issue_id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL UNIQUE,
            finalizer_identifier TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('verifier', 'finalizer')),
            cycle_generation TEXT NOT NULL,
            retired INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_release_evidence_finalizer
        ON release_evidence_issues(finalizer_identifier, role, retired)
        """
    )
    # v3/v4 rows predate host-owned cycle generations and durable evidence
    # identity.  Never grandfather an old APPROVED row: it has no exact-cycle
    # finalizer completion provenance, so force a fresh verification.  Pending
    # rows also get a new timestamp and lose any pre-migration run binding so a
    # lease started before this migration cannot authorize the new generation.
    migrated_at = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT finalizer_identifier, verifier_issue_id, verifier_identifier
        FROM release_gates
        """
    ).fetchall()
    for row in rows:
        generation = uuid.uuid4().hex
        conn.execute(
            """
            UPDATE release_gates
            SET generation = ?,
                approved_fingerprint = NULL,
                status = 'pending',
                target_branch = NULL,
                approved_target_sha = NULL,
                verifier_run_id = NULL,
                finalizer_run_id = NULL,
                finalizer_completed_at = NULL,
                updated_at = ?
            WHERE finalizer_identifier = ?
            """,
            (generation, migrated_at, str(row[0])),
        )
        conn.execute(
            """
            INSERT INTO release_evidence_issues (
                issue_id, identifier, finalizer_identifier, role,
                cycle_generation, retired, recorded_at, updated_at
            ) VALUES (?, ?, ?, 'verifier', ?, 0, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                identifier = excluded.identifier,
                finalizer_identifier = excluded.finalizer_identifier,
                role = 'verifier',
                cycle_generation = excluded.cycle_generation,
                retired = 0,
                updated_at = excluded.updated_at
            """,
            (
                str(row[1]),
                str(row[2]),
                str(row[0]),
                generation,
                migrated_at,
                migrated_at,
            ),
        )


def _migrate_006_release_cycle_authority(conn: sqlite3.Connection) -> None:
    """Durable lifecycle-item identity and non-replayable completion proof.

    Repair/verifier labels live on worker-editable board files, so they cannot
    be the identity used to reconcile a partially written release cycle.  The
    host records the exact ticket allocated for each fingerprint/role/key in
    this table and restores mutable board metadata from that mapping.

    Completed v5 finalizers have no ticket-version token.  They therefore
    cannot prove that a later terminal board edit is the transition produced
    by the bound run; invalidate those approvals and require fresh Verify.
    """
    _add_column_if_missing(
        conn, "release_gates", "finalizer_completion_token", "TEXT"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS release_cycle_items (
            finalizer_identifier TEXT NOT NULL,
            cycle_fingerprint TEXT NOT NULL,
            item_role TEXT NOT NULL CHECK(item_role IN ('repair', 'verifier')),
            item_key TEXT NOT NULL,
            issue_id TEXT NOT NULL UNIQUE,
            identifier TEXT NOT NULL UNIQUE,
            recorded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                finalizer_identifier, cycle_fingerprint, item_role, item_key
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_release_cycle_items_ticket
        ON release_cycle_items(identifier, item_role)
        """
    )

    migrated_at = datetime.now(timezone.utc).isoformat()
    completed = conn.execute(
        """
        SELECT finalizer_identifier, verifier_issue_id, verifier_identifier
        FROM release_gates
        WHERE finalizer_completed_at IS NOT NULL
           OR (status = 'approved' AND finalizer_run_id IS NOT NULL)
        """
    ).fetchall()
    for row in completed:
        generation = uuid.uuid4().hex
        conn.execute(
            """
            UPDATE release_gates
            SET generation = ?,
                approved_fingerprint = NULL,
                status = 'pending',
                target_branch = NULL,
                approved_target_sha = NULL,
                verifier_run_id = NULL,
                finalizer_run_id = NULL,
                finalizer_completed_at = NULL,
                finalizer_completion_token = NULL,
                updated_at = ?
            WHERE finalizer_identifier = ?
            """,
            (generation, migrated_at, str(row[0])),
        )
        conn.execute(
            """
            UPDATE release_evidence_issues
            SET cycle_generation = ?, retired = 0, updated_at = ?
            WHERE issue_id = ? AND identifier = ?
            """,
            (generation, migrated_at, str(row[1]), str(row[2])),
        )


def _migrate_007_run_explorer(conn: sqlite3.Connection) -> None:
    """Add bounded diagnostic metadata without reviving legacy ``run_events``."""
    # Some early test/repair databases recorded v1 without retaining its tables.
    # Recreate the idempotent baseline rather than making this additive upgrade
    # fail halfway through.
    if not _table_columns(conn, "runs"):
        _migrate_001_baseline(conn)
    for column, declaration in (
        ("input_tokens", "INTEGER"),
        ("cache_input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("total_tokens", "INTEGER"),
        ("failure_class", "TEXT"),
        ("failure_message", "TEXT"),
        ("branch_name", "TEXT"),
        ("commit_sha", "TEXT"),
    ):
        _add_column_if_missing(conn, "runs", column, declaration)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attempt_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attempt_events_run_event
        ON attempt_events(run_id, event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_completed_at
        ON runs(completed_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS delete_attempt_events_with_run
        AFTER DELETE ON runs
        BEGIN
            DELETE FROM attempt_events WHERE run_id = OLD.run_id;
        END
        """
    )


def _migrate_008_durable_continuation(conn: sqlite3.Connection) -> None:
    """Add private recovery checkpoints and one-shot successor provenance."""
    if not _table_columns(conn, "runs"):
        _migrate_001_baseline(conn)
    # Keep the upgrade additive so older binaries can still read ordinary run
    # history. The private session id deliberately has no diagnostic index or
    # projection; only the continuation acquisition path reads it.
    for column, declaration in (
        ("backend_process_identity", "TEXT"),
        ("resume_session_id", "TEXT"),
        ("checkpoint_state", "TEXT"),
        ("checkpoint_turn", "INTEGER"),
        ("checkpointed_at", "TEXT"),
        ("continued_from_run_id", "TEXT"),
    ):
        _add_column_if_missing(conn, "runs", column, declaration)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_continued_from_unique
        ON runs(continued_from_run_id)
        WHERE continued_from_run_id IS NOT NULL
        """
    )


def ensure_budget_grants_table(conn: sqlite3.Connection) -> None:
    """Append-only ledger of operator budget grants for exhausted tickets.

    `grant_id` is the caller's idempotency key. Rows are never deleted;
    `turns_used` only grows and is charged before each granted turn starts.

    Deliberately idempotent and outside `MIGRATIONS`: upstream already uses
    schema version 9, so a locally carried numbered migration would make a
    later upstream upgrade skip upstream's own version 9. An upstream
    contribution should register this as the next numbered migration.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS budget_grants (
            grant_id TEXT PRIMARY KEY,
            issue_id TEXT NOT NULL,
            identifier TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            extra_turns INTEGER NOT NULL CHECK (extra_turns > 0),
            turns_used INTEGER NOT NULL DEFAULT 0
                CHECK (turns_used >= 0 AND turns_used <= extra_turns),
            from_state TEXT NOT NULL,
            target_state TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_budget_grants_issue
        ON budget_grants(issue_id, created_at)
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline_runs_and_issue_flags", _migrate_001_baseline),
    # The recorded name stays "governed_workflow_ledger" so existing
    # `schema_migrations` rows keep matching; the tables it creates are inert.
    Migration(2, "governed_workflow_ledger", _migrate_002_legacy_flow_tables),
    Migration(3, "release_gate_authority", _migrate_003_release_gates),
    Migration(
        4,
        "release_finalizer_run_binding",
        _migrate_004_release_finalizer_run_binding,
    ),
    Migration(
        RELEASE_PROVENANCE_VERSION,
        "release_provenance",
        _migrate_005_release_provenance,
    ),
    Migration(
        RELEASE_CYCLE_AUTHORITY_VERSION,
        "release_cycle_authority",
        _migrate_006_release_cycle_authority,
    ),
    Migration(
        RUN_EXPLORER_VERSION,
        "run_explorer_diagnostics",
        _migrate_007_run_explorer,
    ),
    Migration(
        DURABLE_CONTINUATION_VERSION,
        "durable_crash_continuation",
        _migrate_008_durable_continuation,
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied version, or 0 for an unmigrated database."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def backup_database(conn: sqlite3.Connection, path: Path) -> Path:
    """Snapshot the live database to a timestamped sibling file.

    Uses SQLite's online backup API rather than copying the file, because a
    WAL database's committed contents are split across `-wal` and the main
    file; a plain copy of one of them can lose recent writes.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = path.with_name(
        f"{path.name}.backup-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    target = sqlite3.connect(destination)
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def apply_migrations(conn: sqlite3.Connection, path: Path) -> list[int]:
    """Bring `conn` up to `LATEST_SCHEMA_VERSION`; return versions applied.

    `path` is only used to place the pre-upgrade backup next to the
    database. Returns an empty list when nothing was pending.
    """
    applied: list[int] = []
    version = current_schema_version(conn)
    pending = [m for m in MIGRATIONS if m.version > version]
    if not pending:
        return applied

    # Only worth backing up a database that already holds run history; a
    # database still at version 0 with no rows has nothing to lose.
    crosses_backup_line = version < FIRST_LEGACY_FLOW_TABLE_VERSION <= max(
        m.version for m in pending
    )
    release_provenance_backfill = (
        version < RELEASE_PROVENANCE_VERSION
        <= max(migration.version for migration in pending)
        and _has_existing_release_gates(conn)
    )
    release_cycle_authority_backfill = (
        version < RELEASE_CYCLE_AUTHORITY_VERSION
        <= max(migration.version for migration in pending)
        and _has_existing_release_gates(conn)
    )
    if (
        crosses_backup_line and _has_existing_runs(conn)
    ) or release_provenance_backfill or release_cycle_authority_backfill:
        backup_database(conn, path)

    for migration in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another service may have migrated the shared WAL database after
            # our optimistic `pending` snapshot but before this write lock.
            # Re-read under the lock so concurrent starters never double-insert
            # the same schema_migrations version.
            if current_schema_version(conn) >= migration.version:
                conn.execute("COMMIT")
                continue
            migration.apply(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(migration.version)
    return applied


def _has_existing_runs(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
    ).fetchone()
    if table is None:
        return False
    row = conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
    return row is not None


def _has_existing_release_gates(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'release_gates'"
    ).fetchone()
    if table is None:
        return False
    return conn.execute("SELECT 1 FROM release_gates LIMIT 1").fetchone() is not None
