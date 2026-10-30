import pytest

from agents_team.schemas import SCHEMAS, SCHEMAS_DIR, errors, load, validator

EXAMPLES = SCHEMAS_DIR / "examples"


@pytest.fixture
def review():
    return load(EXAMPLES / "review.json")


@pytest.fixture
def state():
    return load(EXAMPLES / "state.json")


@pytest.fixture
def config():
    return load(EXAMPLES / "agents-team.yml")


@pytest.mark.parametrize("name", SCHEMAS)
def test_schemas_are_valid(name):
    validator(name)  # check_schema raises on an invalid schema


@pytest.mark.parametrize(
    ("name", "example"),
    [
        ("finding-report", "review.json"),
        ("finding-report", "sec.json"),
        ("state", "state.json"),
        ("config", "agents-team.yml"),
        ("team-config", "team-config.yml"),
    ],
)
def test_examples_are_valid(name, example):
    assert errors(name, load(EXAMPLES / example)) == []


def test_block_requires_blocking_finding(review):
    review["verdict"] = "block"
    for f in review["findings"]:
        f["blocking"] = False
    assert errors("finding-report", review)


def test_approve_rejects_blocking_finding(review):
    review["verdict"] = "approve"
    assert errors("finding-report", review)


def test_iteration_above_limit(review):
    review["iteration"] = 4
    assert errors("finding-report", review)


def test_plan_stage_requires_iteration_zero(review):
    review["stage"] = "plan"
    assert errors("finding-report", review)
    review["iteration"] = 0
    assert errors("finding-report", review) == []


def test_code_stage_rejects_iteration_zero(review):
    review["iteration"] = 0
    assert errors("finding-report", review)


def test_unknown_jira_action(review):
    review["jira_actions"] = [{"type": "delete_issue"}]
    assert errors("finding-report", review)


def test_update_field_without_operation(review):
    review["jira_actions"] = [{"type": "update_field", "field": "labels"}]
    assert errors("finding-report", review)


def test_owner_format(review):
    review["findings"][0]["owner"] = "Aang"
    assert errors("finding-report", review)


def test_extra_field_rejected(review):
    review["foo"] = 1
    assert errors("finding-report", review)


def test_peer_review_state_requires_mr(state):
    state["flow"] = "peer-review"
    assert any("merge_request" in e for e in errors("state", state))


def test_escalated_state_requires_reason(state):
    state["phase"] = "escalated"
    assert errors("state", state)
    state["escalation"] = {"reason": "max_iterations"}
    assert errors("state", state) == []


def test_work_branch_pattern(state):
    state["branches"]["work"] = "feature/x"
    assert errors("state", state)


def test_plan_round_limit(state):
    state["plan_round"] = 3
    assert errors("state", state)


def test_invalid_datetime_rejected(state):
    state["created_at"] = "yesterday"
    assert errors("state", state)


def test_config_test_single_requires_target(config):
    config["commands"]["test_single"] = "uv run pytest"
    assert errors("config", config)


def test_config_unknown_flow(config):
    config["jira"]["flow_by_label"]["x"] = "deploy"
    assert errors("config", config)


def test_config_plan_approval_values(config):
    config["workflow"]["plan_approval"] = "nobody"
    assert errors("config", config)


def test_team_config_model_map_must_not_be_empty():
    team = load(EXAMPLES / "team-config.yml")
    team["models"]["aang"] = {}
    assert errors("team-config", team)
