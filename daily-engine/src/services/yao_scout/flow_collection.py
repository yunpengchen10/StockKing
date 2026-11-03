"""Deterministic acquisition order; it never grants recommendation eligibility."""
from __future__ import annotations

from datetime import timedelta

from .fund_flow_evidence import _turnover, session, timestamp


def flow_candidate_priority(codes, intraday, cutoff):
    """Stable acquisition priority for candidates with verifiable turnover.

    Use the same 30-minute denominator and freshness checks as flow evidence.
    Direction, acceleration and money flow never affect this ordering. Nothing
    is removed: the caller still collects each candidate's complete peer set.
    """
    ordered = list(codes)
    now = timestamp(cutoff)
    if now is None:
        return ordered
    ready, pending = [], []
    for code in ordered:
        minute = (intraday or {}).get(code)
        end = timestamp(minute.get('asOf')) if isinstance(minute, dict) else None
        valid = False
        if (end is not None and end.date() == now.date()
                and 0 <= (now-end).total_seconds() < 300 and session(end) is not None):
            start = end - timedelta(minutes=30)
            if session(start) == session(end):
                try:
                    amount, reason = _turnover(minute, code, start, end, 30)
                    valid = amount is not None and reason is None
                except (AttributeError, TypeError, ValueError):
                    # Malformed evidence must not gain acquisition priority.
                    valid = False
        (ready if valid else pending).append(code)
    return ready + pending


def flow_collection_order(codes, membership, peers):
    """Finish each discovered sector's real membership before opening another.

    Candidate order comes from research/window readiness, never acceleration.
    Include non-candidate peers: sampling only selected winners biases breadth.
    """
    result, seen, boards = [], set(), set()
    def append(code):
        code = str(code).zfill(6)
        if code not in seen:
            seen.add(code)
            result.append(code)
    for code in codes:
        board = (membership.get(code) or {}).get('code')
        append(code)
        if board and board not in boards:
            boards.add(board)
            for peer in sorted(set(peers.get(board) or [])):
                append(peer)
    return result
