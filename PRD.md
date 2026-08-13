# PRD — Meeting Deconflictor

**One line:** Paste what everyone said about their week, get one meeting slot that collides with nothing.

## Problem

Scheduling an internal meeting across four or five people means reading a scattered set of
statements — "standups every morning till 10", "client call Wed afternoon", "I'm on leave
till the 21st", "avoid lunch pls" — and finding a contiguous block that breaks none of
them. Someone does this by hand, gets it slightly wrong, and the meeting gets moved twice.

The app does the reading and the arithmetic. It proposes; the group confirms. It never
books anything.

**Assumptions:** single timezone; business hours Mon–Fri 09:00–18:00; 30-minute
granularity; no calendar access; up to 25 messages per run.

## Input → output path

1. **Input:** pasted messages, plus today's date, a window (default: next 10 business
   days), meeting duration, and which attendees are required vs optional.
2. **One LLM call — extraction only.** Each message → strict JSON: speaker,
   `busy` / `free`, day reference *as written*, time range, `hard` / `soft`, and the
   verbatim quote it came from. Anything it cannot resolve → `unparseable`.
3. **Code resolves dates.** Relative and recurring references ("every morning",
   "Thu 20th", "till the 21st") are mapped to real business dates against today's date.
   The model never does this.
4. **Code finds slots.** Contiguous blocks of the requested duration. Hard conflicts
   exclude; soft conflicts deduct. Ranking, in strict order: all required attendees free →
   most optional attendees free → earliest.
5. **Output:** top 3 dated slots with attendee lists, an echo of every conflict it thinks
   each person declared, and a "needs confirmation" list of anything unresolved.

## Example

**Input** — today = Mon 17 Aug 2026, window = 10 business days, duration = 60 min,
required = Wei + Aisyah, optional = Ravi + Priya

```
Wei: standups every morning till 10, and I'm out Thu 20th
Aisyah: client call Wed afternoon, avoid lunch hour pls
Ravi: on leave till the 21st
Priya: any time after the sprint review
```

**Output**

```
1. Mon 24 Aug, 10:00–11:00  — Wei, Aisyah, Ravi        (both required free, 1 of 2 optional)
2. Tue 25 Aug, 10:00–11:00  — Wei, Aisyah, Ravi
3. Wed 26 Aug, 10:00–11:00  — Wei, Aisyah, Ravi

Conflicts read from the thread:
  Wei     — daily 09:00–10:00 (hard) · all day Thu 20 Aug (hard)
  Aisyah  — Wed 19 Aug 13:00–18:00 (hard) · daily 12:00–13:00 (soft)
  Ravi    — through Fri 21 Aug (hard)

Needs confirmation:
  Priya — "after the sprint review" (refers to a calendar I can't see)
```

## Acceptance criteria

1. **Zero collisions.** No proposed slot overlaps any declared hard conflict, checked in
   code across the whole golden set.
2. On a 15-message hand-labelled golden set: ≥90% exact match on speaker, polarity and
   hardness; ≥85% on resolved date and time range.
3. **Zero invented conflicts.** Every extracted conflict traces to a verbatim quote in the
   input, and all of them appear in the echo block.
4. **Zero silent drops.** Anything unresolved appears in needs-confirmation. An unheard
   person is never treated as a free person.
5. Top-ranked slot matches the hand-computed answer on all three test logs.
6. 25 messages processed in under 30 seconds, one LLM call.

## Test examples

| # | Input | Must produce |
|---|---|---|
| T1 | 5 clean messages, one obvious gap | Correct single slot, empty needs-confirmation list |
| T2 | Recurring + dated conflicts — "every morning till 10", "out Thu 20th", "till the 21st" | Correct business dates; recurring conflict applied to every day in the window, not just one |
| T3 | No block where both required attendees are free; one calendar-relative message | States no clean slot exists, names the nearest option and exactly whose conflict it breaks, flags the unresolvable message |

## Non-goals

No calendar integration — not Outlook, not Google, not free/busy lookup. No sending invites
or holds. No room or resource booking. No timezone handling. No recurring meeting series.
No seniority or priority weighting. No agenda, no notes, no reminders. No handling of more
than 25 messages. It does not judge whether the meeting is needed, and it does not message
anyone.

## Course techniques

1. **Structured output to a strict JSON schema** — extraction is the model's only job, and
   the schema is what stops it from proposing an answer directly.
2. **Deterministic post-processing** — date resolution, contiguity and collision checks run
   in code, so the deconflict guarantee is provable rather than trusted.
3. **Golden-set eval with required abstention** — 15 labelled messages, where flagging a
   genuinely unresolvable message scores as correct and guessing scores as wrong.
