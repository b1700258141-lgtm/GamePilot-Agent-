"""固定缺陷矩阵评测：标准答案只在这里，执行器与判定器看不到它。

分工：

- `gamepilot.lab` 提供真实的缺陷靶场（进程内应用实例）；
- `gamepilot.testing` 提供与评测无关的执行器、oracle 与重跑比对；
- 本包组装两者，持有**先于运行写死**的清单（`manifest`）、指标口径（`metrics`）
  与证据模型（`models`），并负责逐组合落盘与退出码。

评测不向被测服务暴露 profile，也不给 oracle 任何预期缺陷标签：
判定只看 HTTP 观测到的游戏状态，缺陷是否被检出完全由规则结论决定。
"""

from .manifest import (
    BASELINE_CASE_IDS,
    BENCHMARK_VERSION,
    COMBINATIONS,
    DESIGNATED_TRIGGERS,
    MANIFEST_SOURCE,
    NEGATIVE_VALIDATION_SOURCE,
    SUITE,
    Combination,
)
from .metrics import METRIC_IDS, build_metrics
from .models import (
    BenchmarkReport,
    BenchmarkSummary,
    CombinationEvidence,
    MetricResult,
    NegativeValidation,
)
from .runner import (
    BENCHMARK_BASE_URL,
    EXIT_DEVIATION,
    EXIT_EXECUTION_ERROR,
    EXIT_OK,
    BenchmarkExecutionError,
    run_benchmark,
)

__all__ = [
    "BASELINE_CASE_IDS",
    "BENCHMARK_BASE_URL",
    "BENCHMARK_VERSION",
    "COMBINATIONS",
    "DESIGNATED_TRIGGERS",
    "EXIT_DEVIATION",
    "EXIT_EXECUTION_ERROR",
    "EXIT_OK",
    "MANIFEST_SOURCE",
    "METRIC_IDS",
    "NEGATIVE_VALIDATION_SOURCE",
    "SUITE",
    "BenchmarkExecutionError",
    "BenchmarkReport",
    "BenchmarkSummary",
    "Combination",
    "CombinationEvidence",
    "MetricResult",
    "NegativeValidation",
    "build_metrics",
    "run_benchmark",
]
