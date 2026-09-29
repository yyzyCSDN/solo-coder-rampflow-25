from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .timeutil import Window, utc


@dataclass(frozen=True)
class CrewMember:
    crew_id: str
    qualifications: frozenset[str]
    base: str
    max_duty_min: int
    min_rest_min: int
    max_continuous_work_min: int


@dataclass(frozen=True)
class DutySegment:
    segment_id: str
    window: Window
    location: str
    qualification: str
    work: bool = True


@dataclass(frozen=True)
class DutyRecord:
    crew_id: str
    segments: tuple[DutySegment, ...]
    duty_start: datetime
    duty_end: datetime
    work_minutes: int
    version: int = 1


class DutyViolation(RuntimeError):
    pass


class CrewDutyController:
    def __init__(self, members: Iterable[CrewMember]):
        self.members = {m.crew_id: m for m in members}
        self.records: dict[str, DutyRecord] = {}

    @staticmethod
    def _minutes(window: Window) -> int:
        return int((window.end - window.start).total_seconds() // 60)

    def _validate_segments(self, member: CrewMember, segments: tuple[DutySegment, ...]) -> None:
        ordered = tuple(sorted(segments, key=lambda s: (s.window.start, s.segment_id)))
        for i, seg in enumerate(ordered):
            if seg.qualification not in member.qualifications:
                raise DutyViolation(
                    f"crew {member.crew_id} lacks {seg.qualification}"
                )
            if i and ordered[i - 1].window.overlaps(seg.window):
                raise DutyViolation("overlapping duty segments")
        if not ordered:
            return
        duty = int((ordered[-1].window.end - ordered[0].window.start).total_seconds() // 60)
        if duty > member.max_duty_min:
            raise DutyViolation("maximum duty exceeded")
        continuous = 0
        last_end = None
        for seg in ordered:
            if last_end is not None:
                gap = int((seg.window.start - last_end).total_seconds() // 60)
                if gap >= 30:
                    continuous = 0
            if seg.work:
                continuous += self._minutes(seg.window)
                if continuous > member.max_continuous_work_min:
                    raise DutyViolation("continuous work limit exceeded")
            last_end = seg.window.end

    def assign(self, crew_id: str, segment: DutySegment) -> DutyRecord:
        member = self.members[crew_id]
        current = self.records.get(crew_id)
        segments = tuple(current.segments if current else ()) + (segment,)
        segments = tuple(sorted(segments, key=lambda s: (s.window.start, s.segment_id)))
        self._validate_segments(member, segments)
        work = sum(self._minutes(s.window) for s in segments if s.work)
        record = DutyRecord(
            crew_id,
            segments,
            segments[0].window.start,
            segments[-1].window.end,
            work,
            1 if current is None else current.version + 1,
        )
        self.records[crew_id] = record
        return record

    def can_accept(self, crew_id: str, segment: DutySegment) -> tuple[bool, str]:
        try:
            member = self.members[crew_id]
            existing = self.records.get(crew_id)
            segments = tuple(existing.segments if existing else ()) + (segment,)
            self._validate_segments(member, segments)
            return True, "ok"
        except (DutyViolation, KeyError) as exc:
            return False, str(exc)

    def rest_until(self, crew_id: str) -> datetime | None:
        record = self.records.get(crew_id)
        if record is None:
            return None
        return record.duty_end + timedelta(minutes=self.members[crew_id].min_rest_min)

    def eligible(
        self,
        qualification: str,
        window: Window,
        location: str,
        last_duty_end: Mapping[str, datetime] | None = None,
    ) -> tuple[str, ...]:
        last_duty_end = last_duty_end or {}
        candidates = []
        for crew_id, member in self.members.items():
            if qualification not in member.qualifications:
                continue
            prior_end = last_duty_end.get(crew_id)
            if prior_end is not None:
                prior_end = utc(prior_end)
                if window.start < prior_end + timedelta(minutes=member.min_rest_min):
                    continue
            probe = DutySegment(
                f"probe:{crew_id}:{window.start.isoformat()}",
                window,
                location,
                qualification,
            )
            ok, _ = self.can_accept(crew_id, probe)
            if ok:
                candidates.append(crew_id)
        return tuple(sorted(candidates))

    def replace_segment(
        self,
        crew_id: str,
        segment_id: str,
        replacement: DutySegment,
        expected_version: int,
    ) -> DutyRecord:
        current = self.records[crew_id]
        if current.version != expected_version:
            raise DutyViolation("stale duty record")
        if segment_id not in {s.segment_id for s in current.segments}:
            raise KeyError(segment_id)
        segments = tuple(
            replacement if s.segment_id == segment_id else s
            for s in current.segments
        )
        segments = tuple(sorted(segments, key=lambda s: (s.window.start, s.segment_id)))
        self._validate_segments(self.members[crew_id], segments)
        work = sum(self._minutes(s.window) for s in segments if s.work)
        updated = DutyRecord(
            crew_id,
            segments,
            segments[0].window.start,
            segments[-1].window.end,
            work,
            current.version + 1,
        )
        self.records[crew_id] = updated
        return updated

    def release_after(self, crew_id: str, cutoff: datetime) -> DutyRecord | None:
        cutoff = utc(cutoff)
        current = self.records.get(crew_id)
        if current is None:
            return None
        kept = tuple(s for s in current.segments if s.window.start < cutoff)
        if not kept:
            self.records.pop(crew_id, None)
            return None
        self._validate_segments(self.members[crew_id], kept)
        work = sum(self._minutes(s.window) for s in kept if s.work)
        updated = DutyRecord(
            crew_id,
            kept,
            kept[0].window.start,
            kept[-1].window.end,
            work,
            current.version + 1,
        )
        self.records[crew_id] = updated
        return updated
