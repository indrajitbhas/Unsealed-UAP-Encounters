"""Neutral entity extraction — dates, places, agencies, roles ONLY.

Hard rule: zero interpretation. We surface *literal* strings matched by
conservative patterns/gazetteers. No sentiment, no summarisation, no
inference about what a document "means". Downstream analysts do that; the
probe only demonstrates that these fields are mechanically extractable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List


# --------------------------------------------------------------------------- #
# Dates — a few unambiguous, literal formats.
# --------------------------------------------------------------------------- #

_MONTHS = (r"January|February|March|April|May|June|July|August|September|"
           r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")

_DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                       # 1975-07-14
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}\b", re.I),  # July 14, 1975
    re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\.?\s+\d{{4}}\b", re.I),    # 14 July 1975
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                 # 07/14/1975
]

# --------------------------------------------------------------------------- #
# Agencies — conservative gazetteer of literal agency names/acronyms.
# --------------------------------------------------------------------------- #

_AGENCIES = [
    "Department of Defense", "Department of Defense (DoD)", "DoD",
    "Department of the Air Force", "Department of the Navy",
    "Department of the Army", "Department of State", "Department of Energy",
    "Central Intelligence Agency", "CIA",
    "Federal Bureau of Investigation", "FBI",
    "National Security Agency", "NSA",
    "Defense Intelligence Agency", "DIA",
    "National Reconnaissance Office", "NRO",
    "National Aeronautics and Space Administration", "NASA",
    "Federal Aviation Administration", "FAA",
    "Office of Naval Intelligence", "ONI",
    "All-domain Anomaly Resolution Office", "AARO",
    "Advanced Aerospace Threat Identification Program", "AATIP",
    "United States Air Force", "U.S. Air Force", "US Air Force", "USAF",
    "United States Navy", "U.S. Navy", "US Navy", "USN",
    "Joint Chiefs of Staff", "North American Aerospace Defense Command",
    "NORAD", "Strategic Air Command", "SAC",
]

# --------------------------------------------------------------------------- #
# Roles — literal titles frequently present in declassified memos.
# --------------------------------------------------------------------------- #

_ROLES = [
    "Secretary of Defense", "Secretary of the Air Force", "Secretary of the Navy",
    "Director", "Deputy Director", "Assistant Secretary",
    "Intelligence Officer", "Commanding Officer", "Commanding General",
    "Chief of Staff", "General", "Colonel", "Lieutenant Colonel", "Lt. Col.",
    "Major", "Captain", "Lieutenant", "Commander", "Admiral", "Brigadier General",
    "Special Agent", "Analyst", "Project Officer", "Program Manager",
]

# --------------------------------------------------------------------------- #
# Places — US states + common base/place indicators. This is deliberately
# conservative: we match "<Proper Noun...> AFB/Air Force Base/Base" and a US
# state gazetteer. No geocoding, no disambiguation.
# --------------------------------------------------------------------------- #

_US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]

_BASE_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3}\s+"
    r"(?:AFB|Air Force Base|Naval Air Station|NAS|Army Base|Air Station|Airfield))\b"
)


def _compile_gazetteer(terms: List[str]) -> re.Pattern:
    # Longest-first so "Department of the Air Force" wins over "Department".
    ordered = sorted(set(terms), key=len, reverse=True)
    escaped = [re.escape(t) for t in ordered]
    return re.compile(r"\b(" + "|".join(escaped) + r")\b")


_AGENCY_RE = _compile_gazetteer(_AGENCIES)
_ROLE_RE = _compile_gazetteer(_ROLES)
_STATE_RE = _compile_gazetteer(_US_STATES)


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        k = it.strip()
        low = k.lower()
        if k and low not in seen:
            seen.add(low)
            out.append(k)
    return out


@dataclass
class Entities:
    dates: List[str] = field(default_factory=list)
    places: List[str] = field(default_factory=list)
    agencies: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, List[str]]:
        return {
            "dates": self.dates,
            "places": self.places,
            "agencies": self.agencies,
            "roles": self.roles,
        }

    def merge(self, other: "Entities") -> "Entities":
        return Entities(
            dates=_dedupe_preserve_order(self.dates + other.dates),
            places=_dedupe_preserve_order(self.places + other.places),
            agencies=_dedupe_preserve_order(self.agencies + other.agencies),
            roles=_dedupe_preserve_order(self.roles + other.roles),
        )


def extract_entities(text: str) -> Entities:
    """Extract literal dates/places/agencies/roles from ``text``.

    Purely mechanical: every returned value is a substring of the input.
    """
    dates: List[str] = []
    for pat in _DATE_PATTERNS:
        dates.extend(m.group(0) for m in pat.finditer(text))

    agencies = [m.group(1) for m in _AGENCY_RE.finditer(text)]
    roles = [m.group(1) for m in _ROLE_RE.finditer(text)]

    places = [m.group(1) for m in _BASE_PATTERN.finditer(text)]
    places += [m.group(1) for m in _STATE_RE.finditer(text)]

    return Entities(
        dates=_dedupe_preserve_order(dates),
        places=_dedupe_preserve_order(places),
        agencies=_dedupe_preserve_order(agencies),
        roles=_dedupe_preserve_order(roles),
    )
