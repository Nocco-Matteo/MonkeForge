"""agents: model+cmd are required in monkeforge.yaml — no built-in defaults."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_graph import config as C


_MINIMAL_AGENTS = {
    role: {
        "model": f"model-{role.lower()}",
        "cmd": f"echo-{role.lower()} --model {{model}} -p {{prompt}}",
    }
    for role in C.REQUIRED_ROLES
}


class TestBuildRoleConfig:
    def test_full_agents_ok(self):
        cfg = C.build_role_config(_MINIMAL_AGENTS)
        assert set(cfg) == set(C.REQUIRED_ROLES)
        assert cfg["IMPLEMENTER"]["model"] == "model-implementer"
        assert "echo-implementer" in cfg["IMPLEMENTER"]["cmd"]

    def test_missing_agents_map_raises(self):
        with pytest.raises(C.AgentsConfigError) as ei:
            C.build_role_config(None, yaml_path=Path("/tmp/monkeforge.yaml"))
        msg = ei.value.cli_message()
        assert msg.startswith("error: /tmp/monkeforge.yaml:")
        assert "missing required top-level `agents:`" in msg
        assert "BOTH `model:` and `cmd:`" in msg
        assert "traceback" not in msg.lower()
        with pytest.raises(C.AgentsConfigError):
            C.build_role_config({})

    def test_missing_one_model_raises(self):
        agents = {k: dict(v) for k, v in _MINIMAL_AGENTS.items()}
        del agents["JUDGE"]["model"]
        with pytest.raises(C.AgentsConfigError) as ei:
            C.build_role_config(
                agents,
                yaml_path=Path("/x/monkeforge.yaml"),
                example_path=Path("/x/monkeforge.example.yaml"),
            )
        msg = ei.value.cli_message()
        assert "JUDGE (missing model)" in msg
        assert "model: <your-model>" in msg
        assert "cmd:" in msg

    def test_missing_cmd_raises(self):
        agents = {k: dict(v) for k, v in _MINIMAL_AGENTS.items()}
        del agents["PROPOSER"]["cmd"]
        with pytest.raises(C.AgentsConfigError) as ei:
            C.build_role_config(agents)
        assert "PROPOSER (missing cmd)" in ei.value.cli_message()

    def test_empty_model_raises(self):
        agents = {k: dict(v) for k, v in _MINIMAL_AGENTS.items()}
        agents["PROPOSER"]["model"] = "   "
        with pytest.raises(C.AgentsConfigError, match="PROPOSER"):
            C.build_role_config(agents)

    def test_cmd_from_yaml_only(self):
        agents = {k: dict(v) for k, v in _MINIMAL_AGENTS.items()}
        agents["JUDGE"]["cmd"] = "my-cli --model {model} {prompt}"
        cfg = C.build_role_config(agents)
        assert cfg["JUDGE"]["cmd"] == "my-cli --model {model} {prompt}"

    def test_live_role_config_has_model_and_cmd(self):
        """The repo monkeforge.yaml must satisfy the contract."""
        assert set(C.ROLE_CONFIG) == set(C.REQUIRED_ROLES)
        for role, cfg in C.ROLE_CONFIG.items():
            assert cfg["model"].strip(), f"{role} has empty model"
            assert cfg["cmd"].strip(), f"{role} has empty cmd"


class TestNoHardcodedCmds:
    def test_config_has_no_default_cmd_table(self):
        assert not hasattr(C, "_DEFAULT_ROLE_CMD")
