"""Prometheus v0.0.4 运行指标注册表：HTTP / 问数 / LLM / 数据源查询 / Job 与运行时池状态。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_PROCESS_STARTED = time.monotonic()

DEFAULT_HTTP_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
ASK_DURATION_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
LLM_DURATION_BUCKETS: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
DS_QUERY_DURATION_BUCKETS: tuple[float, ...] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
JOB_DURATION_BUCKETS: tuple[float, ...] = (0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0)


def _escape_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_prom_label(v)}"' for k, v in pairs)
    return f"{{{inner}}}"


def _observe_histogram(
    buckets: tuple[float, ...],
    bucket_store: dict[tuple[str, ...], list[int]],
    sum_store: dict[tuple[str, ...], float],
    count_store: dict[tuple[str, ...], int],
    key: tuple[str, ...],
    value: float,
) -> None:
    dur = max(0.0, float(value))
    counts = bucket_store.get(key)
    if counts is None:
        counts = [0] * len(buckets)
        bucket_store[key] = counts
    for idx, bound in enumerate(buckets):
        if dur <= bound:
            counts[idx] += 1
    sum_store[key] += dur
    count_store[key] += 1


def _render_histogram(
    lines: list[str],
    name: str,
    help_text: str,
    label_names: tuple[str, ...],
    buckets: tuple[float, ...],
    bucket_store: dict[tuple[str, ...], list[int]],
    sum_store: dict[tuple[str, ...], float],
    count_store: dict[tuple[str, ...], int],
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} histogram"])
    for key, counts in sorted(bucket_store.items()):
        base_pairs = tuple(zip(label_names, key, strict=True))
        for bound, count in zip(buckets, counts, strict=True):
            lbl = _format_labels((("le", f"{bound:g}"), *base_pairs))
            lines.append(f"{name}_bucket{lbl} {count}")
        inf_lbl = _format_labels((("le", "+Inf"), *base_pairs))
        base_lbl = _format_labels(base_pairs)
        lines.append(f"{name}_bucket{inf_lbl} {count_store[key]}")
        lines.append(f"{name}_sum{base_lbl} {sum_store[key]:.6f}")
        lines.append(f"{name}_count{base_lbl} {count_store[key]}")


def _db_pool_snapshot() -> dict[str, int]:
    try:
        from avelis.db import get_engine

        pool = get_engine().pool
        out: dict[str, int] = {}
        for state, fn_name in (
            ("size", "size"),
            ("checked_in", "checkedin"),
            ("checked_out", "checkedout"),
            ("overflow", "overflow"),
        ):
            fn = getattr(pool, fn_name, None)
            if callable(fn):
                out[state] = int(fn())
        return out
    except Exception:  # noqa: BLE001 - 未初始化引擎或 SQLite NullPool 时忽略
        return {}


def _build_metadata() -> tuple[str, str]:
    try:
        from avelis.api.app import API_VERSION
    except Exception:  # noqa: BLE001
        API_VERSION = "unknown"
    try:
        from avelis.config import get_settings

        env_name = get_settings().environment
    except Exception:  # noqa: BLE001
        env_name = "unknown"
    return API_VERSION, env_name


class MetricsRegistry:
    """线程安全的生产级 Prometheus v0.0.4 运行指标注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._http_in_flight = 0
            self._http_requests: dict[tuple[str, str, str], int] = defaultdict(int)
            self._http_dur_buckets: dict[tuple[str, str], list[int]] = {}
            self._http_dur_sum: dict[tuple[str, str], float] = defaultdict(float)
            self._http_dur_count: dict[tuple[str, str], int] = defaultdict(int)

            self._ask_requests: dict[tuple[str], int] = defaultdict(int)
            self._ask_dur_buckets: dict[tuple[str], list[int]] = {}
            self._ask_dur_sum: dict[tuple[str], float] = defaultdict(float)
            self._ask_dur_count: dict[tuple[str], int] = defaultdict(int)

            self._llm_calls: dict[tuple[str, str], int] = defaultdict(int)
            self._llm_dur_buckets: dict[tuple[str], list[int]] = {}
            self._llm_dur_sum: dict[tuple[str], float] = defaultdict(float)
            self._llm_dur_count: dict[tuple[str], int] = defaultdict(int)
            self._llm_tokens: dict[tuple[str, str], int] = defaultdict(int)

            self._ds_queries: dict[tuple[str, str], int] = defaultdict(int)
            self._ds_dur_buckets: dict[tuple[str], list[int]] = {}
            self._ds_dur_sum: dict[tuple[str], float] = defaultdict(float)
            self._ds_dur_count: dict[tuple[str], int] = defaultdict(int)

            self._job_runs: dict[tuple[str, str], int] = defaultdict(int)
            self._job_dur_buckets: dict[tuple[str], list[int]] = {}
            self._job_dur_sum: dict[tuple[str], float] = defaultdict(float)
            self._job_dur_count: dict[tuple[str], int] = defaultdict(int)

    def http_enter(self) -> None:
        with self._lock:
            self._http_in_flight += 1

    def http_exit(self) -> None:
        with self._lock:
            self._http_in_flight = max(0, self._http_in_flight - 1)

    def inc_http(self, method: str, path: str, status: int, duration_seconds: float) -> None:
        method_norm = (method or "GET").upper()
        path_norm = path or "unmatched"
        with self._lock:
            self._http_requests[(method_norm, path_norm, str(status))] += 1
            _observe_histogram(
                DEFAULT_HTTP_BUCKETS,
                self._http_dur_buckets,
                self._http_dur_sum,
                self._http_dur_count,
                (method_norm, path_norm),
                duration_seconds,
            )

    def observe_ask(self, status: str, duration_seconds: float | None = None) -> None:
        st = status or "unknown"
        with self._lock:
            self._ask_requests[(st,)] += 1
            if duration_seconds is not None:
                _observe_histogram(
                    ASK_DURATION_BUCKETS,
                    self._ask_dur_buckets,
                    self._ask_dur_sum,
                    self._ask_dur_count,
                    (st,),
                    duration_seconds,
                )

    def inc_ask(self, status: str) -> None:
        self.observe_ask(status)

    def observe_llm(
        self,
        purpose: str,
        status: str,
        *,
        duration_seconds: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit_tokens: int = 0,
    ) -> None:
        p = purpose or "unknown"
        st = status or "ok"
        with self._lock:
            self._llm_calls[(p, st)] += 1
            if duration_seconds is not None:
                _observe_histogram(
                    LLM_DURATION_BUCKETS,
                    self._llm_dur_buckets,
                    self._llm_dur_sum,
                    self._llm_dur_count,
                    (p,),
                    duration_seconds,
                )
            if input_tokens > 0:
                self._llm_tokens[(p, "input")] += int(input_tokens)
            if output_tokens > 0:
                self._llm_tokens[(p, "output")] += int(output_tokens)
            if cache_hit_tokens > 0:
                self._llm_tokens[(p, "cache_hit")] += int(cache_hit_tokens)

    def inc_llm(self, purpose: str, status: str) -> None:
        self.observe_llm(purpose, status)

    def observe_datasource_query(self, engine: str, status: str, duration_seconds: float | None = None) -> None:
        eng = engine or "unknown"
        st = status or "ok"
        with self._lock:
            self._ds_queries[(eng, st)] += 1
            if duration_seconds is not None:
                _observe_histogram(
                    DS_QUERY_DURATION_BUCKETS,
                    self._ds_dur_buckets,
                    self._ds_dur_sum,
                    self._ds_dur_count,
                    (eng,),
                    duration_seconds,
                )

    def observe_job(self, kind: str, status: str, duration_seconds: float | None = None) -> None:
        k = kind or "unknown"
        st = status or "unknown"
        with self._lock:
            self._job_runs[(k, st)] += 1
            if duration_seconds is not None:
                _observe_histogram(
                    JOB_DURATION_BUCKETS,
                    self._job_dur_buckets,
                    self._job_dur_sum,
                    self._job_dur_count,
                    (k,),
                    duration_seconds,
                )

    def inc_job(self, kind: str, status: str) -> None:
        self.observe_job(kind, status)

    def render_prometheus(self) -> str:
        with self._lock:
            in_flight = self._http_in_flight
            http_reqs = dict(self._http_requests)
            http_buckets = {k: list(v) for k, v in self._http_dur_buckets.items()}
            http_sum = dict(self._http_dur_sum)
            http_count = dict(self._http_dur_count)

            ask_reqs = dict(self._ask_requests)
            ask_buckets = {k: list(v) for k, v in self._ask_dur_buckets.items()}
            ask_sum = dict(self._ask_dur_sum)
            ask_count = dict(self._ask_dur_count)

            llm_calls = dict(self._llm_calls)
            llm_buckets = {k: list(v) for k, v in self._llm_dur_buckets.items()}
            llm_sum = dict(self._llm_dur_sum)
            llm_count = dict(self._llm_dur_count)
            llm_tokens = dict(self._llm_tokens)

            ds_queries = dict(self._ds_queries)
            ds_buckets = {k: list(v) for k, v in self._ds_dur_buckets.items()}
            ds_sum = dict(self._ds_dur_sum)
            ds_count = dict(self._ds_dur_count)

            job_runs = dict(self._job_runs)
            job_buckets = {k: list(v) for k, v in self._job_dur_buckets.items()}
            job_sum = dict(self._job_dur_sum)
            job_count = dict(self._job_dur_count)

        lines: list[str] = [
            "# HELP avelis_http_requests_total Total HTTP requests handled.",
            "# TYPE avelis_http_requests_total counter",
        ]
        for (method, path, status_code), val in sorted(http_reqs.items()):
            lbl = _format_labels((("method", method), ("path", path), ("status", status_code)))
            lines.append(f"avelis_http_requests_total{lbl} {val}")
        _render_histogram(
            lines,
            "avelis_http_request_duration_seconds",
            "HTTP request duration histogram in seconds.",
            ("method", "path"),
            DEFAULT_HTTP_BUCKETS,
            http_buckets,
            http_sum,
            http_count,
        )
        lines.extend(
            [
                "# HELP avelis_http_requests_in_flight Current number of in-flight HTTP requests.",
                "# TYPE avelis_http_requests_in_flight gauge",
                f"avelis_http_requests_in_flight {in_flight}",
                "# HELP avelis_ask_requests_total Total ask executions by terminal status.",
                "# TYPE avelis_ask_requests_total counter",
            ]
        )
        for (status_val,), val in sorted(ask_reqs.items()):
            lines.append(f"avelis_ask_requests_total{_format_labels((('status', status_val),))} {val}")
        _render_histogram(
            lines,
            "avelis_ask_duration_seconds",
            "End-to-end ask pipeline duration in seconds.",
            ("status",),
            ASK_DURATION_BUCKETS,
            ask_buckets,
            ask_sum,
            ask_count,
        )
        lines.extend(
            [
                "# HELP avelis_llm_calls_total Total recorded LLM calls by purpose and status.",
                "# TYPE avelis_llm_calls_total counter",
            ]
        )
        for (purpose, status_val), val in sorted(llm_calls.items()):
            lines.append(
                f"avelis_llm_calls_total{_format_labels((('purpose', purpose), ('status', status_val)))} {val}"
            )
        _render_histogram(
            lines,
            "avelis_llm_call_duration_seconds",
            "LLM invocation latency in seconds.",
            ("purpose",),
            LLM_DURATION_BUCKETS,
            llm_buckets,
            llm_sum,
            llm_count,
        )
        lines.extend(
            [
                "# HELP avelis_llm_tokens_total Cumulative LLM tokens by purpose and token type.",
                "# TYPE avelis_llm_tokens_total counter",
            ]
        )
        for (purpose, tok_type), val in sorted(llm_tokens.items()):
            lines.append(f"avelis_llm_tokens_total{_format_labels((('purpose', purpose), ('type', tok_type)))} {val}")
        lines.extend(
            [
                "# HELP avelis_datasource_queries_total Total read-only datasource SELECT executions.",
                "# TYPE avelis_datasource_queries_total counter",
            ]
        )
        for (engine, status_val), val in sorted(ds_queries.items()):
            lines.append(
                f"avelis_datasource_queries_total{_format_labels((('engine', engine), ('status', status_val)))} {val}"
            )
        _render_histogram(
            lines,
            "avelis_datasource_query_duration_seconds",
            "Datasource query execution latency in seconds.",
            ("engine",),
            DS_QUERY_DURATION_BUCKETS,
            ds_buckets,
            ds_sum,
            ds_count,
        )
        lines.extend(
            [
                "# HELP avelis_job_runs_total Total background job runs by kind and outcome.",
                "# TYPE avelis_job_runs_total counter",
            ]
        )
        for (kind, status_val), val in sorted(job_runs.items()):
            lines.append(f"avelis_job_runs_total{_format_labels((('kind', kind), ('status', status_val)))} {val}")
        _render_histogram(
            lines,
            "avelis_job_duration_seconds",
            "Background job execution duration in seconds.",
            ("kind",),
            JOB_DURATION_BUCKETS,
            job_buckets,
            job_sum,
            job_count,
        )

        pool_snap = _db_pool_snapshot()
        lines.extend(
            [
                "# HELP avelis_db_pool_connections Metadata PostgreSQL connection pool state.",
                "# TYPE avelis_db_pool_connections gauge",
            ]
        )
        for state, val in sorted(pool_snap.items()):
            lines.append(f"avelis_db_pool_connections{_format_labels((('state', state),))} {val}")

        uptime = max(0.0, time.monotonic() - _PROCESS_STARTED)
        version, env_name = _build_metadata()
        lines.extend(
            [
                "# HELP avelis_process_uptime_seconds Seconds since Avelis observability module initialization.",
                "# TYPE avelis_process_uptime_seconds gauge",
                f"avelis_process_uptime_seconds {uptime:.3f}",
                "# HELP avelis_build_info Static build and environment metadata gauge.",
                "# TYPE avelis_build_info gauge",
                f"avelis_build_info{_format_labels((('environment', env_name), ('version', version)))} 1",
            ]
        )
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()
