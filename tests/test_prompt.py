"""The sender's PROMPT overrides (DESIGN section 21): pure parsing, no network, no clock."""

from datetime import date

import pytest

from app.extract.prompt import (
    assignee,
    deadline,
    overrides,
    parse_prompt,
    reviewer,
    split_email_prompt,
)

MONDAY = date(2026, 10, 26)


def test_the_full_sentence_sets_all_three():
    prompt = parse_prompt(
        "prompt inject: assign this to staff p with reviewer staff d deadline 3 days from now"
    )
    assert prompt == "assign this to staff p with reviewer staff d deadline 3 days from now"
    asked = overrides(prompt, MONDAY)
    assert (asked.assign_to, asked.reviewer, asked.due) == (
        "staff p",
        "staff d",
        date(2026, 10, 29),
    )
    assert asked.flags == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("assign this to Staff P with reviewer Staff A", "Staff P"),
        ("assign to Staff P, reviewer Staff A", "Staff P"),
        ("assign to Staff P and reviewer Staff A", "Staff P"),
        ("assign to Staff P deadline 20 Nov", "Staff P"),
        ("assign to Staff P by 20 Nov", "Staff P"),
        ("assign to Staff P in 3 days please", "Staff P"),
        ("tugaskan ke Staff P dengan reviewer Staff A", "Staff P"),
        ("kasih ke Staff P ya", "Staff P"),
        ("this is the ATMR report", None),
    ],
)
def test_the_name_stops_at_the_next_instruction(text, name):
    assert assignee(text) == name


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("with reviewer Staff A deadline 3 days from now", "Staff A"),
        ("reviewer: Staff A", "Staff A"),
        ("reviewer is Staff A.", "Staff A"),
        ("reviewed by Staff A", "Staff A"),
        ("direview oleh Staff A ya", "Staff A"),
        ("assign to Staff P", None),
    ],
)
def test_reviewer(text, name):
    assert reviewer(text) == name


@pytest.mark.parametrize(
    ("text", "due"),
    [
        ("deadline 3 days from now", date(2026, 10, 29)),
        ("in 3 days", date(2026, 10, 29)),
        ("deadline 3 days", date(2026, 10, 29)),
        ("3 hari lagi", date(2026, 10, 29)),
        ("dalam 2 minggu", date(2026, 11, 9)),
        ("tomorrow", date(2026, 10, 27)),
        ("deadline 5 days from now", date(2026, 10, 30)),  # Saturday -> the Friday before
        ("in 6 days", date(2026, 10, 30)),  # Sunday -> the Friday before
        ("deadline 20 Nov", date(2026, 11, 20)),
        ("due 20/11/2026", date(2026, 11, 20)),
        ("deadline is on Saturday 21 Nov 2026", date(2026, 11, 21)),  # typed: kept as typed
        ("this is the ATMR report from 20 Nov", None),  # a date without a deadline word
        ("assign to Staff P", None),
    ],
)
def test_deadline(text, due):
    assert deadline(text, MONDAY)[0] == due


def test_a_typed_deadline_in_the_past_is_flagged():
    assert deadline("deadline 1 Oct 2026", MONDAY) == (
        date(2026, 10, 1),
        ["PROMPT deadline already passed"],
    )


def test_prompt_inject_is_a_prompt():
    assert parse_prompt("PROMPT INJECT: assign to Staff P") == "assign to Staff P"
    assert parse_prompt("Prompt injection assign to Staff P") == "assign to Staff P"
    assert parse_prompt("Please inject the prompt") is None


def test_split_email_prompt():
    body = "\nPROMPT: assign to Staff P\nreviewer Staff A\n\nTolong dicek ya\n"
    assert split_email_prompt(body) == ("assign to Staff P\nreviewer Staff A", "Tolong dicek ya")
    assert split_email_prompt("Tolong dicek\nPROMPT: assign to Staff P") == (
        "",
        "Tolong dicek\nPROMPT: assign to Staff P",
    )
