"""Guard against the edge-map drift that once left `route_escalation_return`'s
"summary" branch unmapped — a crash reachable only on that one path, at runtime.

Two checks:
  1. Every string literal `route_escalation_return` can return is declared in
     ESCALATION_RETURNS (parsed from the function's own source).
  2. The compiled graph's escalate edge map covers every declared return, so the
     map cannot silently fall behind the set it is built from.
"""
import ast
import inspect
import unittest

from langgraph.graph import END

from pipeline_graph import graph as G


def _returned_string_literals(fn) -> set[str]:
    """The set of string constants `fn` can `return` (ignores `return END`)."""
    tree = ast.parse(inspect.getsource(fn))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out.add(node.value.value)
    return out


class EscalationReturns(unittest.TestCase):
    def test_every_returned_literal_is_declared(self):
        literals = _returned_string_literals(G.route_escalation_return)
        missing = literals - set(G.ESCALATION_RETURNS)
        self.assertFalse(
            missing,
            f"route_escalation_return can return {sorted(missing)} but they are "
            f"not in ESCALATION_RETURNS — add them (and the escalate edge follows).")

    def test_no_dead_declarations(self):
        literals = _returned_string_literals(G.route_escalation_return)
        # "escalate" is the self-loop target: route_escalation_return is itself
        # wrapped in _safe_router, so a crash inside it returns "escalate" — the
        # escalate node needs an edge to itself, but the function never returns
        # it as a literal on the happy path. Exclude it from the dead-diff.
        dead = set(G.ESCALATION_RETURNS) - literals - {"escalate"}
        self.assertFalse(
            dead, f"ESCALATION_RETURNS lists {sorted(dead)} the function never returns.")

    def test_compiled_escalate_edges_cover_every_return(self):
        # The escalate node must have an outgoing edge to every declared target.
        compiled = G.build_graph()
        edges = compiled.get_graph().edges
        targets = {e.target for e in edges if e.source == "escalate"}
        for name in G.ESCALATION_RETURNS:
            self.assertIn(
                name, targets,
                f"escalate has no edge to {name!r}; the edge map drifted from "
                "ESCALATION_RETURNS.")


if __name__ == "__main__":
    unittest.main()
