"""结构化澄清请求的校验与公开投影(迁移自 TF tests/test_agent_user_input.py)。

API 契约用例(MessageIn/SessionOut)随 api/v1/chat.py 在后续波次搬运。
"""

from uuid import uuid4

import pytest

from nicekit.agent.user_input import (
    USER_INPUT_MAX_OPTIONS,
    USER_INPUT_MAX_QUESTIONS,
    UserInputValidationError,
    normalize_answers,
    normalize_questions,
    public_user_input_request,
)


def questions() -> list[dict]:
    return [
        {
            "id": "start_city",
            "header": "出发地",
            "question": "从哪里出发？",
            "options": [
                {"label": "上海", "description": "浦东或虹桥"},
                {"label": "北京", "description": None},
            ],
            "multi_select": False,
        },
        {
            "id": "interests",
            "header": "关注点",
            "question": "关注哪些体验？",
            "options": [
                {"label": "美食", "description": None},
                {"label": "展览", "description": None},
            ],
            "multi_select": True,
        },
    ]


def test_normalize_questions_returns_bounded_normalized_payload() -> None:
    normalized = normalize_questions(questions())

    assert [item["id"] for item in normalized] == ["start_city", "interests"]
    assert normalized[0]["multi_select"] is False
    assert normalized[0]["options"][1]["description"] is None


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda items: [], f"1-{USER_INPUT_MAX_QUESTIONS}"),
        (lambda items: items * 3, f"1-{USER_INPUT_MAX_QUESTIONS}"),
        (lambda items: [{**items[0], "id": "1bad"}], "必须以字母开头"),
        (lambda items: [items[0], {**items[1], "id": items[0]["id"]}], "重复"),
        (lambda items: [{**items[0], "header": ""}], "header"),
        (lambda items: [{**items[0], "question": ""}], "question"),
        (lambda items: [{**items[0], "options": items[0]["options"][:1]}], "options"),
    ],
)
def test_normalize_questions_rejects_contract_violations(mutate, error: str) -> None:
    with pytest.raises(UserInputValidationError, match=error):
        normalize_questions(mutate(questions()))


def test_options_are_capped_and_deduplicated() -> None:
    too_many = questions()[:1]
    too_many[0]["options"] = [
        {"label": f"选项{index}", "description": None}
        for index in range(USER_INPUT_MAX_OPTIONS + 1)
    ]
    with pytest.raises(UserInputValidationError, match="options"):
        normalize_questions(too_many)

    duplicated = questions()[:1]
    duplicated[0]["options"][1]["label"] = duplicated[0]["options"][0]["label"]
    with pytest.raises(UserInputValidationError, match="重复"):
        normalize_questions(duplicated)


def test_normalize_answers_accepts_fixed_and_other_values() -> None:
    request = {"questions": questions()}
    submission = {
        "answers": [
            {"question_id": "start_city", "selected": ["上海"], "other": None},
            {"question_id": "interests", "selected": ["美食"], "other": "夜场活动"},
        ]
    }

    assert normalize_answers(request, submission) == [
        {
            "question_id": "start_city",
            "question": "从哪里出发？",
            "values": ["上海"],
        },
        {
            "question_id": "interests",
            "question": "关注哪些体验？",
            "values": ["美食", "夜场活动"],
        },
    ]


def test_normalize_answers_rejects_missing_or_foreign_selection() -> None:
    with pytest.raises(UserInputValidationError, match="完整回答"):
        normalize_answers(
            {"questions": questions()},
            {
                "answers": [
                    {"question_id": "start_city", "selected": ["上海"], "other": None}
                ]
            },
        )

    with pytest.raises(UserInputValidationError, match="不属于"):
        normalize_answers(
            {"questions": questions()[:1]},
            {
                "answers": [
                    {"question_id": "start_city", "selected": ["广州"], "other": None}
                ]
            },
        )


def test_single_select_question_accepts_exactly_one_value() -> None:
    with pytest.raises(UserInputValidationError, match="只能选择一项"):
        normalize_answers(
            {"questions": questions()[:1]},
            {
                "answers": [
                    {
                        "question_id": "start_city",
                        "selected": ["上海"],
                        "other": "深圳",
                    }
                ]
            },
        )


def test_model_questions_cannot_supply_client_owned_other_option() -> None:
    invalid = questions()[:1]
    invalid[0]["options"][1]["label"] = "其他"

    with pytest.raises(UserInputValidationError, match="客户端"):
        normalize_questions(invalid)


def test_public_request_hides_internal_tool_call_identity() -> None:
    request = {
        "kind": "user_input",
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "tool_call_id": "secret-call-id",
        "requested_at": "2026-07-30T00:00:00Z",
        "questions": questions(),
    }

    public = public_user_input_request(request)

    assert public is not None
    assert public["request_id"] == request["request_id"]
    assert "tool_call_id" not in public
    assert public_user_input_request(None) is None
