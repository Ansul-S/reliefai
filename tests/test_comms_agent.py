"""
Tests for the SMS limit in the communications agent.

The prompt asks the model to stay under the limit; these check the code that
makes it true, since the README's claim that SMS limits are enforced used to
rest on the prompt alone.
"""
from __future__ import annotations

import json

import database.crud as crud
from agents.comms_agent import (
    SMS_GSM7_LIMIT,
    SMS_UCS2_LIMIT,
    fit_sms,
    run_comms_agent,
    sms_units,
)
from tests.conftest import groq_response

LONG_ENGLISH = " ".join(
    ["Evacuation centre open at Rizal High School, bring water and medicine."] * 4
)


def test_plain_latin_text_is_gsm7_with_a_160_limit():
    assert sms_units("Shelter open now.") == (17, SMS_GSM7_LIMIT, "GSM-7")


def test_filipino_accents_stay_in_gsm7():
    """ñ is in the GSM-7 basic set; Filipino SMS keep the 160 limit."""
    assert sms_units("Bukas na ang silungan sa Parañaque.")[2] == "GSM-7"


def test_extended_characters_cost_two_units():
    assert sms_units("[OK]")[0] == 6


def test_one_non_gsm_character_switches_the_whole_message_to_ucs2():
    """Hindi, Tamil and Japanese all fit 70, not 160 — as does an em dash."""
    for text in ("नमस्ते", "வணக்கம்", "避難所", "Open now — come"):
        assert sms_units(text)[1:] == (SMS_UCS2_LIMIT, "UCS-2"), text


def test_a_message_that_fits_is_left_alone():
    text, check = fit_sms("Shelter open now.")
    assert text == "Shelter open now."
    assert check["trimmed"] is False


def test_an_overlong_message_is_cut_at_a_word_and_marked():
    text, check = fit_sms(LONG_ENGLISH)
    assert check["trimmed"] is True
    assert check["length"] <= SMS_GSM7_LIMIT
    assert check["original_length"] > SMS_GSM7_LIMIT
    assert text.endswith("...")
    # Cut at a word boundary, not mid-word.
    assert LONG_ENGLISH.startswith(text[:-3])
    assert LONG_ENGLISH[len(text) - 3] == " "


def test_the_trim_marker_does_not_itself_force_ucs2():
    """"…" is not GSM-7; using it would shrink the limit from 160 to 70."""
    _, check = fit_sms(LONG_ENGLISH)
    assert check["encoding"] == "GSM-7"


def test_unspaced_scripts_are_cut_by_character():
    text, check = fit_sms("避難所が開設されました" * 10)
    assert check["trimmed"] is True
    assert check["length"] <= SMS_UCS2_LIMIT


async def test_the_saved_broadcast_carries_the_enforced_sms(db, seeded_disaster, fake_groq):
    did = seeded_disaster["disaster_id"]
    fake_groq(groq_response(json.dumps({
        "original_message": "Evacuation centre open",
        "translations": {"en": "Evacuation centre open at Rizal High School."},
        "sms_versions": {"en": LONG_ENGLISH, "tl": "Bukas na ang evacuation center."},
        "channel_recommendation": "sms",
        "broadcast_id": 0,
    })))

    result = await run_comms_agent(
        did, message="Evacuation centre open", languages=["en", "tl"],
        channel="sms", db=db,
    )

    assert result["sms_checks"]["en"]["trimmed"] is True
    assert result["sms_checks"]["tl"]["trimmed"] is False
    assert sms_units(result["sms_versions"]["en"])[0] <= SMS_GSM7_LIMIT

    saved = await crud.get_broadcast(db, result["broadcast_id"])
    assert saved.message_translations["sms"]["en"] == result["sms_versions"]["en"]
