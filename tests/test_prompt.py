"""The sender's PROMPT overrides (DESIGN section 21): pure parsing, no network, no clock."""

from datetime import date

import pytest

from app.extract.prompt import (
    assignee,
    deadline,
    overrides,
    parse_prompt,
    reviewer,
    split_prompt_lines,
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


def test_split_prompt_lines():
    body = "make a financial report for q3\nprompt : ask Staff P to do it\nThanks\n"
    assert split_prompt_lines(body) == (
        "ask Staff P to do it",
        "make a financial report for q3\nThanks",
    )
    two = "PROMPT: assign to Staff P\nPROMPT inject reviewer Staff D\n\nTolong dicek"
    assert split_prompt_lines(two) == ("assign to Staff P\nreviewer Staff D", "Tolong dicek")
    for text in ("Prompt action is required by Friday", "Tolong dicek\nNo prompt here"):
        assert split_prompt_lines(text) == ("", text)


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("ask Staff P to do it and the deadline is this friday", "Staff P"),
        ("ask staff p to do this deadline this friday", "staff p"),
        ("minta Staff P untuk siapkan laporannya", "Staff P"),
        ("suruh Staff P buat laporan", "Staff P"),
        ("ask the bank to confirm", "the bank"),  # matched against Staff later, then flagged
    ],
)
def test_ask_someone_to(text, name):
    assert assignee(text) == name


@pytest.mark.parametrize(
    ("text", "today", "due"),
    [
        ("the deadline is this friday", MONDAY, date(2026, 10, 30)),
        ("deadline friday", MONDAY, date(2026, 10, 30)),
        ("paling lambat jumat ini", MONDAY, date(2026, 10, 30)),
        ("deadline next friday", MONDAY, date(2026, 11, 6)),
        ("deadline friday next week", MONDAY, date(2026, 11, 6)),
        ("deadline jumat depan", MONDAY, date(2026, 11, 6)),
        ("by wednesday", MONDAY, date(2026, 10, 28)),
        ("by monday", MONDAY, date(2026, 11, 2)),  # said on a Monday: the next one
        ("deadline this monday", MONDAY, MONDAY),
        ("deadline this friday", date(2026, 10, 30), date(2026, 10, 30)),
        ("this is the friday report", MONDAY, None),  # no deadline word: not a deadline
    ],
)
def test_weekday_deadlines(text, today, due):
    assert deadline(text, today)[0] == due
