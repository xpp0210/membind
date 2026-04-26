"""
MemBind Prometheus 指标
"""

from prometheus_client import Counter, Histogram, Gauge

REQUEST_TOTAL = Counter(
    "membind_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

REQUEST_DURATION = Histogram(
    "membind_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
)

MEMORIES_TOTAL = Gauge(
    "membind_memories_total",
    "Total active memories",
    ["namespace"],
)

CONFLICTS_TOTAL = Counter(
    "membind_conflicts_total",
    "Total conflicts detected",
    ["resolution"],
)
