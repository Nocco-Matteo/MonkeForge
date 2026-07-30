"""Safety net before the nodes.py split: every N.<name> referenced in graph.py
is a real callable on the nodes module, and the graph compiles.

AST-introspects graph.py for all attribute accesses on the nodes module (N),
then asserts each one exists and is callable — catching a re-export forgotten
during the split before a runtime AttributeError does.
"""
import ast
import inspect
import unittest

from pipeline_graph import graph as G, nodes as N


def _n_attribute_names(source: str) -> set[str]:
    """All N.<name> attribute references in the source."""
    tree = ast.parse(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "N"):
            out.add(node.attr)
    return out


class NodeSurface(unittest.TestCase):
    def test_every_n_reference_is_callable(self):
        names = _n_attribute_names(inspect.getsource(G))
        missing = {n for n in names if not callable(getattr(N, n, None))}
        self.assertFalse(
            missing,
            f"graph.py references N.{missing} but these are not callable on "
            f"the nodes module — a re-export was forgotten.")

    def test_build_graph_compiles(self):
        compiled = G.build_graph()
        self.assertIsNotNone(compiled)


if __name__ == "__main__":
    unittest.main()
