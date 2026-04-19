"""Tests for the in-process Prometheus-format metrics."""

from __future__ import annotations

from beaconmcp.metrics import Counter, Histogram, Registry


def test_counter_increments_with_labels() -> None:
    c = Counter("foo_total", "help")
    c.inc(tool="ssh_run", status="ok")
    c.inc(tool="ssh_run", status="ok")
    c.inc(tool="ssh_run", status="err")
    out = c.render()
    assert 'foo_total{status="ok",tool="ssh_run"} 2' in out
    assert 'foo_total{status="err",tool="ssh_run"} 1' in out


def test_histogram_buckets_and_sum() -> None:
    h = Histogram("lat_ms", "help", buckets_ms=(10, 100))
    h.observe(5, tool="x")
    h.observe(50, tool="x")
    h.observe(200, tool="x")
    out = h.render()
    # 5ms: fits bucket <=10, <=100, +Inf
    # 50ms: fits <=100, +Inf
    # 200ms: only +Inf
    assert 'lat_ms_bucket{le="10",tool="x"} 1' in out
    assert 'lat_ms_bucket{le="100",tool="x"} 2' in out
    assert 'lat_ms_bucket{le="+Inf",tool="x"} 3' in out
    assert "lat_ms_sum" in out
    assert 'lat_ms_count{tool="x"} 3' in out


def test_registry_concats() -> None:
    r = Registry()
    r.register(Counter("a_total", "a"))
    r.register(Counter("b_total", "b"))
    out = r.render()
    assert "# TYPE a_total counter" in out
    assert "# TYPE b_total counter" in out
    # No empty tail.
    assert out.endswith("\n")


def test_histogram_time_context_manager() -> None:
    h = Histogram("t_ms", "help")
    with h.time(tool="y"):
        pass
    out = h.render()
    assert 't_ms_count{tool="y"} 1' in out
