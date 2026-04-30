"""Rubric for grading Files MCP eval traces.

A rubric is a dataclass — not a free-form LLM-rated score — because
PR1's eval gate is a **correctness** check, not a model-quality
check. We grade four dimensions:

- ``tool_calls_match`` — actual tool sequence equals expected sequence
  (order matters; tools are individually pure)
- ``no_extra_calls`` — actual sequence has no extra Graph calls
  (catches cache misses, retry storms)
- ``output_shape_ok`` — final tool output matches the trace's
  ``expected_output_subset`` (subset match: every key the trace
  specifies must be present and equal)
- ``no_errors`` — no FilesError raised at any step

A trace passes only when all four dimensions are True. A failing
trace produces a structured ``RubricScore`` so the test framework
can show *which* dimension failed without sifting through tool logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RubricScore:
    """Per-trace eval result."""

    trace_name: str
    tool_calls_match: bool
    no_extra_calls: bool
    output_shape_ok: bool
    no_errors: bool
    actual_tool_sequence: list[str] = field(default_factory=list)
    expected_tool_sequence: list[str] = field(default_factory=list)
    failure_reason: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.tool_calls_match
            and self.no_extra_calls
            and self.output_shape_ok
            and self.no_errors
        )

    def assert_passed(self) -> None:
        """Raise AssertionError with a structured failure message."""
        if self.passed:
            return
        bits = []
        if not self.tool_calls_match:
            bits.append(
                f"tool_calls_match=False "
                f"(expected={self.expected_tool_sequence!r} "
                f"actual={self.actual_tool_sequence!r})"
            )
        if not self.no_extra_calls:
            bits.append(
                f"no_extra_calls=False "
                f"(expected={self.expected_tool_sequence!r} "
                f"actual={self.actual_tool_sequence!r})"
            )
        if not self.output_shape_ok:
            bits.append("output_shape_ok=False (subset mismatch)")
        if not self.no_errors:
            bits.append(f"no_errors=False ({self.failure_reason!r})")
        raise AssertionError(f"Eval trace {self.trace_name!r} failed: " + "; ".join(bits))


def score_trace(
    *,
    trace_name: str,
    expected_tools: list[str],
    actual_tools: list[str],
    expected_output_subset: dict | None,
    actual_output: dict | None,
    error: Exception | None = None,
) -> RubricScore:
    """Compute a RubricScore from a trace's actual + expected behavior."""

    tool_calls_match = expected_tools == actual_tools
    no_extra_calls = len(actual_tools) == len(expected_tools) and all(
        a == e for a, e in zip(actual_tools, expected_tools, strict=False)
    )
    output_shape_ok = True
    if expected_output_subset is not None:
        if actual_output is None:
            output_shape_ok = False
        else:
            for key, expected_value in expected_output_subset.items():
                if key not in actual_output:
                    output_shape_ok = False
                    break
                if actual_output[key] != expected_value:
                    output_shape_ok = False
                    break
    no_errors = error is None
    return RubricScore(
        trace_name=trace_name,
        tool_calls_match=tool_calls_match,
        no_extra_calls=no_extra_calls,
        output_shape_ok=output_shape_ok,
        no_errors=no_errors,
        actual_tool_sequence=actual_tools,
        expected_tool_sequence=expected_tools,
        failure_reason=("" if error is None else f"{type(error).__name__}: {error}"),
    )
