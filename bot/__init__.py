"""Optional Discord control plane for the LangGraph pipeline.

lg stays standalone: the pipeline never imports this package and runs fine
without it. The bot only *drives* `run.py` from outside — it lets you handle
escalations (resume with an answer, via buttons) and read status/doctor from a
phone, anywhere, with no VPN or open ports (the bot connects OUT to Discord).

It deliberately cannot START a run — only manage the blocks of runs already
under way.
"""
