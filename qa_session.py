"""
qa_session.py - In-memory model of one manual test execution.

A ``TestSession`` is what the tester works on after picking a test point in the
Test Explorer: the test case's steps (with per-step Pass/Fail), the evidence
captured along the way, and the timing that is reported back to Azure DevOps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

UNSPECIFIED = "Unspecified"
PASSED = "Passed"
FAILED = "Failed"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TestPoint:
    """A test case + configuration inside a suite (what ADO actually executes)."""

    id: int
    test_case_id: int
    title: str
    plan_id: Optional[int] = None
    suite_id: Optional[int] = None
    configuration: str = ""
    tester: str = ""
    outcome: str = UNSPECIFIED  # last outcome reported in ADO


@dataclass
class TestStep:
    number: int  # 1-based position shown to the tester
    step_id: int
    action_path: str  # hex path ADO uses for step results, e.g. "00000002"
    step_identifier: str  # "2", or "4;2" for a step inside shared steps #4
    action: str
    expected: str = ""
    shared_title: Optional[str] = None
    outcome: str = UNSPECIFIED
    comment: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class EvidenceItem:
    path: Path
    kind: str  # "screenshot" | "recording"
    step_number: Optional[int] = None
    step_action_path: Optional[str] = None
    attach: bool = True
    created_at: datetime = field(default_factory=utc_now)

    @property
    def label(self) -> str:
        where = f"Step {self.step_number}" if self.step_number else "Whole test"
        return f"{where} · {self.created_at.astimezone():%H:%M:%S} · {self.path.name}"


@dataclass
class TestSession:
    point: TestPoint
    steps: list[TestStep]
    evidence: list[EvidenceItem] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)
    bugs: list[dict] = field(default_factory=list)  # {"id", "url", "title"} raised during this run
    run_id: Optional[int] = None  # set once the result is published
    result_id: Optional[int] = None

    # ---------------------------------------------------------------- progress
    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.outcome != UNSPECIFIED)

    @property
    def duration_ms(self) -> int:
        return int((utc_now() - self.started_at).total_seconds() * 1000)

    def set_step_outcome(self, index: int, outcome: str, comment: Optional[str] = None) -> None:
        step = self.steps[index]
        now = utc_now()
        if step.started_at is None:
            # Approximate the step start with the previous step's completion.
            previous = [s.completed_at for s in self.steps[:index] if s.completed_at]
            step.started_at = previous[-1] if previous else self.started_at
        step.outcome = outcome
        step.completed_at = now if outcome != UNSPECIFIED else None
        if comment is not None:
            step.comment = comment

    def suggested_outcome(self) -> Optional[str]:
        """Failed if any step failed, Passed if all passed, otherwise undecided."""
        if any(s.outcome == FAILED for s in self.steps):
            return FAILED
        if self.steps and all(s.outcome == PASSED for s in self.steps):
            return PASSED
        return None

    def attachments(self) -> list[EvidenceItem]:
        return [e for e in self.evidence if e.attach and e.path.exists()]
