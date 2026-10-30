import copy
import json
from pathlib import Path

import pytest

from agents_team.schemas import SCHEMAS, SCHEMAS_DIR, errors, load, validator

EXAMPLES = SCHEMAS_DIR / "examples"


@pytest.fixture
def review():
    return load(EXAMPLES / "review.json")


@pytest.mark.parametrize("name", SCHEMAS)
def test_schemas_sao_validos(name):
    validator(name)  # check_schema levanta se o schema for inválido


@pytest.mark.parametrize(
    ("name", "example"),
    [
        ("finding-report", "review.json"),
        ("finding-report", "sec.json"),
        ("state", "state.json"),
        ("config", "agents-team.yml"),
    ],
)
def test_exemplos_validos(name, example):
    assert errors(name, load(EXAMPLES / example)) == []


def test_block_exige_achado_bloqueante(review):
    review["verdict"] = "block"
    for f in review["findings"]:
        f["blocking"] = False
    assert errors("finding-report", review)


def test_approve_nao_aceita_achado_bloqueante(review):
    review["verdict"] = "approve"
    assert errors("finding-report", review)


def test_iteracao_acima_do_limite(review):
    review["iteration"] = 4
    assert errors("finding-report", review)


def test_jira_action_desconhecida(review):
    review["jira_actions"] = [{"type": "delete_issue"}]
    assert errors("finding-report", review)


def test_update_field_sem_operacao(review):
    review["jira_actions"] = [{"type": "update_field", "field": "labels"}]
    assert errors("finding-report", review)


def test_owner_fora_do_padrao(review):
    review["findings"][0]["owner"] = "Aang"
    assert errors("finding-report", review)


def test_campo_extra_rejeitado(review):
    review["foo"] = 1
    assert errors("finding-report", review)


def test_estado_peer_review_exige_mr():
    state = load(EXAMPLES / "state.json")
    state["flow"] = "peer-review"
    assert any("merge_request" in e for e in errors("state", state))


def test_estado_escalado_exige_motivo():
    state = load(EXAMPLES / "state.json")
    state["phase"] = "escalated"
    assert errors("state", state)
    state["escalation"] = {"reason": "max_iterations"}
    assert errors("state", state) == []


def test_branch_de_trabalho_fora_do_padrao():
    state = load(EXAMPLES / "state.json")
    state["branches"]["work"] = "feature/x"
    assert errors("state", state)


def test_config_test_single_exige_target():
    cfg = load(EXAMPLES / "agents-team.yml")
    cfg["commands"]["test_single"] = "uv run pytest"
    assert errors("config", cfg)


def test_config_fluxo_desconhecido():
    cfg = load(EXAMPLES / "agents-team.yml")
    cfg["jira"]["flow_by_label"]["x"] = "deploy"
    assert errors("config", cfg)


def test_data_invalida_rejeitada():
    state = load(EXAMPLES / "state.json")
    state["created_at"] = "ontem"
    assert errors("state", state)
