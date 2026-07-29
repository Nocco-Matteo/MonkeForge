"""Entrypoint for LangGraph Studio / `langgraph dev`.

The platform server supplies its own persistence, so the graph is compiled
WITHOUT a checkpointer here. The CLI (run.py) keeps using SqliteSaver.
"""
from .graph import build_graph

graph = build_graph()
