"""Minimal Prometheus text-format metrics for BeaconMCP.

Deliberately avoids ``prometheus_client`` to keep the dependency tree
small. Two primitive counter types cover everything we need right now:

* :class:`Counter` -- monotonic integer counter, optionally labelled.
* :class:`Histogram` -- fixed-bucket histogram over milliseconds.

The :class:`Registry` collects them and renders the Prometheus text
exposition format on demand. Thread-safe via a single registry lock.

Usage::

    from beaconmcp.metrics import REGISTRY, tool_calls, tool_latency_ms

    tool_calls.inc(tool="proxmox_run", status="ok")
    tool_latency_ms.observe(123.4, tool="proxmox_run")

    text = REGISTRY.render()  # served at /metrics
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


def _labels_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{str(v).replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


class Counter:
    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple[tuple[str, str], ...], int] = {}
        self._lock = threading.Lock()

    def inc(self, amount: int = 1, **labels: str) -> None:
        key = _labels_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            snapshot = dict(self._values)
        for key, value in snapshot.items():
            lines.append(f"{self.name}{_format_labels(key)} {value}")
        return "\n".join(lines)


class Histogram:
    """Fixed-bucket histogram. Buckets are upper bounds in milliseconds."""

    # Covers <10ms cached calls all the way to 30-second BMC round trips.
    DEFAULT_BUCKETS_MS: tuple[float, ...] = (
        5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000,
    )

    def __init__(self, name: str, help_text: str, buckets_ms: tuple[float, ...] | None = None) -> None:
        self.name = name
        self.help = help_text
        self._buckets: tuple[float, ...] = tuple(buckets_ms or self.DEFAULT_BUCKETS_MS)
        # Per-label bucket counts + running sum/count.
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._sum: dict[tuple[tuple[str, str], ...], float] = {}
        self._total: dict[tuple[tuple[str, str], ...], int] = {}
        self._lock = threading.Lock()

    def observe(self, value_ms: float, **labels: str) -> None:
        key = _labels_key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self._buckets))
            for i, upper in enumerate(self._buckets):
                if value_ms <= upper:
                    counts[i] += 1
            self._sum[key] = self._sum.get(key, 0.0) + value_ms
            self._total[key] = self._total.get(key, 0) + 1

    @contextmanager
    def time(self, **labels: str) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.observe((time.monotonic() - start) * 1000.0, **labels)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            counts = {k: list(v) for k, v in self._counts.items()}
            sums = dict(self._sum)
            totals = dict(self._total)
        for key, bucket_counts in counts.items():
            # ``observe`` already increments every bucket whose upper bound
            # is >= the value, so each slot holds the cumulative count --
            # render them directly, Prometheus-style.
            for i, upper in enumerate(self._buckets):
                labels_with_le = tuple(sorted(key + (("le", str(upper)),)))
                lines.append(f"{self.name}_bucket{_format_labels(labels_with_le)} {bucket_counts[i]}")
            labels_inf = tuple(sorted(key + (("le", "+Inf"),)))
            lines.append(f"{self.name}_bucket{_format_labels(labels_inf)} {totals[key]}")
            lines.append(f"{self.name}_sum{_format_labels(key)} {sums[key]}")
            lines.append(f"{self.name}_count{_format_labels(key)} {totals[key]}")
        return "\n".join(lines)


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Counter | Histogram] = []

    def register(self, metric: Counter | Histogram) -> Counter | Histogram:
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        parts = [m.render() for m in self._metrics]
        return "\n".join(parts) + "\n"


# --- Default registry + standard metrics -----------------------------------

REGISTRY = Registry()

tool_calls: Counter = REGISTRY.register(  # type: ignore[assignment]
    Counter("beaconmcp_tool_calls_total", "Total MCP tool invocations, by tool and status.")
)
tool_latency_ms: Histogram = REGISTRY.register(  # type: ignore[assignment]
    Histogram("beaconmcp_tool_latency_ms", "Tool call latency in milliseconds, by tool.")
)
auth_events: Counter = REGISTRY.register(  # type: ignore[assignment]
    Counter("beaconmcp_auth_events_total", "Auth events, by kind (login, token, refresh) and outcome.")
)
http_requests: Counter = REGISTRY.register(  # type: ignore[assignment]
    Counter("beaconmcp_http_requests_total", "HTTP requests to BeaconMCP endpoints, by path and status.")
)
