"""Deterministic acquisition order; it never grants recommendation eligibility."""
from __future__ import annotations


def flow_collection_order(codes, membership, peers):
    """Finish each discovered sector's real membership before opening another.

    Candidate order comes from the research queue, never minute acceleration.
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
