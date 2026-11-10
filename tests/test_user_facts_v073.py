"""User-fact extractor v0.7.3 — third-person PERSON, shows, interviews,
job-search STATUS, trip supersede + stay expiry. Synthetic text only."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

from dream_cycle.config import default_config
from dream_cycle.propose import propose
from dream_cycle.user_facts import (
    extract_from_text,
    extract_user_facts,
    normalize_date,
    parse_date_range,
)

REF = date(2026, 11, 9)


def _cats(facts, cat):
    return [f for f in facts if f.category == cat]


# ---- PERSON ----------------------------------------------------------------

def test_person_third_person_role_capitalized():
    f = extract_from_text("Maya is a teacher, which is a second career.", ref_date=REF)
    p = _cats(f, "PERSON")
    assert p and p[0].summary == "Maya (teacher)"
    assert p[0].proposed_line == "- PERSON: Maya — teacher"


def test_person_role_in_my_household():
    f = extract_from_text("Maya is a teacher in my family.", ref_date=REF)
    assert any("Maya" in x.summary and "teacher" in x.summary for x in _cats(f, "PERSON"))


def test_person_currently_a_role():
    f = extract_from_text("Maya is currently a teacher.", ref_date=REF)
    assert _cats(f, "PERSON")


def test_person_is_our_role():
    f = extract_from_text("Jordan is our nurse.", ref_date=REF)
    assert any(x.summary == "Jordan (nurse)" for x in _cats(f, "PERSON"))


def test_person_my_wife_name_mixed_case_rel():
    f = extract_from_text("My wife Jordan is the manager of the team.", ref_date=REF)
    assert any(x.summary == "Jordan (wife)" for x in _cats(f, "PERSON"))


def test_person_occupation_lowercase_role():
    f = extract_from_text("Riley is a nurse.", ref_date=REF)
    assert any(x.summary == "Riley (nurse)" for x in _cats(f, "PERSON"))


def test_regression_my_dad_makes_is_not_a_person():
    # Musk false positive in battery 2: "My dad makes" -> PERSON 'makes - dad'
    assert _cats(extract_from_text("My dad makes the best chili in town.", ref_date=REF), "PERSON") == []
    assert _cats(extract_from_text("my dad makes furniture", ref_date=REF), "PERSON") == []
    assert _cats(extract_from_text("My mom says hi.", ref_date=REF), "PERSON") == []


def test_person_negatives_common_nouns_and_pronouns():
    for text in (
        "Python is a language.",
        "This is a test.",
        "It is a problem.",
        "She is a nurse.",
        "Phoenix is a city.",
        "Today is a holiday.",
    ):
        assert _cats(extract_from_text(text, ref_date=REF), "PERSON") == [], text


def test_person_question_still_skipped():
    assert extract_from_text("Who is Alexa?", ref_date=REF) == []


# ---- EVENT: shows ----------------------------------------------------------

SHOW_BRIEF = (
    "I'm prepping for a regional show in June.\n"
    "Prepared 2026-11-09 by Coach. Data through 2026-11-03.\n"
    "Goal\n"
    "Open Bodybuilding, Masters 40+.\n"
    "Primary target: Harbor Classic, Sat June 12, 2028.\n"
    "Fallback: Bay State Open, Sep 20, 2028. Newest option: River City Classic, Jul 18, 2028.\n"
)


def test_event_shows_primary_and_fallbacks():
    f = extract_from_text(SHOW_BRIEF, ref_date=REF)
    ev = {x.summary for x in _cats(f, "EVENT")}
    assert "Harbor Classic on 2028-06-12" in ev
    assert "Bay State Open on 2028-09-20" in ev
    assert "River City Classic on 2028-07-18" in ev
    # metadata dates are not events
    assert not any("2026-11-09" in s or "2026-11-03" in s for s in ev)
    vos = next(x for x in _cats(f, "EVENT") if x.summary.startswith("Harbor"))
    assert vos.expires == "2028-06-13"
    assert vos.target == "MEMORY.md" and vos.proposed_line.startswith("STATUS:")


def test_event_unlabeled_npc_show_date():
    f = extract_from_text("I'm competing in the NPC Desert Classic show May 8 2027.", ref_date=REF)
    ev = _cats(f, "EVENT")
    assert ev and "2027-05-08" in ev[0].summary
    assert ev[0].expires == "2027-05-09"


# ---- EVENT: interviews -----------------------------------------------------

def test_event_interview_tue_mdy():
    body = (
        "1 panel: Acme Trust R12345, Sr Principal Architect. "
        "Technical round Tue 11/10, 12:00-12:30 PM Pacific time, Teams."
    )
    f = extract_from_text(body, ref_date=REF)
    ev = _cats(f, "EVENT")
    assert ev, f
    assert "2026-11-10" in ev[0].summary
    assert "Acme Trust" in ev[0].summary
    assert ev[0].expires == "2026-11-11"


def test_event_interview_simple():
    f = extract_from_text("I have an interview Tue 11/10 with Globex.", ref_date=REF)
    ev = _cats(f, "EVENT")
    assert ev and "2026-11-10" in ev[0].summary


def test_interview_without_date_skipped():
    assert _cats(extract_from_text("I have an interview soon.", ref_date=REF), "EVENT") == []


# ---- STATUS: job search ----------------------------------------------------

def test_status_employed_and_searching():
    f = extract_from_text(
        "I'm gainfully employed in a decent job, but looking for a new one. "
        "My current job hunting status is below.",
        ref_date=REF,
    )
    st = _cats(f, "STATUS")
    assert st and st[0].summary == "employed, actively searching"
    assert st[0].target == "USER.md"


def test_status_actively_searching_only():
    f = extract_from_text("I'm actively searching for a director role.", ref_date=REF)
    assert any(x.summary == "actively searching" for x in _cats(f, "STATUS"))


def test_status_job_search_negatives():
    assert _cats(extract_from_text("My friend is looking for a new job.", ref_date=REF), "STATUS") == []
    assert _cats(extract_from_text("Is he employed?", ref_date=REF), "STATUS") == []
    assert _cats(extract_from_text("I'm employed.", ref_date=REF), "STATUS") == []


# ---- EVENT: trips, ranges, supersede ---------------------------------------

def test_parse_date_range_forms():
    assert parse_date_range("Itinerary (Nov 19–22, 2026)", REF) == (date(2026, 11, 19), date(2026, 11, 22))
    assert parse_date_range("Hotel Thu 11/19 to Sun 11/22.", REF) == (date(2026, 11, 19), date(2026, 11, 22))
    assert parse_date_range("arriving 11/19 and staying through 11/22", REF) == (
        date(2026, 11, 19), date(2026, 11, 22))
    assert parse_date_range("Prepared Nov 9, 2026 only.", REF) is None


def test_trip_expiry_end_of_stay():
    body = (
        "I'm traveling to Vegas for F1. Here is the itinerary.\n"
        "Las Vegas F1 Weekend: Proposed Itinerary (Nov 19–22, 2026)\n"
        "Prepared Nov 9, 2026 from the trip log."
    )
    f = extract_from_text(body, ref_date=REF)
    ev = [x for x in _cats(f, "EVENT") if x.key.startswith("event:trip:")]
    assert len(ev) == 1
    assert ev[0].summary == "trip to Las Vegas on 2026-11-19"
    assert ev[0].expires == "2026-11-22"
    assert "revisit after 2026-11-22" in ev[0].proposed_line
    assert ev[0].precision == "explicit"


def test_trip_through_date():
    f = extract_from_text("I'm going to Vegas on 11/19 through 11/22.", ref_date=REF)
    ev = _cats(f, "EVENT")
    assert ev[0].expires == "2026-11-22"


def test_trip_single_date_keeps_plus_one():
    f = extract_from_text("I'm taking a trip to Las Vegas on Nov 19.", ref_date=REF)
    assert f[0].expires == "2026-11-20"


def test_trip_supersede_approx_with_explicit():
    t0 = time.mktime(datetime(2026, 11, 9, 9, 0).timetuple())
    t1 = time.mktime(datetime(2026, 11, 9, 19, 0).timetuple())
    msgs = [
        {"role": "user", "message_id": 1, "session_id": "a", "timestamp": t0,
         "content": "I'm going to Vegas for the F1 race in a week or so. What weather?"},
        {"role": "user", "message_id": 2, "session_id": "b", "timestamp": t1,
         "content": "I'm traveling to Vegas for F1. Itinerary (Nov 19–22, 2026)."},
    ]
    facts = extract_user_facts(msgs, ref_date=REF)
    trips = [f for f in facts if f.key.startswith("event:trip:")]
    assert len(trips) == 1
    assert trips[0].summary == "trip to Las Vegas on 2026-11-19"
    assert trips[0].expires == "2026-11-22"


def test_trip_supersede_order_independent():
    t0 = time.mktime(datetime(2026, 11, 9, 9, 0).timetuple())
    t1 = time.mktime(datetime(2026, 11, 9, 19, 0).timetuple())
    msgs = [
        {"role": "user", "message_id": 2, "session_id": "b", "timestamp": t1,
         "content": "I'm traveling to Vegas for F1. Itinerary (Nov 19–22, 2026)."},
        {"role": "user", "message_id": 1, "session_id": "a", "timestamp": t0,
         "content": "I'm going to Vegas for the F1 race in a week or so."},
    ]
    trips = [f for f in extract_user_facts(msgs, ref_date=REF) if f.key.startswith("event:trip:")]
    assert len(trips) == 1 and trips[0].expires == "2026-11-22"


def test_different_trips_far_apart_both_kept():
    msgs = [
        {"role": "user", "content": "I'm going to Vegas on Nov 19.", "message_id": 1},
        {"role": "user", "content": "I'm going to Vegas on 2027-03-10.", "message_id": 2},
    ]
    trips = [f for f in extract_user_facts(msgs, ref_date=REF) if f.key.startswith("event:trip:")]
    assert len(trips) == 2


def test_different_places_both_kept():
    msgs = [
        {"role": "user", "content": "I'm going to Vegas on Nov 19.", "message_id": 1},
        {"role": "user", "content": "I'm going to Denver on Nov 25.", "message_id": 2},
    ]
    trips = [f for f in extract_user_facts(msgs, ref_date=REF) if f.key.startswith("event:trip:")]
    assert len(trips) == 2


# ---- full brief (synthetic household) + propose integration ---------------

HOUSEHOLD = (
    "There is a brief below. Read it, then make some "
    "suggestions on gifts for Jordan and Maya:\n\n"
    "My wife Jordan is a nurse. Maya is a teacher, which is a "
    "second career. Maya is currently a teacher. Jordan is at the top."
)


def test_household_brief_catches_both():
    f = extract_from_text(HOUSEHOLD, ref_date=REF)
    names = {x.summary for x in _cats(f, "PERSON")}
    assert "Jordan (wife)" in names
    assert "Maya (teacher)" in names
    # "Jordan is at the top" must not become a PERSON
    assert not any("top" in n for n in names)


def test_propose_stages_one_vegas_trip(tmp_path: Path):
    home = tmp_path / "home"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("# mem\n")
    (home / "memories" / "USER.md").write_text("# user\n")
    (home / "plugin-data" / "dream-cycle").mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)")
    t0 = time.mktime(datetime(2026, 11, 9, 9, 0).timetuple())
    t1 = time.mktime(datetime(2026, 11, 9, 19, 0).timetuple())
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                ("a", "user", "I'm going to Vegas for the F1 race in a week or so.", t0))
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                ("b", "user", "I'm traveling to Vegas for F1. Itinerary (Nov 19–22, 2026).", t1))
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                ("c", "assistant", "Maya is a teacher.", t1))
    con.commit(); con.close()
    cfg = default_config()
    cfg["session_corroboration"]["enabled"] = False
    r = propose(home, config=cfg, run_date=date(2026, 11, 10), session_date=date(2026, 11, 9))
    trips = [p for p in r.proposals if p.kind == "user_fact" and p.category == "EVENT"]
    assert len(trips) == 1
    assert trips[0].values[0] == "trip to Las Vegas on 2026-11-19"
    assert trips[0].expires == "2026-11-22"
    # assistant text never yields PERSON
    assert not [p for p in r.proposals if p.category == "PERSON"]


def test_slash_ratio_and_fashion_show_not_events():
    assert normalize_date("stay open 24/7", REF) is None
    f = extract_from_text(
        "Honey Birdette and Fashion Show: re-check hours. Bridges stay open 24/7.",
        ref_date=REF,
    )
    assert _cats(f, "EVENT") == []


def test_dexa_rescan_not_a_show():
    body = (
        "roughly 25 lb of fat still to lose before a May show.\n"
        "Rescan DEXA around 12/10, then every 6-8 weeks.\n"
    )
    ev = [x for x in extract_from_text(body, ref_date=REF) if "12/10" in x.summary or "2026-12-10" in x.summary]
    assert ev == []


def test_past_recruiter_screen_not_staged():
    body = (
        "1 screen: KPMG Director Harness Engineer 139524 "
        "(recruiter screen 10/29, no newer signal)."
    )
    ev = _cats(extract_from_text(body, ref_date=REF), "EVENT")
    assert ev == []


def test_upcoming_interview_still_staged():
    f = extract_from_text("Technical round Tue 11/10 at Acme Trust.", ref_date=REF)
    ev = _cats(f, "EVENT")
    assert ev and "2026-11-10" in ev[0].summary


def test_two_day_show_merges():
    body = (
        "NPC Masters USA (Avis) South Point. Fri 11/20 women's divisions. "
        "Sat 11/21 men's divisions, finals 4 PM."
    )
    ev = [f for f in extract_from_text(body, ref_date=REF) if "Masters" in f.summary]
    assert len(ev) == 1
    assert ev[0].summary.startswith("NPC Masters USA")
    assert "2026-11-20" in ev[0].summary
    assert ev[0].expires == "2026-11-22"


def test_article_headlines_are_not_shows():
    for body in (
        "Article text: Alphabet Expands Stake and Deepens AI Ties Sat, Nov. 7, 2026.",
        "[12] Michael Burry Is Still Short Palantir. Here's What Its Nov. 2 Report Has to Show.",
        "Article text: Published on 2026-10-16 — Space Stocks Slide.",
        "Run consolidate.py --dry-run for 2026-11-03 and 2026-11-04. Paste the candidates.",
    ):
        ev = _cats(extract_from_text(body, ref_date=REF), "EVENT")
        assert ev == [], (body, ev)


def test_old_message_past_event_dropped_on_later_dream_day():
    # Message dated 10/16 mentions a same-day event; dreaming on 11/09 must not restage it.
    facts = extract_user_facts(
        [{"role": "user", "content": "Technical round 10/16 at Globex.",
          "timestamp": "2026-10-16T12:00:00", "message_id": 1}],
        ref_date=REF,
    )
    assert _cats(facts, "EVENT") == []
