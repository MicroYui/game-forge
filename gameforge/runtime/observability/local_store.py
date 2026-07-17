"""Restart-readable best-effort telemetry storage over an independent SQLite WAL.

This database is deliberately outside the authoritative M4a business UnitOfWork.
It stores immutable canonical DTO payloads and only exposes contracts-level views.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import (
    CursorExpired,
    IntegrityViolation,
    QueryTooBroad,
)
from gameforge.contracts.observability import (
    LogPageV1,
    LogQueryV1,
    LogRecordV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPageV1,
    MetricPointV1,
    MetricQueryV1,
    MetricSeriesV1,
    SpanDataV1,
    SpanPageV1,
    SpanViewV1,
    TraceQueryV1,
    TraceSummaryPageV1,
    TraceSummaryV1,
    span_run_ids,
)
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.observability._fields import redact_span_values
from gameforge.runtime.observability.cursor import OpaqueCursorCodec
from gameforge.runtime.observability.metrics import (
    aggregate_metric_points,
    metric_descriptor_key,
    validate_metric_query_shape,
)
from gameforge.runtime.observability.run_scope import (
    RetainedLogPage,
    RetainedTraceRunScope,
    TelemetryRunScopeMode,
    run_ids_are_in_scope,
    validate_telemetry_run_scope,
)


_SCHEMA_VERSION = 2
_READ_KINDS = frozenset({"traces", "run_traces", "spans", "logs", "metrics"})
_PIN_OWNER_KINDS = frozenset({"slo", "alert", "saved_query"})


@dataclass(frozen=True, slots=True)
class LocalTelemetryLimits:
    max_time_range: timedelta = timedelta(days=7)
    max_page_size: int = 1000
    max_series: int = 500
    max_points: int = 10_000
    max_points_per_series: int = 10_000
    max_span_count: int = 10_000
    max_raw_metric_points: int = 100_000
    max_resolution_s: int = 24 * 60 * 60
    max_response_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_time_range <= timedelta(0):
            raise ValueError("max_time_range must be positive")
        for name in (
            "max_page_size",
            "max_series",
            "max_points",
            "max_points_per_series",
            "max_span_count",
            "max_raw_metric_points",
            "max_resolution_s",
            "max_response_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class LocalTelemetryRetention:
    spans: timedelta = timedelta(days=7)
    logs: timedelta = timedelta(days=7)
    metric_points: timedelta = timedelta(days=30)
    metric_descriptors: timedelta = timedelta(days=30)
    read_snapshot_ttl: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        for name in (
            "spans",
            "logs",
            "metric_points",
            "metric_descriptors",
            "read_snapshot_ttl",
        ):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} retention must be positive")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "retention_schema_version": "local-telemetry-retention@1",
                "spans_us": _timedelta_us(self.spans),
                "logs_us": _timedelta_us(self.logs),
                "metric_points_us": _timedelta_us(self.metric_points),
                "metric_descriptors_us": _timedelta_us(self.metric_descriptors),
                "read_snapshot_ttl_us": _timedelta_us(self.read_snapshot_ttl),
            }
        )


@dataclass(frozen=True, slots=True)
class LocalTelemetryRetentionResult:
    deleted_spans: int
    deleted_logs: int
    deleted_metric_points: int
    deleted_metric_descriptors: int
    deleted_metric_registries: int
    deleted_read_snapshots: int
    deleted_expired_pins: int


@dataclass(frozen=True, slots=True)
class _ReadSnapshot:
    snapshot_id: str
    kind: str
    query_hash: str
    authz_fingerprint: str
    principal_binding: str | None
    high_watermark: int
    secondary_high_watermark: int
    expires_at: datetime
    retention_fingerprint: str
    offset: int


def _timedelta_us(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("telemetry timestamps must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation("persisted telemetry timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise IntegrityViolation("persisted telemetry timestamp is not UTC")
    return parsed.astimezone(UTC)


def _wire(value: Any) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _wire_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _enter_wal_mode(connection: sqlite3.Connection, *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            return mode
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _labels_key(labels: Mapping[str, str]) -> str:
    return canonical_json(dict(labels))


class LocalTelemetryStore:
    """Independent local trace/log/metric adapter with retained read snapshots."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: UtcClock,
        signing_key: bytes,
        limits: LocalTelemetryLimits | None = None,
        retention: LocalTelemetryRetention | None = None,
        busy_timeout_s: float = 5.0,
    ) -> None:
        if isinstance(busy_timeout_s, bool) or busy_timeout_s <= 0:
            raise ValueError("busy_timeout_s must be positive")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._limits = limits or LocalTelemetryLimits()
        self._retention = retention or LocalTelemetryRetention()
        self._busy_timeout_s = float(busy_timeout_s)
        self._cursor_codec = OpaqueCursorCodec(signing_key=signing_key, clock=clock)
        self._closed = False
        self._initialize()

    def __enter__(self) -> LocalTelemetryStore:
        self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    @property
    def journal_mode(self) -> str:
        with self._connection() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    @property
    def metric_point_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM metric_points").fetchone()
        return int(row[0])

    @property
    def metric_registry_ref(self) -> MetricDescriptorRegistryRefV1:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM telemetry_meta WHERE key = 'active_metric_registry'"
            ).fetchone()
        if row is None:
            raise IntegrityViolation("no metric descriptor registry is frozen")
        try:
            return MetricDescriptorRegistryRefV1.model_validate_json(row[0])
        except ValueError as exc:
            raise IntegrityViolation("active metric registry binding is invalid") from exc

    def get_metric_descriptor_registry(self) -> MetricDescriptorRegistryV1 | None:
        with self._connection() as connection:
            key = self._active_registry_key(connection)
            if key is None:
                return None
            row = connection.execute(
                """
                SELECT payload FROM metric_registries
                WHERE registry_version = ? AND registry_digest = ?
                """,
                key,
            ).fetchone()
        if row is None:
            raise IntegrityViolation("active metric registry payload is unavailable")
        try:
            return MetricDescriptorRegistryV1.model_validate_json(row["payload"])
        except ValueError as exc:
            raise IntegrityViolation("persisted metric registry payload is invalid") from exc

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("local telemetry store is closed")

    def _now(self) -> datetime:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise IntegrityViolation("telemetry clock must return UTC")
        return now.astimezone(UTC)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._require_open()
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_s,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_s * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_s,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_s * 1000)}")
            mode = _enter_wal_mode(connection, timeout_s=self._busy_timeout_s)
            if mode != "wal":
                raise IntegrityViolation("local telemetry SQLite database did not enter WAL mode")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, _SCHEMA_VERSION}:
                raise IntegrityViolation(
                    "local telemetry schema version is unsupported",
                    actual_version=version,
                    supported_version=_SCHEMA_VERSION,
                )
            if version == 1:
                connection.execute("DELETE FROM read_snapshots WHERE kind = 'logs'")
                connection.execute(
                    """
                    ALTER TABLE read_snapshots
                        ADD COLUMN secondary_high_watermark INTEGER NOT NULL DEFAULT 0
                    """
                )
                connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS telemetry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS spans (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    service TEXT,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE(trace_id, span_id)
                );
                CREATE INDEX IF NOT EXISTS ix_spans_started
                    ON spans(started_at, trace_id, span_id);
                CREATE INDEX IF NOT EXISTS ix_spans_trace
                    ON spans(trace_id, started_at, span_id);
                CREATE INDEX IF NOT EXISTS ix_spans_ended ON spans(ended_at);
                CREATE INDEX IF NOT EXISTS ix_spans_run ON spans(run_id, started_at);
                CREATE INDEX IF NOT EXISTS ix_spans_service ON spans(service, started_at);

                CREATE TABLE IF NOT EXISTS logs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL UNIQUE,
                    ts_utc TEXT NOT NULL,
                    level TEXT NOT NULL,
                    service TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    run_id TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    producer_run_id TEXT,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_logs_order ON logs(ts_utc, log_id);
                CREATE INDEX IF NOT EXISTS ix_logs_service ON logs(service, ts_utc);
                CREATE INDEX IF NOT EXISTS ix_logs_event ON logs(event_name, ts_utc);
                CREATE INDEX IF NOT EXISTS ix_logs_run ON logs(run_id, ts_utc);
                CREATE INDEX IF NOT EXISTS ix_logs_trace ON logs(trace_id, ts_utc);

                CREATE TABLE IF NOT EXISTS metric_registries (
                    registry_version INTEGER NOT NULL,
                    registry_digest TEXT NOT NULL,
                    global_series_limit INTEGER NOT NULL,
                    inserted_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY(registry_version, registry_digest),
                    UNIQUE(registry_version)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metric_descriptors (
                    metric_name TEXT NOT NULL,
                    descriptor_version INTEGER NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    label_keys TEXT NOT NULL,
                    histogram_bounds TEXT NOT NULL,
                    series_limit INTEGER NOT NULL,
                    inserted_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY(metric_name, descriptor_version, descriptor_digest),
                    UNIQUE(metric_name, descriptor_version)
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metric_registry_descriptors (
                    registry_version INTEGER NOT NULL,
                    registry_digest TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    descriptor_version INTEGER NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    PRIMARY KEY(
                        registry_version,
                        registry_digest,
                        metric_name,
                        descriptor_version,
                        descriptor_digest
                    ),
                    FOREIGN KEY(registry_version, registry_digest)
                        REFERENCES metric_registries(registry_version, registry_digest)
                        ON DELETE CASCADE,
                    FOREIGN KEY(metric_name, descriptor_version, descriptor_digest)
                        REFERENCES metric_descriptors(
                            metric_name,
                            descriptor_version,
                            descriptor_digest
                        ) ON DELETE RESTRICT
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metric_points (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    point_id TEXT NOT NULL UNIQUE,
                    metric_name TEXT NOT NULL,
                    descriptor_version INTEGER NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    ts_utc TEXT NOT NULL,
                    value REAL NOT NULL,
                    labels TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    FOREIGN KEY(metric_name, descriptor_version, descriptor_digest)
                        REFERENCES metric_descriptors(
                            metric_name,
                            descriptor_version,
                            descriptor_digest
                        ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS ix_metric_points_order
                    ON metric_points(ts_utc, point_id);
                CREATE INDEX IF NOT EXISTS ix_metric_points_descriptor
                    ON metric_points(
                        metric_name,
                        descriptor_version,
                        descriptor_digest,
                        labels,
                        ts_utc,
                        point_id
                    );

                CREATE TABLE IF NOT EXISTS metric_point_identities (
                    point_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS read_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    authz_fingerprint TEXT NOT NULL,
                    high_watermark INTEGER NOT NULL,
                    secondary_high_watermark INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    retention_fingerprint TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_read_snapshots_expiry
                    ON read_snapshots(expires_at);

                CREATE TABLE IF NOT EXISTS read_snapshot_descriptors (
                    snapshot_id TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    descriptor_version INTEGER NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    PRIMARY KEY(
                        snapshot_id,
                        metric_name,
                        descriptor_version,
                        descriptor_digest
                    ),
                    FOREIGN KEY(snapshot_id) REFERENCES read_snapshots(snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(metric_name, descriptor_version, descriptor_digest)
                        REFERENCES metric_descriptors(
                            metric_name,
                            descriptor_version,
                            descriptor_digest
                        ) ON DELETE RESTRICT
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS metric_descriptor_pins (
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    descriptor_version INTEGER NOT NULL,
                    descriptor_digest TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY(
                        owner_kind,
                        owner_id,
                        metric_name,
                        descriptor_version,
                        descriptor_digest
                    ),
                    FOREIGN KEY(metric_name, descriptor_version, descriptor_digest)
                        REFERENCES metric_descriptors(
                            metric_name,
                            descriptor_version,
                            descriptor_digest
                        ) ON DELETE RESTRICT
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS ix_metric_descriptor_pins_expiry
                    ON metric_descriptor_pins(expires_at);
                PRAGMA user_version=2;
                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def register_metric_registry(self, registry: MetricDescriptorRegistryV1) -> None:
        registry_payload = _wire(registry)
        registry_hash = _wire_hash(registry_payload)
        now = _format_utc(self._now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                same_version = connection.execute(
                    """
                    SELECT registry_digest, payload, payload_hash
                    FROM metric_registries WHERE registry_version = ?
                    """,
                    (registry.registry_version,),
                ).fetchone()
                if same_version is not None and (
                    same_version["registry_digest"] != registry.registry_digest
                    or same_version["payload_hash"] != registry_hash
                    or same_version["payload"] != registry_payload
                ):
                    raise IntegrityViolation(
                        "metric registry version has conflicting immutable payload"
                    )
                if same_version is None:
                    connection.execute(
                        """
                        INSERT INTO metric_registries(
                            registry_version, registry_digest, global_series_limit,
                            inserted_at, payload, payload_hash
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            registry.registry_version,
                            registry.registry_digest,
                            registry.global_series_limit,
                            now,
                            registry_payload,
                            registry_hash,
                        ),
                    )
                for descriptor in registry.descriptors:
                    self._put_descriptor(connection, descriptor, inserted_at=now)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO metric_registry_descriptors(
                            registry_version, registry_digest, metric_name,
                            descriptor_version, descriptor_digest
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            registry.registry_version,
                            registry.registry_digest,
                            descriptor.metric_name,
                            descriptor.descriptor_version,
                            descriptor.descriptor_digest,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO telemetry_meta(key, value)
                    VALUES ('active_metric_registry', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (_wire(registry.ref),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _put_descriptor(
        connection: sqlite3.Connection,
        descriptor: MetricDescriptorV1,
        *,
        inserted_at: str,
    ) -> None:
        payload = _wire(descriptor)
        payload_hash = _wire_hash(payload)
        existing = connection.execute(
            """
            SELECT descriptor_digest, payload, payload_hash
            FROM metric_descriptors
            WHERE metric_name = ? AND descriptor_version = ?
            """,
            (descriptor.metric_name, descriptor.descriptor_version),
        ).fetchone()
        if existing is not None:
            if (
                existing["descriptor_digest"] != descriptor.descriptor_digest
                or existing["payload_hash"] != payload_hash
                or existing["payload"] != payload
            ):
                raise IntegrityViolation(
                    "metric descriptor identity has conflicting immutable payload"
                )
            return
        connection.execute(
            """
            INSERT INTO metric_descriptors(
                metric_name, descriptor_version, descriptor_digest, metric_type,
                unit, label_keys, histogram_bounds, series_limit, inserted_at,
                payload, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                descriptor.metric_name,
                descriptor.descriptor_version,
                descriptor.descriptor_digest,
                descriptor.metric_type,
                descriptor.unit,
                canonical_json(descriptor.label_keys),
                canonical_json(descriptor.histogram_bucket_bounds),
                descriptor.series_limit,
                inserted_at,
                payload,
                payload_hash,
            ),
        )

    def get_metric_descriptor(
        self,
        ref: MetricDescriptorRefV1,
    ) -> MetricDescriptorV1 | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload FROM metric_descriptors
                WHERE metric_name = ? AND descriptor_version = ?
                  AND descriptor_digest = ?
                """,
                metric_descriptor_key(ref),
            ).fetchone()
        if row is None:
            return None
        return self._parse_descriptor(row["payload"])

    def put(self, span: SpanDataV1) -> None:
        span = redact_span_values(span)
        payload = _wire(span)
        payload_hash = _wire_hash(payload)
        run_id = span.attributes.get("run_id")
        service = span.resource.get("service.name")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload, payload_hash FROM spans WHERE trace_id = ? AND span_id = ?",
                    (span.trace_id, span.span_id),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash or existing["payload"] != payload:
                        raise IntegrityViolation("span identity has conflicting immutable payload")
                    connection.commit()
                    return
                connection.execute(
                    """
                    INSERT INTO spans(
                        trace_id, span_id, parent_span_id, started_at, ended_at,
                        status, run_id, service, payload, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span.trace_id,
                        span.span_id,
                        span.parent_span_id,
                        _format_utc(span.started_at),
                        _format_utc(span.ended_at),
                        span.status,
                        run_id if isinstance(run_id, str) and run_id else None,
                        service if isinstance(service, str) and service else None,
                        payload,
                        payload_hash,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get(self, trace_id: str, span_id: str) -> SpanDataV1 | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM spans WHERE trace_id = ? AND span_id = ?",
                (trace_id, span_id),
            ).fetchone()
        return None if row is None else self._parse_span(row["payload"])

    def get_trace_summary(self, trace_id: str) -> TraceSummaryV1 | None:
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("trace_id must be non-empty")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM spans WHERE trace_id = ?
                ORDER BY started_at, span_id LIMIT ?
                """,
                (trace_id, self._limits.max_span_count + 1),
            ).fetchall()
        if len(rows) > self._limits.max_span_count:
            raise QueryTooBroad("trace span count exceeds service cap")
        if not rows:
            return None
        return self._summarize_trace(
            trace_id,
            tuple(self._parse_span(row["payload"]) for row in rows),
        )

    def append(self, record: LogRecordV1) -> None:
        payload = _wire(record)
        payload_hash = _wire_hash(payload)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload, payload_hash FROM logs WHERE log_id = ?",
                    (record.log_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash or existing["payload"] != payload:
                        raise IntegrityViolation("log_id has conflicting immutable payload")
                    connection.commit()
                    return
                connection.execute(
                    """
                    INSERT INTO logs(
                        log_id, ts_utc, level, service, event_name, run_id,
                        trace_id, span_id, producer_run_id, payload, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.log_id,
                        _format_utc(record.ts_utc),
                        record.level,
                        record.service,
                        record.event_name,
                        record.run_id,
                        record.trace_id,
                        record.span_id,
                        record.producer_run_id,
                        payload,
                        payload_hash,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record(self, point: MetricPointV1) -> None:
        payload = _wire(point)
        payload_hash = _wire_hash(payload)
        descriptor_key = metric_descriptor_key(point.descriptor)
        labels = _labels_key(point.labels)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT payload_hash FROM metric_point_identities
                    WHERE point_id = ?
                    """,
                    (point.point_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise IntegrityViolation("metric point_id has conflicting payload")
                    connection.commit()
                    return
                descriptor_row = connection.execute(
                    """
                    SELECT payload FROM metric_descriptors
                    WHERE metric_name = ? AND descriptor_version = ?
                      AND descriptor_digest = ?
                    """,
                    descriptor_key,
                ).fetchone()
                if descriptor_row is None:
                    raise IntegrityViolation("metric point references an unknown exact descriptor")
                descriptor = self._parse_descriptor(descriptor_row["payload"])
                if descriptor.metric_type != point.metric_type:
                    raise IntegrityViolation("metric point type differs from exact descriptor")
                if set(descriptor.label_keys) != set(point.labels):
                    raise IntegrityViolation("metric point label keys differ from exact descriptor")
                known_series = int(
                    connection.execute(
                        """
                        SELECT COUNT(DISTINCT labels) FROM metric_points
                        WHERE metric_name = ? AND descriptor_version = ?
                          AND descriptor_digest = ?
                        """,
                        descriptor_key,
                    ).fetchone()[0]
                )
                series_exists = connection.execute(
                    """
                    SELECT 1 FROM metric_points
                    WHERE metric_name = ? AND descriptor_version = ?
                      AND descriptor_digest = ? AND labels = ? LIMIT 1
                    """,
                    (*descriptor_key, labels),
                ).fetchone()
                if series_exists is None and known_series >= descriptor.series_limit:
                    raise BufferError("metric descriptor series limit exceeded")
                if series_exists is None:
                    self._validate_registry_series_limits(connection, descriptor_key)
                connection.execute(
                    """
                    INSERT INTO metric_point_identities(point_id, payload_hash)
                    VALUES (?, ?)
                    """,
                    (point.point_id, payload_hash),
                )
                connection.execute(
                    """
                    INSERT INTO metric_points(
                        point_id, metric_name, descriptor_version,
                        descriptor_digest, metric_type, ts_utc, value, labels,
                        payload, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        point.point_id,
                        *descriptor_key,
                        point.metric_type,
                        _format_utc(point.ts_utc),
                        point.value,
                        labels,
                        payload,
                        payload_hash,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _validate_registry_series_limits(
        connection: sqlite3.Connection,
        descriptor_key: tuple[str, int, str],
    ) -> None:
        memberships = connection.execute(
            """
            SELECT r.registry_version, r.registry_digest, r.global_series_limit
            FROM metric_registry_descriptors AS rd
            JOIN metric_registries AS r
              ON r.registry_version = rd.registry_version
             AND r.registry_digest = rd.registry_digest
            WHERE rd.metric_name = ? AND rd.descriptor_version = ?
              AND rd.descriptor_digest = ?
            """,
            descriptor_key,
        ).fetchall()
        for registry in memberships:
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT p.metric_name, p.descriptor_version,
                               p.descriptor_digest, p.labels
                        FROM metric_points AS p
                        JOIN metric_registry_descriptors AS rd
                          ON rd.metric_name = p.metric_name
                         AND rd.descriptor_version = p.descriptor_version
                         AND rd.descriptor_digest = p.descriptor_digest
                        WHERE rd.registry_version = ? AND rd.registry_digest = ?
                        GROUP BY p.metric_name, p.descriptor_version,
                                 p.descriptor_digest, p.labels
                    )
                    """,
                    (registry["registry_version"], registry["registry_digest"]),
                ).fetchone()[0]
            )
            if count >= int(registry["global_series_limit"]):
                raise BufferError("metric registry global series limit exceeded")

    def query_traces(
        self,
        query: TraceQueryV1,
        *,
        principal_binding: str | None = None,
        run_scope_mode: TelemetryRunScopeMode | None = None,
        allowed_run_ids: Sequence[str] = (),
    ) -> TraceSummaryPageV1:
        self._validate_time_range(query.time_range.start_utc, query.time_range.end_utc)
        self._validate_page_limit(query.limit, kind="trace")
        scope_mode, scope_run_ids = validate_telemetry_run_scope(
            run_scope_mode,
            allowed_run_ids,
        )
        if scope_mode is None:
            query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        else:
            query_hash = self._query_hash(
                {
                    "query": query.model_dump(
                        mode="json",
                        exclude={"cursor", "authz_fingerprint"},
                    ),
                    "authorized_run_scope": {
                        "mode": scope_mode,
                        "allowed_run_ids": scope_run_ids,
                    },
                }
            )
            legacy_query_hash = None
        snapshot = self._resolve_snapshot(
            kind="traces",
            query_hash=query_hash,
            authz_fingerprint=query.authz_fingerprint,
            principal_binding=principal_binding,
            page_limit=query.limit,
            cursor=query.cursor,
            legacy_query_hash=legacy_query_hash,
        )
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM spans
                WHERE seq <= ? AND started_at >= ? AND started_at < ?
                ORDER BY started_at, trace_id, span_id
                LIMIT ?
                """,
                (
                    snapshot.high_watermark,
                    _format_utc(query.time_range.start_utc),
                    _format_utc(query.time_range.end_utc),
                    self._limits.max_span_count + 1,
                ),
            ).fetchall()
        if len(rows) > self._limits.max_span_count:
            raise QueryTooBroad("trace query span count exceeds service cap")
        grouped: dict[str, list[SpanDataV1]] = defaultdict(list)
        for row in rows:
            span = self._parse_span(row["payload"])
            grouped[span.trace_id].append(span)
        summaries: list[TraceSummaryV1] = []
        for trace_id, spans in grouped.items():
            summary = self._summarize_trace(trace_id, spans)
            if query.run_id is not None and query.run_id not in summary.run_ids:
                continue
            if query.service is not None and query.service not in summary.service_names:
                continue
            if query.status is not None and query.status != summary.status:
                continue
            if scope_mode is not None and not run_ids_are_in_scope(
                summary.run_ids,
                mode=scope_mode,
                allowed_run_ids=scope_run_ids,
            ):
                continue
            summaries.append(summary)
        summaries.sort(key=lambda item: (item.started_at, item.trace_id))
        items, next_cursor = self._page_items(
            summaries,
            snapshot=snapshot,
            page_limit=query.limit,
        )
        page = TraceSummaryPageV1(
            items=tuple(items),
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return page

    def page_run_traces(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
        authz_fingerprint: str,
        principal_binding: str | None = None,
        run_scope_mode: TelemetryRunScopeMode | None = None,
        allowed_run_ids: Sequence[str] = (),
    ) -> TraceSummaryPageV1:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")
        self._validate_page_limit(limit, kind="trace")
        scope_mode, scope_run_ids = validate_telemetry_run_scope(
            run_scope_mode,
            allowed_run_ids,
        )
        if scope_mode is not None and run_id not in scope_run_ids:
            raise ValueError("Run trace scope must include the requested Run")
        query_projection: dict[str, Any] = {"run_id": run_id}
        if scope_mode is not None:
            query_projection["authorized_run_scope"] = {
                "mode": scope_mode,
                "allowed_run_ids": scope_run_ids,
            }
        query_hash = self._query_hash(query_projection)
        snapshot = self._resolve_snapshot(
            kind="run_traces",
            query_hash=query_hash,
            authz_fingerprint=authz_fingerprint,
            principal_binding=principal_binding,
            page_limit=limit,
            cursor=cursor,
        )
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM spans
                WHERE seq <= ? AND trace_id IN (
                    SELECT DISTINCT trace_id FROM spans
                    WHERE seq <= ? AND (
                        run_id = ? OR
                        json_extract(payload, '$.attributes.producer_run_id') = ?
                    )
                )
                ORDER BY started_at, trace_id, span_id
                LIMIT ?
                """,
                (
                    snapshot.high_watermark,
                    snapshot.high_watermark,
                    run_id,
                    run_id,
                    self._limits.max_span_count + 1,
                ),
            ).fetchall()
        if len(rows) > self._limits.max_span_count:
            raise QueryTooBroad("Run trace span count exceeds service cap")
        grouped: dict[str, list[SpanDataV1]] = defaultdict(list)
        for row in rows:
            span = self._parse_span(row["payload"])
            grouped[span.trace_id].append(span)
        summaries = sorted(
            (self._summarize_trace(trace_id, spans) for trace_id, spans in grouped.items()),
            key=lambda item: (item.started_at, item.trace_id),
        )
        if scope_mode is not None:
            summaries = [
                summary
                for summary in summaries
                if run_ids_are_in_scope(
                    summary.run_ids,
                    mode=scope_mode,
                    allowed_run_ids=scope_run_ids,
                )
            ]
        items, next_cursor = self._page_items(
            summaries,
            snapshot=snapshot,
            page_limit=limit,
        )
        coverage_start = min((item.started_at for item in summaries), default=self._now())
        coverage_end = max(
            (item.ended_at or item.started_at for item in summaries),
            default=coverage_start,
        )
        page = TraceSummaryPageV1(
            items=tuple(items),
            next_cursor=next_cursor,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return page

    def get_run_trace_scope(self, run_id: str) -> tuple[str, ...]:
        """Discover the complete bounded Run membership of current candidate traces."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM spans
                WHERE trace_id IN (
                    SELECT DISTINCT trace_id FROM spans
                    WHERE run_id = ? OR
                        json_extract(payload, '$.attributes.producer_run_id') = ?
                )
                ORDER BY started_at, trace_id, span_id
                LIMIT ?
                """,
                (run_id, run_id, self._limits.max_span_count + 1),
            ).fetchall()
        if len(rows) > self._limits.max_span_count:
            raise QueryTooBroad("Run trace scope span count exceeds service cap")
        grouped: dict[str, list[SpanDataV1]] = defaultdict(list)
        for row in rows:
            span = self._parse_span(row["payload"])
            grouped[span.trace_id].append(span)
        related_run_ids = {run_id}
        for trace_id, spans in grouped.items():
            related_run_ids.update(self._summarize_trace(trace_id, spans).run_ids)
        return tuple(sorted(related_run_ids))

    def page_spans(
        self,
        trace_id: str,
        *,
        cursor: str | None,
        limit: int,
        authz_fingerprint: str,
        principal_binding: str | None = None,
        run_scope_mode: TelemetryRunScopeMode | None = None,
        allowed_run_ids: Sequence[str] = (),
    ) -> SpanPageV1:
        if not trace_id:
            raise ValueError("trace_id must be non-empty")
        if not authz_fingerprint:
            raise ValueError("authz_fingerprint must be non-empty")
        self._validate_page_limit(limit, kind="span")
        scope_mode, scope_run_ids = validate_telemetry_run_scope(
            run_scope_mode,
            allowed_run_ids,
        )
        query_projection: dict[str, Any] = {"trace_id": trace_id}
        if scope_mode is not None:
            query_projection["authorized_run_scope"] = {
                "mode": scope_mode,
                "allowed_run_ids": scope_run_ids,
            }
        query_hash = self._query_hash(query_projection)
        snapshot = self._resolve_snapshot(
            kind="spans",
            query_hash=query_hash,
            authz_fingerprint=authz_fingerprint,
            principal_binding=principal_binding,
            page_limit=limit,
            cursor=cursor,
        )
        with self._connection() as connection:
            if scope_mode is None:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM spans WHERE seq <= ? AND trace_id = ?",
                        (snapshot.high_watermark, trace_id),
                    ).fetchone()[0]
                )
            else:
                scope_rows = connection.execute(
                    """
                    SELECT payload FROM spans
                    WHERE seq <= ? AND trace_id = ?
                    ORDER BY started_at, span_id LIMIT ?
                    """,
                    (snapshot.high_watermark, trace_id, self._limits.max_span_count + 1),
                ).fetchall()
                count = len(scope_rows)
                trace_run_ids = tuple(
                    sorted(
                        {
                            run_id
                            for row in scope_rows
                            for run_id in span_run_ids(self._parse_span(row["payload"]))
                        }
                    )
                )
                if not run_ids_are_in_scope(
                    trace_run_ids,
                    mode=scope_mode,
                    allowed_run_ids=scope_run_ids,
                ):
                    raise IntegrityViolation("trace Run scope changed before span pagination")
            if count > self._limits.max_span_count:
                raise QueryTooBroad("trace span count exceeds service cap")
            rows = connection.execute(
                """
                SELECT payload FROM spans
                WHERE seq <= ? AND trace_id = ?
                ORDER BY started_at, span_id
                LIMIT ? OFFSET ?
                """,
                (
                    snapshot.high_watermark,
                    trace_id,
                    limit + 1,
                    snapshot.offset,
                ),
            ).fetchall()
        views = tuple(SpanViewV1(span=self._parse_span(row["payload"])) for row in rows[:limit])
        next_cursor = self._issue_next_cursor(
            snapshot,
            next_offset=snapshot.offset + len(views),
            page_limit=limit,
            has_more=len(rows) > limit,
        )
        page = SpanPageV1(
            trace_id=trace_id,
            items=views,
            next_cursor=next_cursor,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return page

    def query_logs(
        self,
        query: LogQueryV1,
        *,
        principal_binding: str | None = None,
        run_scope_mode: TelemetryRunScopeMode | None = None,
        allowed_run_ids: Sequence[str] = (),
    ) -> LogPageV1:
        return self._query_logs(
            query,
            principal_binding=principal_binding,
            run_scope_mode=run_scope_mode,
            allowed_run_ids=allowed_run_ids,
            require_trace_scope_proof=False,
        ).page

    def query_logs_with_scope(
        self,
        query: LogQueryV1,
        *,
        principal_binding: str,
        run_scope_mode: TelemetryRunScopeMode,
        allowed_run_ids: Sequence[str] = (),
    ) -> RetainedLogPage:
        return self._query_logs(
            query,
            principal_binding=principal_binding,
            run_scope_mode=run_scope_mode,
            allowed_run_ids=allowed_run_ids,
            require_trace_scope_proof=True,
        )

    def _query_logs(
        self,
        query: LogQueryV1,
        *,
        principal_binding: str | None = None,
        run_scope_mode: TelemetryRunScopeMode | None = None,
        allowed_run_ids: Sequence[str] = (),
        require_trace_scope_proof: bool,
    ) -> RetainedLogPage:
        self._validate_time_range(query.time_range.start_utc, query.time_range.end_utc)
        self._validate_page_limit(query.limit, kind="log")
        scope_mode, scope_run_ids = validate_telemetry_run_scope(
            run_scope_mode,
            allowed_run_ids,
        )
        if scope_mode is None:
            query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        else:
            query_hash = self._query_hash(
                {
                    "query": query.model_dump(
                        mode="json",
                        exclude={"cursor", "authz_fingerprint"},
                    ),
                    "authorized_log_scope": {
                        "mode": scope_mode,
                        "allowed_run_ids": scope_run_ids,
                    },
                }
            )
            legacy_query_hash = None
        snapshot = self._resolve_snapshot(
            kind="logs",
            query_hash=query_hash,
            authz_fingerprint=query.authz_fingerprint,
            principal_binding=principal_binding,
            page_limit=query.limit,
            cursor=query.cursor,
            legacy_query_hash=legacy_query_hash,
        )
        clauses = ["seq <= ?", "ts_utc >= ?", "ts_utc < ?"]
        parameters: list[object] = [
            snapshot.high_watermark,
            _format_utc(query.time_range.start_utc),
            _format_utc(query.time_range.end_utc),
        ]
        self._append_in_filter(clauses, parameters, "service", query.services)
        self._append_in_filter(clauses, parameters, "level", query.levels)
        self._append_in_filter(clauses, parameters, "event_name", query.event_names)
        if scope_mode == "domainless_only":
            clauses.extend(("run_id IS NULL", "producer_run_id IS NULL"))
            clauses.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM spans AS scoped_span
                    WHERE scoped_span.seq <= ?
                      AND scoped_span.trace_id = logs.trace_id
                      AND (
                          scoped_span.run_id IS NOT NULL OR
                          json_extract(
                              scoped_span.payload,
                              '$.attributes.producer_run_id'
                          ) IS NOT NULL
                      )
                )
                """
            )
            parameters.append(snapshot.secondary_high_watermark)
        elif scope_mode == "run_allowlist":
            placeholders = ",".join("?" for _ in scope_run_ids)
            clauses.extend(
                (
                    f"(run_id IS NULL OR run_id IN ({placeholders}))",
                    f"(producer_run_id IS NULL OR producer_run_id IN ({placeholders}))",
                )
            )
            parameters.extend(scope_run_ids)
            parameters.extend(scope_run_ids)
            clauses.append(
                f"""
                NOT EXISTS (
                    SELECT 1 FROM spans AS scoped_span
                    WHERE scoped_span.seq <= ?
                      AND scoped_span.trace_id = logs.trace_id
                      AND (
                          (
                              scoped_span.run_id IS NOT NULL AND
                              scoped_span.run_id NOT IN ({placeholders})
                          ) OR (
                              json_extract(
                                  scoped_span.payload,
                                  '$.attributes.producer_run_id'
                              ) IS NOT NULL AND
                              json_extract(
                                  scoped_span.payload,
                                  '$.attributes.producer_run_id'
                              ) NOT IN ({placeholders})
                          )
                      )
                )
                """
            )
            parameters.append(snapshot.secondary_high_watermark)
            parameters.extend(scope_run_ids)
            parameters.extend(scope_run_ids)
        if scope_mode is not None:
            clauses.append(
                """
                (
                    trace_id IS NULL OR EXISTS (
                        SELECT 1 FROM spans AS retained_trace_span
                        WHERE retained_trace_span.seq <= ?
                          AND retained_trace_span.trace_id = logs.trace_id
                    )
                )
                """
            )
            parameters.append(snapshot.secondary_high_watermark)
        if query.run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(query.run_id)
        if query.trace_id is not None:
            clauses.append("trace_id = ?")
            parameters.append(query.trace_id)
        if query.span_id is not None:
            clauses.append("span_id = ?")
            parameters.append(query.span_id)
        if query.producer_run_id is not None:
            clauses.append("producer_run_id = ?")
            parameters.append(query.producer_run_id)
        parameters.extend((query.limit + 1, snapshot.offset))
        statement = (
            "SELECT payload FROM logs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts_utc, log_id LIMIT ? OFFSET ?"
        )
        with self._connection() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        items = tuple(self._parse_log(row["payload"]) for row in rows[: query.limit])
        next_cursor = self._issue_next_cursor(
            snapshot,
            next_offset=snapshot.offset + len(items),
            page_limit=query.limit,
            has_more=len(rows) > query.limit,
        )
        page = LogPageV1(
            items=items,
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        trace_scopes: list[RetainedTraceRunScope] = []
        if require_trace_scope_proof:
            for trace_id in sorted({item.trace_id for item in items if item.trace_id is not None}):
                trace_scopes.append(
                    RetainedTraceRunScope(
                        trace_id=trace_id,
                        run_ids=self._retained_trace_run_scope(
                            trace_id,
                            high_watermark=snapshot.secondary_high_watermark,
                        ),
                    )
                )
        return RetainedLogPage(page=page, trace_scopes=tuple(trace_scopes))

    def _retained_trace_run_scope(
        self,
        trace_id: str,
        *,
        high_watermark: int,
    ) -> tuple[str, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM spans
                WHERE seq <= ? AND trace_id = ?
                ORDER BY started_at, span_id LIMIT ?
                """,
                (high_watermark, trace_id, self._limits.max_span_count + 1),
            ).fetchall()
        if not rows:
            raise IntegrityViolation("retained log trace scope is unavailable")
        if len(rows) > self._limits.max_span_count:
            raise QueryTooBroad("retained log trace exceeds the span count cap")
        return tuple(
            sorted(
                {
                    run_id
                    for row in rows
                    for run_id in span_run_ids(self._parse_span(row["payload"]))
                }
            )
        )

    @staticmethod
    def _append_in_filter(
        clauses: list[str],
        parameters: list[object],
        column: str,
        values: Sequence[str],
    ) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"{column} IN ({placeholders})")
        parameters.extend(values)

    def query_metrics(
        self,
        query: MetricQueryV1,
        *,
        principal_binding: str | None = None,
    ) -> MetricPageV1:
        self._validate_time_range(query.time_range.start_utc, query.time_range.end_utc)
        if query.series_limit > self._limits.max_series:
            raise QueryTooBroad("metric series limit exceeds service cap")
        if query.max_points > self._limits.max_points:
            raise QueryTooBroad("metric max_points exceeds service cap")
        if query.resolution_s > self._limits.max_resolution_s:
            raise QueryTooBroad("metric resolution exceeds service cap")
        descriptors = self._load_descriptors(query.descriptor_refs)
        validate_metric_query_shape(query, descriptors)
        query_hash, legacy_query_hash = self._query_hashes(query, principal_binding)
        authz = query.authz_fingerprint
        snapshot = self._resolve_snapshot(
            kind="metrics",
            query_hash=query_hash,
            authz_fingerprint=authz,
            principal_binding=principal_binding,
            page_limit=query.series_limit,
            cursor=query.cursor,
            descriptor_refs=query.descriptor_refs,
            legacy_query_hash=legacy_query_hash,
        )
        points = self._load_metric_points(query, snapshot.high_watermark)
        series = aggregate_metric_points(points, descriptors, query)
        if snapshot.offset > len(series):
            raise IntegrityViolation("metric cursor offset exceeds its retained result")
        selected: list[MetricSeriesV1] = []
        point_count = 0
        index = snapshot.offset
        while index < len(series) and len(selected) < query.series_limit:
            candidate = series[index]
            candidate_points = self._series_point_count(candidate)
            if candidate_points > self._limits.max_points_per_series:
                raise QueryTooBroad("one metric series exceeds service point cap")
            if candidate_points > query.max_points:
                raise QueryTooBroad("one metric series exceeds max_points")
            if selected and point_count + candidate_points > query.max_points:
                break
            selected.append(candidate)
            point_count += candidate_points
            index += 1
        next_cursor = self._issue_next_cursor(
            snapshot,
            next_offset=index,
            page_limit=query.series_limit,
            has_more=index < len(series),
        )
        page = MetricPageV1(
            series=tuple(selected),
            next_cursor=next_cursor,
            coverage_start=query.time_range.start_utc,
            coverage_end=query.time_range.end_utc,
            effective_resolution_s=query.resolution_s,
            truncated=next_cursor is not None,
        )
        self._check_response_size(page)
        return page

    def query(self, query: MetricQueryV1) -> MetricPageV1:
        """Backward-compatible alias; new consumers use the typed method name."""

        return self.query_metrics(query)

    def _load_descriptors(
        self,
        refs: Sequence[MetricDescriptorRefV1],
    ) -> dict[tuple[str, int, str], MetricDescriptorV1]:
        result: dict[tuple[str, int, str], MetricDescriptorV1] = {}
        with self._connection() as connection:
            for ref in refs:
                row = connection.execute(
                    """
                    SELECT payload FROM metric_descriptors
                    WHERE metric_name = ? AND descriptor_version = ?
                      AND descriptor_digest = ?
                    """,
                    metric_descriptor_key(ref),
                ).fetchone()
                if row is None:
                    raise IntegrityViolation("metric query references an unknown descriptor")
                descriptor = self._parse_descriptor(row["payload"])
                result[metric_descriptor_key(descriptor.ref)] = descriptor
        return result

    def resolve_metric_descriptors(
        self,
        refs: Sequence[MetricDescriptorRefV1],
    ) -> tuple[MetricDescriptorV1, ...]:
        descriptors = self._load_descriptors(refs)
        return tuple(descriptors[metric_descriptor_key(ref)] for ref in refs)

    def _load_metric_points(
        self,
        query: MetricQueryV1,
        high_watermark: int,
    ) -> tuple[MetricPointV1, ...]:
        ref_clauses: list[str] = []
        parameters: list[object] = [
            high_watermark,
            _format_utc(query.time_range.start_utc),
            _format_utc(query.time_range.end_utc),
        ]
        for ref in query.descriptor_refs:
            ref_clauses.append(
                "(metric_name = ? AND descriptor_version = ? AND descriptor_digest = ?)"
            )
            parameters.extend(metric_descriptor_key(ref))
        parameters.append(self._limits.max_raw_metric_points + 1)
        statement = (
            "SELECT payload FROM metric_points "
            "WHERE seq <= ? AND ts_utc >= ? AND ts_utc < ? AND ("
            + " OR ".join(ref_clauses)
            + ") ORDER BY ts_utc, point_id LIMIT ?"
        )
        with self._connection() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        if len(rows) > self._limits.max_raw_metric_points:
            raise QueryTooBroad("metric raw point count exceeds service cap")
        return tuple(self._parse_point(row["payload"]) for row in rows)

    @staticmethod
    def _series_point_count(series: MetricSeriesV1) -> int:
        return len(series.scalar_points or ()) + len(series.histogram_points or ())

    def _validate_time_range(self, start: datetime, end: datetime) -> None:
        if end - start > self._limits.max_time_range:
            raise QueryTooBroad("telemetry time range exceeds service cap")

    def _validate_page_limit(self, value: int, *, kind: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > self._limits.max_page_size
        ):
            raise QueryTooBroad(f"{kind} page limit exceeds service cap")

    def _query_hash(self, query_projection: Mapping[str, Any]) -> str:
        return canonical_sha256(
            {
                "local_telemetry_query_schema_version": "local-telemetry-query@1",
                "query": dict(query_projection),
                "retention_fingerprint": self._retention.fingerprint,
            }
        )

    def _query_hashes(
        self,
        query: Any,
        principal_binding: str | None,
    ) -> tuple[str, str | None]:
        legacy = self._query_hash(query.model_dump(mode="json", exclude={"cursor"}))
        if principal_binding is None:
            return legacy, None
        current = self._query_hash(
            query.model_dump(
                mode="json",
                exclude={"cursor", "authz_fingerprint"},
            )
        )
        return current, legacy

    def _resolve_snapshot(
        self,
        *,
        kind: str,
        query_hash: str,
        authz_fingerprint: str,
        principal_binding: str | None,
        page_limit: int,
        cursor: str | None,
        descriptor_refs: Sequence[MetricDescriptorRefV1] = (),
        legacy_query_hash: str | None = None,
    ) -> _ReadSnapshot:
        if kind not in _READ_KINDS:
            raise ValueError("unknown telemetry read snapshot kind")
        if cursor is None:
            return self._create_snapshot(
                kind=kind,
                query_hash=query_hash,
                authz_fingerprint=authz_fingerprint,
                principal_binding=principal_binding,
                descriptor_refs=descriptor_refs,
            )
        state = self._cursor_codec.verify(
            cursor,
            expected_kind=kind,
            expected_query_hash=query_hash,
            expected_authz_fingerprint=authz_fingerprint,
            expected_page_limit=page_limit,
            expected_principal_binding=principal_binding,
            expected_legacy_query_hash=legacy_query_hash,
        )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM read_snapshots WHERE snapshot_id = ?",
                (state.snapshot_id,),
            ).fetchone()
        if row is None:
            raise CursorExpired("telemetry query snapshot is no longer retained")
        snapshot = self._snapshot_from_row(
            row,
            offset=state.offset,
            principal_binding=state.principal_binding,
        )
        if self._now() >= snapshot.expires_at:
            raise CursorExpired("telemetry query snapshot has expired")
        if (
            snapshot.kind != kind
            or snapshot.query_hash != state.query_hash
            or snapshot.authz_fingerprint != state.authz_fingerprint
            or snapshot.retention_fingerprint != self._retention.fingerprint
            or snapshot.expires_at != state.expires_at
        ):
            raise IntegrityViolation("telemetry read snapshot binding is inconsistent")
        if descriptor_refs:
            retained = self._snapshot_descriptor_refs(snapshot.snapshot_id)
            if retained != tuple(sorted(metric_descriptor_key(ref) for ref in descriptor_refs)):
                raise IntegrityViolation("metric read snapshot descriptor binding is inconsistent")
        return snapshot

    def _create_snapshot(
        self,
        *,
        kind: str,
        query_hash: str,
        authz_fingerprint: str,
        principal_binding: str | None,
        descriptor_refs: Sequence[MetricDescriptorRefV1],
    ) -> _ReadSnapshot:
        now = self._now()
        expires_at = now + self._retention.read_snapshot_ttl
        table = "spans" if kind in {"traces", "run_traces", "spans"} else kind
        if table == "metrics":
            table = "metric_points"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM read_snapshots WHERE expires_at <= ?",
                    (_format_utc(now),),
                )
                high_watermark = int(
                    connection.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {table}").fetchone()[0]
                )
                secondary_high_watermark = (
                    int(connection.execute("SELECT COALESCE(MAX(seq), 0) FROM spans").fetchone()[0])
                    if kind == "logs"
                    else 0
                )
                snapshot_id = f"telemetry-snapshot:{secrets.token_hex(16)}"
                connection.execute(
                    """
                    INSERT INTO read_snapshots(
                        snapshot_id, kind, query_hash, authz_fingerprint,
                        high_watermark, secondary_high_watermark, created_at,
                        expires_at, retention_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        kind,
                        query_hash,
                        authz_fingerprint,
                        high_watermark,
                        secondary_high_watermark,
                        _format_utc(now),
                        _format_utc(expires_at),
                        self._retention.fingerprint,
                    ),
                )
                for ref in descriptor_refs:
                    connection.execute(
                        """
                        INSERT INTO read_snapshot_descriptors(
                            snapshot_id, metric_name, descriptor_version,
                            descriptor_digest
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (snapshot_id, *metric_descriptor_key(ref)),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _ReadSnapshot(
            snapshot_id=snapshot_id,
            kind=kind,
            query_hash=query_hash,
            authz_fingerprint=authz_fingerprint,
            principal_binding=principal_binding,
            high_watermark=high_watermark,
            secondary_high_watermark=secondary_high_watermark,
            expires_at=expires_at,
            retention_fingerprint=self._retention.fingerprint,
            offset=0,
        )

    @staticmethod
    def _snapshot_from_row(
        row: sqlite3.Row,
        *,
        offset: int,
        principal_binding: str | None,
    ) -> _ReadSnapshot:
        return _ReadSnapshot(
            snapshot_id=row["snapshot_id"],
            kind=row["kind"],
            query_hash=row["query_hash"],
            authz_fingerprint=row["authz_fingerprint"],
            principal_binding=principal_binding,
            high_watermark=int(row["high_watermark"]),
            secondary_high_watermark=int(row["secondary_high_watermark"]),
            expires_at=_parse_utc(row["expires_at"]),
            retention_fingerprint=row["retention_fingerprint"],
            offset=offset,
        )

    def _snapshot_descriptor_refs(
        self,
        snapshot_id: str,
    ) -> tuple[tuple[str, int, str], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT metric_name, descriptor_version, descriptor_digest
                FROM read_snapshot_descriptors WHERE snapshot_id = ?
                ORDER BY metric_name, descriptor_version, descriptor_digest
                """,
                (snapshot_id,),
            ).fetchall()
        return tuple(
            (row["metric_name"], int(row["descriptor_version"]), row["descriptor_digest"])
            for row in rows
        )

    def _page_items(
        self,
        items: Sequence[Any],
        *,
        snapshot: _ReadSnapshot,
        page_limit: int,
    ) -> tuple[Sequence[Any], str | None]:
        if snapshot.offset > len(items):
            raise IntegrityViolation("telemetry cursor offset exceeds its retained result")
        page = items[snapshot.offset : snapshot.offset + page_limit]
        next_offset = snapshot.offset + len(page)
        return page, self._issue_next_cursor(
            snapshot,
            next_offset=next_offset,
            page_limit=page_limit,
            has_more=next_offset < len(items),
        )

    def _issue_next_cursor(
        self,
        snapshot: _ReadSnapshot,
        *,
        next_offset: int,
        page_limit: int,
        has_more: bool,
    ) -> str | None:
        if not has_more:
            return None
        return self._cursor_codec.issue(
            kind=snapshot.kind,
            snapshot_id=snapshot.snapshot_id,
            query_hash=snapshot.query_hash,
            authz_fingerprint=snapshot.authz_fingerprint,
            principal_binding=snapshot.principal_binding,
            offset=next_offset,
            page_limit=page_limit,
            expires_at=snapshot.expires_at,
        )

    def _check_response_size(self, value: Any) -> None:
        if len(value.model_dump_json().encode("utf-8")) > self._limits.max_response_bytes:
            raise QueryTooBroad("telemetry response exceeds service byte cap")

    @staticmethod
    def _summarize_trace(trace_id: str, spans: Sequence[SpanDataV1]) -> TraceSummaryV1:
        ordered = sorted(spans, key=lambda item: (item.started_at, item.span_id))
        roots = [item for item in ordered if item.parent_span_id is None]
        root = roots[0] if len(roots) == 1 else None
        run_ids = tuple(sorted({run_id for span in ordered for run_id in span_run_ids(span)}))
        services = tuple(
            sorted(
                {
                    value
                    for span in ordered
                    if isinstance((value := span.resource.get("service.name")), str) and value
                }
            )
        )
        status = (
            "error"
            if any(item.status == "error" for item in ordered)
            else "ok"
            if all(item.status == "ok" for item in ordered)
            else "unset"
        )
        return TraceSummaryV1(
            trace_id=trace_id,
            root_span_id=root.span_id if root else None,
            run_ids=run_ids,
            started_at=min(item.started_at for item in ordered),
            ended_at=max(item.ended_at for item in ordered),
            duration_ns=root.duration_ns if root else None,
            status=status,
            span_count=len(ordered),
            service_names=services,
            truncated=False,
        )

    def retain_metric_descriptors(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        descriptor_refs: Sequence[MetricDescriptorRefV1],
        expires_at: datetime | None = None,
    ) -> None:
        if owner_kind not in _PIN_OWNER_KINDS:
            raise ValueError("metric descriptor pin owner kind is unsupported")
        if not owner_id:
            raise ValueError("metric descriptor pin owner_id must be non-empty")
        keys = tuple(sorted(metric_descriptor_key(ref) for ref in descriptor_refs))
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("metric descriptor pins must be non-empty and unique")
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() != UTC.utcoffset(expires_at):
                raise ValueError("metric descriptor pin expiry must be UTC")
            if expires_at <= self._now():
                raise ValueError("metric descriptor pin expiry must be in the future")
        expiry = None if expires_at is None else _format_utc(expires_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for key in keys:
                    if (
                        connection.execute(
                            """
                        SELECT 1 FROM metric_descriptors
                        WHERE metric_name = ? AND descriptor_version = ?
                          AND descriptor_digest = ?
                        """,
                            key,
                        ).fetchone()
                        is None
                    ):
                        raise IntegrityViolation(
                            "metric descriptor pin references an unknown descriptor"
                        )
                    connection.execute(
                        """
                        INSERT INTO metric_descriptor_pins(
                            owner_kind, owner_id, metric_name, descriptor_version,
                            descriptor_digest, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(
                            owner_kind, owner_id, metric_name, descriptor_version,
                            descriptor_digest
                        ) DO UPDATE SET expires_at = excluded.expires_at
                        """,
                        (owner_kind, owner_id, *key, expiry),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def release_metric_descriptors(self, *, owner_kind: str, owner_id: str) -> int:
        if owner_kind not in _PIN_OWNER_KINDS:
            raise ValueError("metric descriptor pin owner kind is unsupported")
        if not owner_id:
            raise ValueError("metric descriptor pin owner_id must be non-empty")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    """
                    DELETE FROM metric_descriptor_pins
                    WHERE owner_kind = ? AND owner_id = ?
                    """,
                    (owner_kind, owner_id),
                )
                connection.commit()
                return result.rowcount
            except Exception:
                connection.rollback()
                raise

    def purge_expired(self) -> LocalTelemetryRetentionResult:
        now = self._now()
        now_text = _format_utc(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                snapshots = connection.execute(
                    "DELETE FROM read_snapshots WHERE expires_at <= ?",
                    (now_text,),
                ).rowcount
                pins = connection.execute(
                    """
                    DELETE FROM metric_descriptor_pins
                    WHERE expires_at IS NOT NULL AND expires_at <= ?
                    """,
                    (now_text,),
                ).rowcount

                active_kinds = {
                    row["kind"]
                    for row in connection.execute(
                        "SELECT DISTINCT kind FROM read_snapshots WHERE expires_at > ?",
                        (now_text,),
                    ).fetchall()
                }
                deleted_spans = 0
                if not active_kinds.intersection({"traces", "run_traces", "spans", "logs"}):
                    deleted_spans = connection.execute(
                        "DELETE FROM spans WHERE ended_at < ?",
                        (_format_utc(now - self._retention.spans),),
                    ).rowcount
                deleted_logs = 0
                if "logs" not in active_kinds:
                    deleted_logs = connection.execute(
                        "DELETE FROM logs WHERE ts_utc < ?",
                        (_format_utc(now - self._retention.logs),),
                    ).rowcount
                deleted_points = 0
                if "metrics" not in active_kinds:
                    deleted_points = connection.execute(
                        "DELETE FROM metric_points WHERE ts_utc < ?",
                        (_format_utc(now - self._retention.metric_points),),
                    ).rowcount

                descriptor_cutoff = _format_utc(now - self._retention.metric_descriptors)
                active_registry = self._active_registry_key(connection)
                deleted_registries = 0
                candidates = connection.execute(
                    """
                    SELECT registry_version, registry_digest
                    FROM metric_registries WHERE inserted_at < ?
                    ORDER BY registry_version, registry_digest
                    """,
                    (descriptor_cutoff,),
                ).fetchall()
                for candidate in candidates:
                    key = (int(candidate["registry_version"]), candidate["registry_digest"])
                    if key == active_registry:
                        continue
                    if self._registry_is_referenced(connection, key):
                        continue
                    connection.execute(
                        """
                        DELETE FROM metric_registries
                        WHERE registry_version = ? AND registry_digest = ?
                        """,
                        key,
                    )
                    deleted_registries += 1

                descriptor_candidates = connection.execute(
                    """
                    SELECT metric_name, descriptor_version, descriptor_digest
                    FROM metric_descriptors AS d
                    WHERE inserted_at < ?
                      AND NOT EXISTS (
                        SELECT 1 FROM metric_registry_descriptors AS rd
                        WHERE rd.metric_name = d.metric_name
                          AND rd.descriptor_version = d.descriptor_version
                          AND rd.descriptor_digest = d.descriptor_digest
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM metric_points AS p
                        WHERE p.metric_name = d.metric_name
                          AND p.descriptor_version = d.descriptor_version
                          AND p.descriptor_digest = d.descriptor_digest
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM metric_descriptor_pins AS pin
                        WHERE pin.metric_name = d.metric_name
                          AND pin.descriptor_version = d.descriptor_version
                          AND pin.descriptor_digest = d.descriptor_digest
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM read_snapshot_descriptors AS sd
                        WHERE sd.metric_name = d.metric_name
                          AND sd.descriptor_version = d.descriptor_version
                          AND sd.descriptor_digest = d.descriptor_digest
                      )
                    """,
                    (descriptor_cutoff,),
                ).fetchall()
                deleted_descriptors = 0
                for candidate in descriptor_candidates:
                    connection.execute(
                        """
                        DELETE FROM metric_descriptors
                        WHERE metric_name = ? AND descriptor_version = ?
                          AND descriptor_digest = ?
                        """,
                        (
                            candidate["metric_name"],
                            candidate["descriptor_version"],
                            candidate["descriptor_digest"],
                        ),
                    )
                    deleted_descriptors += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return LocalTelemetryRetentionResult(
            deleted_spans=deleted_spans,
            deleted_logs=deleted_logs,
            deleted_metric_points=deleted_points,
            deleted_metric_descriptors=deleted_descriptors,
            deleted_metric_registries=deleted_registries,
            deleted_read_snapshots=snapshots,
            deleted_expired_pins=pins,
        )

    @staticmethod
    def _active_registry_key(
        connection: sqlite3.Connection,
    ) -> tuple[int, str] | None:
        row = connection.execute(
            "SELECT value FROM telemetry_meta WHERE key = 'active_metric_registry'"
        ).fetchone()
        if row is None:
            return None
        try:
            ref = MetricDescriptorRegistryRefV1.model_validate_json(row["value"])
        except ValueError as exc:
            raise IntegrityViolation("active metric registry binding is invalid") from exc
        return (ref.registry_version, ref.registry_digest)

    @staticmethod
    def _registry_is_referenced(
        connection: sqlite3.Connection,
        key: tuple[int, str],
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM metric_registry_descriptors AS rd
            WHERE rd.registry_version = ? AND rd.registry_digest = ?
              AND (
                EXISTS (
                    SELECT 1 FROM metric_points AS p
                    WHERE p.metric_name = rd.metric_name
                      AND p.descriptor_version = rd.descriptor_version
                      AND p.descriptor_digest = rd.descriptor_digest
                )
                OR EXISTS (
                    SELECT 1 FROM metric_descriptor_pins AS pin
                    WHERE pin.metric_name = rd.metric_name
                      AND pin.descriptor_version = rd.descriptor_version
                      AND pin.descriptor_digest = rd.descriptor_digest
                )
                OR EXISTS (
                    SELECT 1 FROM read_snapshot_descriptors AS sd
                    WHERE sd.metric_name = rd.metric_name
                      AND sd.descriptor_version = rd.descriptor_version
                      AND sd.descriptor_digest = rd.descriptor_digest
                )
              )
            LIMIT 1
            """,
            key,
        ).fetchone()
        return row is not None

    @staticmethod
    def _parse_span(payload: str) -> SpanDataV1:
        try:
            return redact_span_values(SpanDataV1.model_validate_json(payload))
        except ValueError as exc:
            raise IntegrityViolation("persisted span payload is invalid") from exc

    @staticmethod
    def _parse_log(payload: str) -> LogRecordV1:
        try:
            return LogRecordV1.model_validate_json(payload)
        except ValueError as exc:
            raise IntegrityViolation("persisted log payload is invalid") from exc

    @staticmethod
    def _parse_point(payload: str) -> MetricPointV1:
        try:
            return MetricPointV1.model_validate_json(payload)
        except ValueError as exc:
            raise IntegrityViolation("persisted metric point payload is invalid") from exc

    @staticmethod
    def _parse_descriptor(payload: str) -> MetricDescriptorV1:
        try:
            return MetricDescriptorV1.model_validate_json(payload)
        except ValueError as exc:
            raise IntegrityViolation("persisted metric descriptor payload is invalid") from exc


__all__ = [
    "LocalTelemetryLimits",
    "LocalTelemetryRetention",
    "LocalTelemetryRetentionResult",
    "LocalTelemetryStore",
]
