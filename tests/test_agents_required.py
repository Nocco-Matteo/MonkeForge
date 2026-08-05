"""agents: models are required in monkeforge.yaml — no built-in defaults."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_graph import config as C


_MINIMAL_AGENTS = {
    role: {"model": f"model-{role.lower()}"}
    for role in C.REQUIRED_ROLES
}


class TestBuildRoleConfig:
    def test_full_agents_ok(self):
        cfg = C.build_role_config(_MINIMAL_AGENTS)
        assert set(cfg) == set(C.REQUIRED_ROLES)
        assert cfg["IMPLEMENTER"]["model"] == "model-implementer"
        assert "{model}" in cfg["IMPLEMENTER"]["cmd"]

    def test_missing_agents_map_raises(self):
        with pytest.raises(C.AgentsConfigError) as ei:
            C.build_role_config(None, yaml_path=Path("/tmp/monkeforge.yaml"))
        msg = ei.value.cli_message()
        assert msg.startswith("error: /tmp/monkeforge.yaml:")
        assert "missing required top-level `agents:`" in msg
        assert "monkeforge.example.yaml" in msg
        assert "traceback" not in msg.lower()
        with pytest.raises(C.AgentsConfigError):
            C.build_role_config({})

    def test_missing_one_model_raises(self):
        agents = dict(_MINIMAL_AGENTS)
        del agents["JUDGE"]
        with pytest.raises(C.AgentsConfigError) as ei:
            C.build_role_config(
                agents,
                yaml_path=Path("/x/monkeforge.yaml"),
                example_path=Path("/x/monkeforge.example.yaml"),
            )
        msg = ei.value.cli_message()
        assert msg.startswith("error: /x/monkeforge.yaml:")
        assert "- JUDGE" in msg
        assert "model: <your-model>" in msg
        assert "/x/monkeforge.example.yaml" in msg

    def test_empty_model_raises(self):
        agents = dict(_MINIMAL_AGENTS)
        agents["PROPOSER"] = {"model": "   "}
        with pytest.raises(C.AgentsConfigError, match="PROPOSER"):
            C.build_role_config(agents)

    def test_cmd_override_wins(self):
        agents = dict(_MINIMAL_AGENTS)
        agents["JUDGE"] = {
            "model": "sonnet",
            "cmd": "echo {model} {prompt}",
        }
        cfg = C.build_role_config(agents)
        assert cfg["JUDGE"]["cmd"] == "echo {model} {prompt}"

    def test_live_role_config_has_no_empty_models(self):
        """The repo monkeforge.yaml must satisfy the contract."""
        assert set(C.ROLE_CONFIG) == set(C.REQUIRED_ROLES)
        for role, cfg in C.ROLE_CONFIG.items():
            assert cfg["model"].strip(), f"{role} has empty model"
            assert cfg["cmd"].strip(), f"{role} has empty cmd"
