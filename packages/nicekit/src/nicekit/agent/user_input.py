"""Structured clarification request validation and public projection."""

from __future__ import annotations

import re
from collections.abc import Mapping

USER_INPUT_MAX_QUESTIONS = 4
USER_INPUT_MAX_OPTIONS = 4
USER_INPUT_MIN_OPTIONS = 2

_QUESTION_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_RESERVED_OTHER_LABELS = {"other", "其他", "其它"}


class UserInputValidationError(ValueError):
    """The model request or user submission violates the interaction contract."""


def normalize_questions(value: object) -> list[dict]:
    if not isinstance(value, list) or not (
        1 <= len(value) <= USER_INPUT_MAX_QUESTIONS
    ):
        raise UserInputValidationError(
            f"questions 必须包含 1-{USER_INPUT_MAX_QUESTIONS} 个问题"
        )

    questions: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise UserInputValidationError(f"questions[{index}] 必须是对象")

        question_id = str(raw.get("id") or "").strip()
        header = str(raw.get("header") or "").strip()
        prompt = str(raw.get("question") or "").strip()
        if not _QUESTION_ID_PATTERN.fullmatch(question_id):
            raise UserInputValidationError(
                f"questions[{index}].id 必须以字母开头且只含字母、数字、_、-"
            )
        if question_id in seen_ids:
            raise UserInputValidationError(f"questions[{index}].id 重复")
        if not header or len(header) > 24:
            raise UserInputValidationError(
                f"questions[{index}].header 必须为 1-24 个字符"
            )
        if not prompt or len(prompt) > 500:
            raise UserInputValidationError(
                f"questions[{index}].question 必须为 1-500 个字符"
            )

        raw_options = raw.get("options")
        if not isinstance(raw_options, list) or not (
            USER_INPUT_MIN_OPTIONS
            <= len(raw_options)
            <= USER_INPUT_MAX_OPTIONS
        ):
            raise UserInputValidationError(
                f"questions[{index}].options 必须包含 "
                f"{USER_INPUT_MIN_OPTIONS}-{USER_INPUT_MAX_OPTIONS} 个选项"
            )

        options: list[dict] = []
        seen_labels: set[str] = set()
        for option_index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, Mapping):
                raise UserInputValidationError(
                    f"questions[{index}].options[{option_index}] 必须是对象"
                )
            label = str(raw_option.get("label") or "").strip()
            description_value = raw_option.get("description")
            description = (
                str(description_value).strip()
                if description_value is not None
                else None
            )
            normalized_label = label.casefold()
            if not label or len(label) > 100:
                raise UserInputValidationError(
                    f"questions[{index}].options[{option_index}].label "
                    "必须为 1-100 个字符"
                )
            if normalized_label in seen_labels:
                raise UserInputValidationError(
                    f"questions[{index}].options[{option_index}].label 重复"
                )
            if normalized_label in _RESERVED_OTHER_LABELS:
                raise UserInputValidationError(
                    "无需提供 Other/其他选项，客户端会统一添加"
                )
            if description is not None and len(description) > 300:
                raise UserInputValidationError(
                    f"questions[{index}].options[{option_index}].description "
                    "最多 300 个字符"
                )
            seen_labels.add(normalized_label)
            options.append({"label": label, "description": description or None})

        seen_ids.add(question_id)
        questions.append(
            {
                "id": question_id,
                "header": header,
                "question": prompt,
                "options": options,
                "multi_select": bool(raw.get("multi_select")),
            }
        )
    return questions


def normalize_answers(request: Mapping, submission: Mapping) -> list[dict]:
    questions = normalize_questions(request.get("questions"))
    raw_answers = submission.get("answers")
    if not isinstance(raw_answers, list):
        raise UserInputValidationError("answers 必须是数组")

    answers_by_id: dict[str, Mapping] = {}
    for index, raw in enumerate(raw_answers):
        if not isinstance(raw, Mapping):
            raise UserInputValidationError(f"answers[{index}] 必须是对象")
        question_id = str(raw.get("question_id") or "").strip()
        if not question_id or question_id in answers_by_id:
            raise UserInputValidationError("answers.question_id 不能为空或重复")
        answers_by_id[question_id] = raw

    expected_ids = {question["id"] for question in questions}
    if set(answers_by_id) != expected_ids:
        raise UserInputValidationError("必须完整回答当前请求中的所有问题")

    normalized: list[dict] = []
    for question in questions:
        raw = answers_by_id[question["id"]]
        selected_value = raw.get("selected")
        if not isinstance(selected_value, list):
            raise UserInputValidationError(
                f"{question['id']}.selected 必须是数组"
            )
        selected = [str(value).strip() for value in selected_value]
        if any(not value for value in selected) or len(selected) != len(set(selected)):
            raise UserInputValidationError(
                f"{question['id']}.selected 不可包含空值或重复值"
            )
        allowed = {option["label"] for option in question["options"]}
        if not set(selected).issubset(allowed):
            raise UserInputValidationError(
                f"{question['id']} 包含不属于当前问题的选项"
            )
        other_value = raw.get("other")
        other = str(other_value).strip() if other_value is not None else ""
        if len(other) > 1000:
            raise UserInputValidationError(
                f"{question['id']}.other 最多 1000 个字符"
            )
        values = [*selected, *([other] if other else [])]
        if question["multi_select"]:
            if not values:
                raise UserInputValidationError(f"{question['id']} 至少选择一项")
        elif len(values) != 1:
            raise UserInputValidationError(f"{question['id']} 只能选择一项")
        normalized.append(
            {
                "question_id": question["id"],
                "question": question["question"],
                "values": values,
            }
        )
    return normalized


def public_user_input_request(value: Mapping | None) -> dict | None:
    if not value:
        return None
    return {
        "kind": "user_input",
        "request_id": value.get("request_id"),
        "run_id": value.get("run_id"),
        "requested_at": value.get("requested_at"),
        "questions": value.get("questions") or [],
    }
