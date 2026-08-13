"""F1 -- the date resolution engine (stage 4).

Every test here constructs ``RawConflict``s directly, so nothing in this file
touches the model, the gateway, or a fixture. Resolution is pure arithmetic
against ``run.today``; that is the whole point of Technique 2.

Calendar anchor used throughout: **Mon 17 Aug 2026**.

    Aug 2026   Mo 17  Tu 18  We 19  Th 20  Fr 21  [Sa 22  Su 23]
               Mo 24  Tu 25  We 26  Th 27  Fr 28  [Sa 29  Su 30]

So a 10-business-day window from Mon 17 Aug is
17, 18, 19, 20, 21, 24, 25, 26, 27, 28 -- weekends absent.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from meeting_deconflictor.dates import business_days, resolve
from meeting_deconflictor.models import Interval, RunInput
from meeting_deconflictor.schema import RawConflict

MONDAY = date(2026, 8, 17)

#: The 10 business days of the reference window, for readability in assertions.
WINDOW_10 = [
    date(2026, 8, 17),
    date(2026, 8, 18),
    date(2026, 8, 19),
    date(2026, 8, 20),
    date(2026, 8, 21),
    date(2026, 8, 24),
    date(2026, 8, 25),
    date(2026, 8, 26),
    date(2026, 8, 27),
    date(2026, 8, 28),
]


def make_run(*, today: date = MONDAY, window: int = 10, duration: int = 60) -> RunInput:
    """A RunInput with no messages -- stage 4 never reads them."""
    return RunInput(
        messages=(),
        today=today,
        window_business_days=window,
        duration_minutes=duration,
        required=("Wei", "Aisyah"),
        optional=("Ravi", "Priya"),
    )


def raw(
    day_reference: str,
    *,
    speaker: str = "Wei",
    polarity: str = "busy",
    time_start: str | None = None,
    time_end: str | None = None,
    hardness: str = "hard",
    quote: str = "a verbatim quote",
    unparseable: bool = False,
) -> RawConflict:
    """One extraction row, as the model would emit it."""
    return RawConflict(
        speaker=speaker,
        polarity=polarity,
        day_reference=day_reference,
        time_start=time_start,
        time_end=time_end,
        hardness=hardness,
        quote=quote,
        unparseable=unparseable,
    )


def dates_of(intervals: tuple[Interval, ...]) -> list[date]:
    return [i.start.date() for i in intervals]


def spans_of(intervals: tuple[Interval, ...]) -> set[tuple[time, time]]:
    return {(i.start.time(), i.end.time()) for i in intervals}


# --------------------------------------------------------------------------
# The window itself
# --------------------------------------------------------------------------


def test_business_days_skips_weekends():
    assert business_days(make_run()) == WINDOW_10


def test_business_days_starting_on_a_friday_jumps_the_weekend():
    friday = date(2026, 8, 21)
    assert business_days(make_run(today=friday, window=3)) == [
        date(2026, 8, 21),
        date(2026, 8, 24),
        date(2026, 8, 25),
    ]


# --------------------------------------------------------------------------
# Recurrence -- the acceptance check for F1
# --------------------------------------------------------------------------


def test_daily_standup_expands_to_every_business_day():
    """TEAM_PLAN F1 acceptance check.

    "standups every morning till 10" over a 10-business-day window is 10
    intervals of 09:00-10:00, one per business day, with no Saturday or Sunday.
    """
    conflicts, unresolved = resolve(
        [raw("every morning", time_end="10:00", quote="standups every morning till 10")],
        make_run(),
    )

    assert unresolved == []
    assert len(conflicts) == 1
    intervals = conflicts[0].intervals
    assert len(intervals) == 10
    assert dates_of(intervals) == WINDOW_10
    assert spans_of(intervals) == {(time(9, 0), time(10, 0))}
    assert all(i.start.weekday() < 5 for i in intervals)


def test_recurrence_bound_written_into_the_day_reference_still_ends_at_ten():
    """The model may leave "till 10" inside the verbatim day reference."""
    conflicts, unresolved = resolve([raw("every morning till 10")], make_run())

    assert unresolved == []
    assert len(conflicts[0].intervals) == 10
    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(10, 0))}


def test_daily_covers_the_whole_business_day_when_no_time_is_given():
    conflicts, _ = resolve([raw("daily")], make_run())

    assert len(conflicts[0].intervals) == 10
    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(18, 0))}


def test_each_tuesday_expands_to_only_the_tuesdays_in_the_window():
    conflicts, unresolved = resolve([raw("each Tuesday")], make_run())

    assert unresolved == []
    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 18), date(2026, 8, 25)]


def test_recurring_lunch_is_noon_to_one_on_every_business_day():
    conflicts, _ = resolve(
        [raw("every day", hardness="soft", quote="avoid lunch hour pls")], make_run()
    )
    daily = conflicts[0]
    assert daily.hardness == "soft"

    conflicts, _ = resolve([raw("lunch hour every day", hardness="soft")], make_run())
    assert len(conflicts[0].intervals) == 10
    assert spans_of(conflicts[0].intervals) == {(time(12, 0), time(13, 0))}


# --------------------------------------------------------------------------
# Dated references
# --------------------------------------------------------------------------


def test_weekday_plus_ordinal_resolves_to_that_single_date():
    conflicts, unresolved = resolve([raw("Thu 20th", quote="I'm out Thu 20th")], make_run())

    assert unresolved == []
    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 20)]
    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(18, 0))}


def test_bare_ordinal_resolves_to_that_day_of_the_month():
    conflicts, _ = resolve([raw("the 21st")], make_run())

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 21)]


def test_bare_weekday_is_the_next_occurrence_not_every_occurrence():
    conflicts, _ = resolve([raw("Wednesday")], make_run())

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 19)]


def test_weekday_that_contradicts_the_ordinal_is_unresolved():
    """20 Aug 2026 is a Thursday, so "Fri 20th" is a reference we must not guess at."""
    conflicts, unresolved = resolve([raw("Fri 20th")], make_run())

    assert conflicts == []
    assert len(unresolved) == 1
    assert "Fri 20th" in unresolved[0].reason


def test_month_name_is_honoured():
    conflicts, _ = resolve([raw("Mon 24 Aug")], make_run())

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 24)]


# --------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------


def test_leave_till_the_21st_covers_every_business_day_through_that_friday():
    conflicts, unresolved = resolve(
        [raw("till the 21st", speaker="Ravi", quote="on leave till the 21st")], make_run()
    )

    assert unresolved == []
    assert dates_of(conflicts[0].intervals) == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
    ]
    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(18, 0))}


def test_through_a_weekday_is_inclusive_of_that_weekday():
    conflicts, _ = resolve([raw("through Wednesday")], make_run())

    assert dates_of(conflicts[0].intervals) == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
    ]


def test_range_between_two_dates_keeps_only_the_business_days():
    conflicts, _ = resolve([raw("20th to 25th")], make_run())

    assert dates_of(conflicts[0].intervals) == [
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 24),
        date(2026, 8, 25),
    ]


# --------------------------------------------------------------------------
# Relative references
# --------------------------------------------------------------------------


def test_tomorrow_is_the_next_day():
    conflicts, _ = resolve([raw("tomorrow")], make_run())

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 18)]


def test_next_week_is_the_five_business_days_of_the_following_week():
    conflicts, unresolved = resolve([raw("next week")], make_run())

    assert unresolved == []
    assert dates_of(conflicts[0].intervals) == [
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
        date(2026, 8, 27),
        date(2026, 8, 28),
    ]


def test_next_weekday_skips_to_the_following_week():
    conflicts, _ = resolve([raw("next Monday")], make_run())

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 24)]


# --------------------------------------------------------------------------
# Part-of-day defaults
# --------------------------------------------------------------------------


def test_afternoon_defaults_to_noon_till_close_of_business():
    conflicts, _ = resolve(
        [raw("Wed afternoon", speaker="Aisyah", quote="client call Wed afternoon")], make_run()
    )

    assert dates_of(conflicts[0].intervals) == [date(2026, 8, 19)]
    assert spans_of(conflicts[0].intervals) == {(time(12, 0), time(18, 0))}


def test_morning_defaults_to_nine_till_noon():
    conflicts, _ = resolve([raw("Tuesday morning")], make_run())

    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(12, 0))}


def test_explicit_times_override_the_part_of_day_default():
    conflicts, _ = resolve(
        [raw("Wed afternoon", time_start="13:00", time_end="15:30")], make_run()
    )

    assert spans_of(conflicts[0].intervals) == {(time(13, 0), time(15, 30))}


def test_times_outside_business_hours_are_clamped_to_the_business_day():
    conflicts, _ = resolve([raw("Tuesday", time_start="07:00", time_end="20:00")], make_run())

    assert spans_of(conflicts[0].intervals) == {(time(9, 0), time(18, 0))}


# --------------------------------------------------------------------------
# Abstention -- never a guess
# --------------------------------------------------------------------------


def test_calendar_relative_reference_is_unresolved_never_guessed():
    conflicts, unresolved = resolve(
        [
            raw(
                "after the sprint review",
                speaker="Priya",
                polarity="free",
                quote="any time after the sprint review",
            )
        ],
        make_run(),
    )

    assert conflicts == []
    assert len(unresolved) == 1
    assert unresolved[0].speaker == "Priya"
    assert unresolved[0].quote == "any time after the sprint review"


def test_weekend_reference_is_reported_rather_than_silently_dropped():
    conflicts, unresolved = resolve([raw("Saturday")], make_run())

    assert conflicts == []
    assert len(unresolved) == 1
    assert "business" in unresolved[0].reason


def test_date_outside_the_window_is_reported_rather_than_silently_dropped():
    conflicts, unresolved = resolve([raw("Thu 20th")], make_run(window=1))

    assert conflicts == []
    assert len(unresolved) == 1
    assert "window" in unresolved[0].reason


def test_inverted_time_range_is_unresolved():
    conflicts, unresolved = resolve(
        [raw("Tuesday", time_start="15:00", time_end="11:00")], make_run()
    )

    assert conflicts == []
    assert len(unresolved) == 1


def test_a_free_statement_never_becomes_a_conflict():
    conflicts, unresolved = resolve([raw("Tuesday", polarity="free")], make_run())

    assert conflicts == []
    assert unresolved == []


# --------------------------------------------------------------------------
# Contract preservation
# --------------------------------------------------------------------------


def test_speaker_hardness_and_quote_are_carried_through_untouched():
    conflicts, _ = resolve(
        [
            raw(
                "every morning",
                speaker="Wei",
                hardness="soft",
                quote="standups every morning till 10",
                time_end="10:00",
            )
        ],
        make_run(),
    )

    assert conflicts[0].speaker == "Wei"
    assert conflicts[0].hardness == "soft"
    assert conflicts[0].quote == "standups every morning till 10"


def test_intervals_are_half_open_and_chronological():
    conflicts, _ = resolve([raw("daily")], make_run())
    intervals = conflicts[0].intervals

    assert list(intervals) == sorted(intervals)
    # Half-open: a 09:00-18:00 interval does not overlap the next day's 09:00 start.
    assert not intervals[0].overlaps(intervals[1])


def test_resolution_is_deterministic():
    rows = [raw("every morning", time_end="10:00"), raw("till the 21st", speaker="Ravi")]
    first = resolve(rows, make_run())
    assert all(resolve(rows, make_run()) == first for _ in range(3))


@pytest.mark.parametrize(
    "reference",
    ["", "sometime", "when the dust settles", "after standup", "the usual slot"],
)
def test_unrecognised_references_abstain_instead_of_defaulting(reference: str):
    conflicts, unresolved = resolve([raw(reference)], make_run())

    assert conflicts == []
    assert len(unresolved) == 1
