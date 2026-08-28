"""Build the Rosetta database.

Two layers, and the distinction matters enough to state in the README:

  REAL      Entity names, entity count, and every anchor magnitude come from
            the published Union Budget reference file (see scripts/reference.py).
            96 real spending entities, real allocations in rupees crore, real
            BE/RE/Actual relationships, spanning five orders of magnitude from
            Rs 11.61 crore to Rs 4.77 lakh crore.

  GENERATED The detail beneath each entity -- scheme, account head, state and
            month level rows -- plus the years the reference file does not
            cover. Generated detail is scaled so it sums back to the real
            entity anchor, so aggregate queries return realistic figures.

  HISTORICAL Ministries that were renamed or merged before the reference file
            begins are added on their real years. These are real events; the
            reference file simply predates none of them.

Deterministic: same seed, same database, every time.

    python scripts/load_data.py
"""

from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference import re_to_be_ratio, spending_entities  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "rosetta.duckdb"
SCHEMA_PATH = ROOT / "data" / "schema.sql"
SEED = 20260826

rng = random.Random(SEED)

PLAN_REGIME_ENDS = 2017          # Plan/Non-Plan abolished from 2017-18
YEARS = [(y, f"{y}-{str(y + 1)[2:]}") for y in range(2014, 2024)]
LATEST_YEAR = YEARS[-1][0]
REFERENCE_YEAR = 2024            # the year the real anchors describe


def re_published(start: int) -> bool:
    """Whether a Revised Estimate exists for the fiscal year beginning `start`.

    RE for year Y is published with year Y+1's budget, so the most recent year
    in this database does not have one yet. The database models a point DURING
    2023-24, before the 2024-25 budget was presented: BE through 2023-24,
    RE through 2022-23, actuals through 2021-22.

    Every RE column in the loader is gated on this, and
    fiscal_years.re_available is derived from it, so the flag and the data
    cannot drift apart. They previously did: the flag said "no RE for 2023-24"
    while the columns held 1,167 values, and two eval questions were written
    against the flag rather than against the data. Neither the gold set nor the
    refusal rules caught it, because both had been written from the same wrong
    assumption -- agreement between them was never evidence of correctness.
    """
    return start <= LATEST_YEAR - 1

# Ministries renamed or merged out of existence before the reference file
# begins. Real events, real years.
# (name, short, sector, from_year, to_year, successor_name)
# Successor names are the REAL names in the reference file, which is organised
# by department rather than ministry. That is why a ministry-level predecessor
# hands over to a department-level successor here: the Ministry of Education
# has no row in the published budget, its money appears under Department of
# School Education and Literacy. The granularity shift is real and is one of
# the reasons a question about "the education ministry" needs a certified
# reading rather than a guess.
HISTORICAL = [
    ("Ministry of Human Resource Development", "MHRD", "education", 2014, 2019,
     "Department of School Education and Literacy"),
    ("Ministry of Water Resources", "MoWR", "water", 2014, 2018,
     "Department of Water Resources, River Development and Ganga Rejuvenation"),
    ("Ministry of Urban Development", "MoUD", "urban", 2014, 2016,
     "Ministry of Housing and Urban Affairs"),
    ("Ministry of Housing and Urban Poverty Alleviation", "MoHUPA", "urban", 2014, 2016,
     "Ministry of Housing and Urban Affairs"),
    ("Ministry of Agriculture", "MoA", "agriculture", 2014, 2014,
     "Department of Agriculture and Farmers Welfare"),
]

SECTOR_KEYWORDS = [
    ("education", ("education", "school", "literacy", "human resource")),
    ("health", ("health", "ayush", "family welfare")),
    ("defence", ("defence",)),
    ("transport", ("railway", "road transport", "civil aviation", "shipping", "ports")),
    ("rural", ("rural", "panchayati")),
    ("urban", ("urban", "housing")),
    ("agriculture", ("agricultur", "farmers", "fisheries", "animal husbandry", "food")),
    ("water", ("jal shakti", "water", "sanitation")),
    ("energy", ("power", "petroleum", "coal", "renewable", "atomic")),
    ("technology", ("electronics", "information technology", "science", "space", "telecommunic")),
    ("social", ("women", "child", "tribal", "social justice", "minority", "disabilit")),
    ("finance", ("finance", "revenue", "expenditure", "economic affairs", "niti")),
    ("home", ("home", "police")),
    ("external", ("external affairs",)),
]


def classify_sector(name: str) -> str:
    lowered = name.lower()
    for sector, keys in SECTOR_KEYWORDS:
        if any(k in lowered for k in keys):
            return sector
    return "other"


# --- build the ministry universe from real data -----------------------------
class Ministry:
    __slots__ = ("id", "name", "short", "sector", "frm", "to",
                 "renamed_from", "merged_into", "anchor", "is_real", "kind")

    def __init__(self, mid, name, short, sector, frm, to, anchor, is_real, kind="ministry"):
        self.id = mid
        self.name = name
        self.short = short
        self.sector = sector
        self.frm = frm
        self.to = to
        self.renamed_from = None
        self.merged_into = None
        self.anchor = anchor
        self.is_real = is_real
        self.kind = kind


def build_ministries() -> tuple[list[Ministry], dict[str, Ministry]]:
    entities = spending_entities()
    ministries: list[Ministry] = []
    by_name: dict[str, Ministry] = {}

    for i, entity in enumerate(entities, start=1):
        short = "".join(w[0] for w in entity.name.split()[:4]).upper()
        m = Ministry(
            i, entity.name, short, classify_sector(entity.name),
            2014, None, entity.anchor, True, entity.kind,
        )
        ministries.append(m)
        by_name[entity.name] = m

    # Real entities that only exist from a known year onward. Without this the
    # historical layer has nothing to hand over to.
    # Real formation years for entities that did not exist for the whole
    # series. Verified against the reference file's actual names.
    STARTS = {
        "Ministry of Housing and Urban Affairs": 2017,   # merger of MoUD + MoHUPA
        "Ministry of Cooperation": 2021,                 # created July 2021
        "Department of School Education and Literacy": 2020,   # ex-MHRD
        "Department of Water Resources, River Development and Ganga Rejuvenation": 2019,
        "Department of Agriculture and Farmers Welfare": 2015,
    }
    for name, start in STARTS.items():
        if name in by_name:
            by_name[name].frm = start

    next_id = len(ministries) + 1
    for name, short, sector, frm, to, successor in HISTORICAL:
        successor_m = by_name.get(successor)
        # Historical entities carry the successor's anchor, scaled down, so the
        # money does not vanish when a ministry is renamed mid-series.
        anchor = (successor_m.anchor * 0.85) if successor_m else 5000.0
        m = Ministry(next_id, name, short, sector, frm, to, anchor, False, "ministry")
        if successor_m:
            # A rename keeps one predecessor; a merge folds several into one.
            if name in ("Ministry of Urban Development",
                        "Ministry of Housing and Urban Poverty Alleviation"):
                m.merged_into = successor_m.id
            else:
                successor_m.renamed_from = m.id
        ministries.append(m)
        by_name[name] = m
        next_id += 1

    return ministries, by_name


MINISTRIES, BY_NAME = build_ministries()
RE_BE = re_to_be_ratio()


def find(*keywords: str) -> Ministry | None:
    """First ministry whose name contains all keywords, preferring real ones."""
    lowered = [k.lower() for k in keywords]
    matches = [
        m for m in MINISTRIES
        if m.kind != "functional" and all(k in m.name.lower() for k in lowered)
    ]
    if not matches:
        return None
    matches.sort(key=lambda m: (not m.is_real, len(m.name)))
    return matches[0]


def active_ministries(year: int) -> list[Ministry]:
    return [m for m in MINISTRIES if m.frm <= year and (m.to is None or year <= m.to)]


# --- fiscal years -----------------------------------------------------------
def fy_rows():
    rows = []
    for start, label in YEARS:
        is_plan = start < PLAN_REGIME_ENDS
        rows.append({
            "fiscal_year": label,
            "start_year": start,
            "end_year": start + 1,
            "label_short": f"FY{str(start + 1)[2:]}",
            "is_plan_regime": is_plan,
            "classification_note": (
                "Plan/Non-Plan classification in force" if is_plan
                else "Plan/Non-Plan abolished; Scheme/Establishment in force"
            ),
            # No actuals for the most recent two years, no RE for the latest.
            # "Actual spending in 2023-24" therefore has no honest answer,
            # which is one of the refusal cases the eval tests.
            "actuals_available": start <= LATEST_YEAR - 2,
            "re_available": re_published(start),
            "presented_on": dt.date(start, 2, 1),
            "is_interim_budget": start == 2019,
            "notes": None,
        })
    return rows


def ministry_rows():
    return [{
        "ministry_id": m.id,
        "ministry_name": m.name,
        "ministry_name_short": m.short,
        "ministry_code": f"M{m.id:03d}",
        "sector": m.sector,
        "entity_kind": m.kind,
        "effective_from_year": m.frm,
        "effective_to_year": m.to,
        "renamed_from_id": m.renamed_from,
        "merged_into_id": m.merged_into,
        "is_active": m.to is None,
        "department_count": rng.randint(1, 6),
        "notes": None if m.is_real else "predecessor entity, not in 2024-25 reference",
    } for m in MINISTRIES]


DEPARTMENT_NAMES = [
    "Secretariat", "Establishment", "Policy and Planning", "Field Operations",
    "Schemes and Programmes", "Research and Statistics",
]


DEPTS_BY_MINISTRY: dict[int, list[int]] = {}


def department_rows():
    rows, did = [], 1
    for m in MINISTRIES:
        for dname in rng.sample(DEPARTMENT_NAMES, rng.randint(2, 4)):
            rows.append({
                "department_id": did,
                "department_name": f"{dname}, {m.short}",
                "department_code": f"D{did:04d}",
                "ministry_id": m.id,
                "effective_from_year": m.frm,
                "effective_to_year": m.to,
                "is_active": m.to is None,
                "head_of_dept": None,
                "notes": None,
            })
            DEPTS_BY_MINISTRY.setdefault(m.id, []).append(did)
            did += 1
    return rows


# --- states -----------------------------------------------------------------
STATES = [
    ("AP", "Andhra Pradesh", "south", False, False, 49577103, 1956, None),
    ("TG", "Telangana", "south", False, False, 35003674, 2014, None),
    ("TN", "Tamil Nadu", "south", False, False, 72147030, 1956, None),
    ("KA", "Karnataka", "south", False, False, 61095297, 1956, None),
    ("KL", "Kerala", "south", False, False, 33406061, 1956, None),
    ("MH", "Maharashtra", "west", False, False, 112374333, 1960, None),
    ("GJ", "Gujarat", "west", False, False, 60439692, 1960, None),
    ("RJ", "Rajasthan", "west", False, False, 68548437, 1956, None),
    ("MP", "Madhya Pradesh", "central", False, False, 72626809, 1956, None),
    ("CG", "Chhattisgarh", "central", False, False, 25545198, 2000, None),
    ("UP", "Uttar Pradesh", "north", False, False, 199812341, 1950, None),
    ("BR", "Bihar", "east", False, False, 104099452, 1950, None),
    ("JH", "Jharkhand", "east", False, False, 32988134, 2000, None),
    ("WB", "West Bengal", "east", False, False, 91276115, 1950, None),
    ("OD", "Odisha", "east", False, False, 41974218, 1950, None),
    ("PB", "Punjab", "north", False, False, 27743338, 1966, None),
    ("HR", "Haryana", "north", False, False, 25351462, 1966, None),
    ("UK", "Uttarakhand", "north", False, True, 10086292, 2000, None),
    ("HP", "Himachal Pradesh", "north", False, True, 6864602, 1971, None),
    ("AS", "Assam", "northeast", False, True, 31205576, 1950, None),
    ("MN", "Manipur", "northeast", False, True, 2855794, 1972, None),
    ("ML", "Meghalaya", "northeast", False, True, 2966889, 1972, None),
    ("NL", "Nagaland", "northeast", False, True, 1978502, 1963, None),
    ("TR", "Tripura", "northeast", False, True, 3673917, 1972, None),
    ("MZ", "Mizoram", "northeast", False, True, 1097206, 1987, None),
    ("AR", "Arunachal Pradesh", "northeast", False, True, 1383727, 1987, None),
    ("SK", "Sikkim", "northeast", False, True, 610577, 1975, None),
    ("GA", "Goa", "west", False, False, 1458545, 1987, None),
    # J&K was a state until 2019, then split into two union territories.
    ("JK", "Jammu and Kashmir", "north", False, True, 12541302, 1954, "JKU"),
    ("JKU", "Jammu and Kashmir (UT)", "north", True, True, 12267032, 2019, None),
    ("LA", "Ladakh", "north", True, True, 274289, 2019, None),
    ("DL", "Delhi", "north", True, False, 16787941, 1952, None),
    ("PY", "Puducherry", "south", True, False, 1247953, 1963, None),
]


def state_rows():
    return [{
        "state_code": code, "state_name": name, "state_name_short": code,
        "region": region, "is_union_territory": ut, "is_special_category": special,
        "population_census": pop, "formed_year": formed,
        "successor_state_code": successor, "notes": None,
    } for code, name, region, ut, special, pop, formed, successor in STATES]


def active_states(year: int) -> list[str]:
    out = []
    for code, _, _, _, _, _, formed, successor in STATES:
        if formed > year:
            continue
        if successor is not None and year >= 2019:
            continue
        if code in ("JKU", "LA") and year < 2019:
            continue
        out.append(code)
    return out


# --- schemes ----------------------------------------------------------------
# Real schemes, real launch/closure years, real merges. Ministries are resolved
# by name against the real entity list, so a scheme lands under whichever real
# department actually runs it.
# (name, alt, ministry keywords, type, launch, closure, merged_into_name, sector)
SCHEMES_SPEC = [
    ("Mahatma Gandhi National Rural Employment Guarantee Scheme", "MGNREGA", ("rural development",), "CSS", 2006, None, None, "rural"),
    ("Pradhan Mantri Awaas Yojana - Gramin", "PMAY-G", ("rural development",), "CSS", 2016, None, None, "rural"),
    ("Pradhan Mantri Awaas Yojana - Urban", "PMAY-U", ("housing", "urban"), "CSS", 2015, None, None, "urban"),
    # Two distinct schemes, both colloquially "Swachh Bharat".
    ("Swachh Bharat Mission - Gramin", "SBM-G", ("drinking water",), "CSS", 2014, None, None, "water"),
    ("Swachh Bharat Mission - Urban", "SBM-U", ("housing", "urban"), "CSS", 2014, None, None, "urban"),
    # Real merge: SSA + RMSA + Teacher Education -> Samagra Shiksha, 2018.
    ("Sarva Shiksha Abhiyan", "SSA", ("human resource",), "CSS", 2001, 2017, "Samagra Shiksha", "education"),
    ("Rashtriya Madhyamik Shiksha Abhiyan", "RMSA", ("human resource",), "CSS", 2009, 2017, "Samagra Shiksha", "education"),
    ("Teacher Education Scheme", "TE", ("human resource",), "CSS", 2012, 2017, "Samagra Shiksha", "education"),
    ("Samagra Shiksha", "Samagra Shiksha Abhiyan", ("school education",), "CSS", 2018, None, None, "education"),
    # Real rename: Mid Day Meal -> PM POSHAN, 2021.
    ("National Programme of Mid Day Meal in Schools", "Mid Day Meal", ("human resource",), "CSS", 1995, 2020, "PM POSHAN", "education"),
    ("PM POSHAN", "Pradhan Mantri Poshan Shakti Nirman", ("school education",), "CSS", 2021, None, None, "education"),
    ("National Health Mission", "NHM", ("health",), "CSS", 2013, None, None, "health"),
    ("Ayushman Bharat - PMJAY", "PMJAY", ("health",), "CS", 2018, None, None, "health"),
    ("Pradhan Mantri Kisan Samman Nidhi", "PM-KISAN", ("agricultur",), "CS", 2019, None, None, "agriculture"),
    ("Jal Jeevan Mission", "Har Ghar Jal", ("drinking water",), "CSS", 2019, None, None, "water"),
    ("Integrated Child Development Services", "ICDS", ("women",), "CSS", 1975, 2020, "Saksham Anganwadi and Poshan 2.0", "social"),
    ("Saksham Anganwadi and Poshan 2.0", "Poshan 2.0", ("women",), "CSS", 2021, None, None, "social"),
    ("National Social Assistance Programme", "NSAP", ("rural development",), "CSS", 1995, None, None, "social"),
    ("Pradhan Mantri Gram Sadak Yojana", "PMGSY", ("rural development",), "CSS", 2000, None, None, "rural"),
    ("Digital India", None, ("electronics",), "CS", 2015, None, None, "technology"),
    ("Deendayal Antyodaya Yojana - NRLM", "DAY-NRLM", ("rural development",), "CSS", 2011, None, None, "rural"),
    ("Atal Mission for Rejuvenation and Urban Transformation", "AMRUT", ("housing", "urban"), "CSS", 2015, None, None, "urban"),
    ("Smart Cities Mission", None, ("housing", "urban"), "CS", 2015, None, None, "urban"),
    ("Pradhan Mantri Fasal Bima Yojana", "PMFBY", ("agricultur",), "CSS", 2016, None, None, "agriculture"),
    ("Establishment Expenditure", None, (), "establishment", 2014, None, None, "establishment"),
]


class Scheme:
    __slots__ = ("id", "name", "alt", "ministry_id", "stype", "launch",
                 "closure", "merged_into", "sector")


SCHEMES: list[Scheme] = []
_SCHEME_BY_NAME: dict[str, Scheme] = {}


def build_schemes():
    for i, (name, alt, keys, stype, launch, closure, merged, sector) in enumerate(
        SCHEMES_SPEC, start=1
    ):
        s = Scheme()
        s.id, s.name, s.alt, s.stype = i, name, alt, stype
        s.launch, s.closure, s.sector = launch, closure, sector
        m = find(*keys) if keys else None
        s.ministry_id = m.id if m else None
        s.merged_into = None
        SCHEMES.append(s)
        _SCHEME_BY_NAME[name] = s
    for (name, _, _, _, _, _, merged, _), s in zip(SCHEMES_SPEC, SCHEMES):
        if merged and merged in _SCHEME_BY_NAME:
            s.merged_into = _SCHEME_BY_NAME[merged].id


def scheme_rows():
    rows = []
    for s in SCHEMES:
        centre = 60 if s.stype == "CSS" else 100
        rows.append({
            "scheme_id": s.id, "scheme_name": s.name, "scheme_name_alt": s.alt,
            "scheme_code": f"S{s.id:03d}", "ministry_id": s.ministry_id,
            "scheme_type": s.stype,
            "is_centrally_sponsored": s.stype == "CSS",
            "is_central_sector": s.stype == "CS",
            "umbrella_scheme_id": None, "merged_into_id": s.merged_into,
            "launch_year": s.launch, "closure_year": s.closure,
            "funding_pattern_centre": centre, "funding_pattern_state": 100 - centre,
            "sector": s.sector, "sub_sector": None,
            "status": "closed" if s.closure else "active", "description": None,
        })
    return rows


def schemes_for(ministry_id: int, year: int) -> list[Scheme]:
    out = [
        s for s in SCHEMES
        if s.ministry_id == ministry_id
        and s.launch <= year and (s.closure is None or year <= s.closure)
    ]
    return out or [SCHEMES[-1]]      # Establishment Expenditure


# --- account heads ----------------------------------------------------------
MAJOR_HEADS = [
    ("2011", "Parliament/State Legislatures", "revenue"),
    ("2202", "General Education", "revenue"),
    ("2210", "Medical and Public Health", "revenue"),
    ("2215", "Water Supply and Sanitation", "revenue"),
    ("2216", "Housing", "revenue"),
    ("2401", "Crop Husbandry", "revenue"),
    ("2501", "Special Programmes for Rural Development", "revenue"),
    ("2505", "Rural Employment", "revenue"),
    ("3054", "Roads and Bridges", "revenue"),
    ("3601", "Grants-in-aid to State Governments", "revenue"),
    ("4202", "Capital Outlay on Education", "capital"),
    ("4210", "Capital Outlay on Medical and Public Health", "capital"),
    ("4215", "Capital Outlay on Water Supply", "capital"),
    ("5054", "Capital Outlay on Roads and Bridges", "capital"),
    ("7601", "Loans to State Governments", "loan"),
]
MINOR_HEADS = [
    ("01", "Direction and Administration"), ("04", "Research and Training"),
    ("05", "Grants to Institutions"), ("11", "Assistance to Local Bodies"),
    ("31", "Grants-in-aid General"), ("35", "Grants for Capital Assets"),
    ("50", "Other Charges"),
]
OBJECT_HEADS = [
    ("01", "Salaries"), ("06", "Medical Treatment"), ("11", "Travel Expenses"),
    ("13", "Office Expenses"), ("31", "Grants-in-aid"),
    ("35", "Grants for creation of Capital Assets"), ("50", "Other Charges"),
    ("53", "Major Works"),
]

HEADS_BY_TYPE: dict[str, list[str]] = {}


def account_head_rows():
    rows, order = [], 0
    for major, mdesc, atype in MAJOR_HEADS:
        for minor, minor_desc in MINOR_HEADS:
            for obj, obj_desc in OBJECT_HEADS:
                order += 1
                code = f"{major}-{minor}-{obj}"
                rows.append({
                    "head_code": code, "major_head": major, "major_head_desc": mdesc,
                    "sub_major_head": "00", "minor_head": minor,
                    "minor_head_desc": minor_desc, "sub_head": None,
                    "object_head": obj,
                    "head_description": f"{mdesc} - {minor_desc} - {obj_desc}",
                    "account_type": atype,
                    "is_revenue": atype == "revenue", "is_capital": atype == "capital",
                    "is_charged": False, "parent_code": f"{major}-{minor}",
                    "hierarchy_level": 3, "effective_from_year": 2014,
                    "effective_to_year": None, "sort_order": order,
                })
                HEADS_BY_TYPE.setdefault(atype, []).append(code)
    return rows


# --- demands ----------------------------------------------------------------
def year_scale(start: int) -> float:
    """Scale a 2024-25 anchor back to an earlier year.

    Union expenditure has grown at roughly 10% a year over this period, so
    earlier years are smaller. Applied uniformly, then jittered per entity.
    """
    return 1.10 ** (start - REFERENCE_YEAR)


def demand_rows():
    rows, did = [], 1
    index: dict[tuple[int, int], int] = {}
    for start, label in YEARS:
        actives = active_ministries(start)
        # Demand numbers are assigned in name order each year, so a ministry
        # does NOT keep the same demand number when the set of ministries
        # changes. That is the real behaviour and it breaks naive joins on
        # demand_no across years.
        for no, m in enumerate(sorted(actives, key=lambda x: x.name), start=1):
            be = round(m.anchor * year_scale(start) * rng.uniform(0.92, 1.08), 2)
            re_ = round(be * RE_BE * rng.uniform(0.94, 1.06), 2)
            act = round(re_ * rng.uniform(0.86, 1.02), 2)
            voted = round(be * rng.uniform(0.90, 1.0), 2)
            rows.append({
                "demand_id": did, "demand_no": no, "demand_name": m.name,
                "fiscal_year": label, "ministry_id": m.id, "department_id": None,
                "demand_type": rng.choice(["revenue", "capital", "combined"]),
                "is_charged": False,
                "voted_amount_crore": voted,
                "charged_amount_crore": round(be - voted, 2),
                "total_be_crore": be,
                "total_re_crore": re_ if re_published(start) else None,
                "total_actual_crore": act if start <= LATEST_YEAR - 2 else None,
                "notes": None,
            })
            index[(start, m.id)] = did
            did += 1
    return rows, index


# --- the fact tables --------------------------------------------------------
def allocation_rows(demand_index):
    """Detail rows that sum back to each entity's real anchor.

    The split is generated but the total is not: allocations for a ministry in
    a year are drawn as proportions and then normalised so they sum to that
    ministry's scaled real figure. Aggregate queries therefore return
    magnitudes a reader can sanity-check against the published budget.
    """
    rows, aid = [], 1
    for start, label in YEARS:
        is_plan = start < PLAN_REGIME_ENDS
        states_now = active_states(start)
        scale = year_scale(start)

        for m in active_ministries(start):
            demand_id = demand_index[(start, m.id)]
            target = m.anchor * scale * rng.uniform(0.92, 1.08)
            schemes_here = schemes_for(m.id, start)

            # Draw proportions first, normalise second, so the entity total is
            # exact rather than approximate.
            plan_rows = []
            for scheme in schemes_here:
                for _ in range(rng.randint(6, 14)):
                    atype = rng.choices(["revenue", "capital", "loan"],
                                        weights=[70, 25, 5])[0]
                    plan_rows.append((scheme, atype, rng.uniform(0.2, 3.0)))
            weight_sum = sum(w for _, _, w in plan_rows)

            for scheme, atype, weight in plan_rows:
                be = round(target * weight / weight_sum, 3)
                head = rng.choice(HEADS_BY_TYPE[atype])
                rev = be if atype == "revenue" else 0.0
                cap = be if atype == "capital" else 0.0
                loan = be if atype == "loan" else 0.0
                recoveries = round(be * rng.uniform(0.0, 0.06), 3)
                is_transfer = rng.random() < 0.28
                state = rng.choice(states_now) if is_transfer else None

                depts = DEPTS_BY_MINISTRY.get(m.id) or []
                rows.append({
                    "allocation_id": aid, "fiscal_year": label,
                    "demand_id": demand_id, "ministry_id": m.id,
                    "department_id": rng.choice(depts) if depts else None,
                    "scheme_id": scheme.id,
                    "head_code": head, "state_code": state,

                    "be_amount_crore": be,
                    "re_amount_crore": (
                        round(be * RE_BE * rng.uniform(0.88, 1.12), 3)
                        if re_published(start) else None
                    ),
                    "actual_amount_crore": (
                        round(be * rng.uniform(0.72, 1.06), 3)
                        if start <= LATEST_YEAR - 2 else None
                    ),

                    # Exactly 100x and 10^7x. The unit trap only works if these
                    # are computed, never drawn.
                    "be_amount_lakh": round(be * 100, 3),
                    "be_amount_rupees": round(be * 10_000_000, 2),

                    "gross_amount_crore": be,
                    "net_amount_crore": round(be - recoveries, 3),
                    "recoveries_crore": recoveries,

                    "revenue_amount_crore": rev,
                    "capital_amount_crore": cap,
                    "loan_amount_crore": loan,

                    # The 2017 discontinuity: exactly one pair is populated.
                    "plan_amount_crore": round(be * 0.45, 3) if is_plan else None,
                    "non_plan_amount_crore": round(be * 0.55, 3) if is_plan else None,
                    "scheme_amount_crore": None if is_plan else round(be * 0.62, 3),
                    "establishment_amount_crore": None if is_plan else round(be * 0.38, 3),

                    "is_charged": False, "is_voted": True,
                    "is_transfer_to_state": is_transfer,
                    "is_supplementary": rng.random() < 0.07,
                    "supplementary_batch": None, "revision_no": 0,
                    "source_document": f"DG-{start}",
                    "entered_on": dt.date(start, 3, 15),
                    "last_revised_on": dt.date(start + 1, 2, 1),
                    "notes": None,
                })
                aid += 1
    return rows


MONTHS = ["April", "May", "June", "July", "August", "September",
          "October", "November", "December", "January", "February", "March"]


def actual_rows(allocations):
    """Monthly actuals at a different grain from the annual allocations.

    Joining this to budget_allocations on (fiscal_year, ministry_id) without
    aggregating first fans each allocation out twelve times. The trap is the
    reason this table exists.
    """
    rows, rid = [], 1
    for alloc in allocations:
        if alloc["actual_amount_crore"] is None or rng.random() > 0.30:
            continue
        total = alloc["actual_amount_crore"]
        weights = [rng.uniform(0.4, 1.6) for _ in MONTHS]
        wsum = sum(weights)
        for i, month in enumerate(MONTHS):
            amount = round(total * weights[i] / wsum, 4)
            rows.append({
                "actual_id": rid, "fiscal_year": alloc["fiscal_year"],
                "month_no": i + 1, "month_name": month,
                "ministry_id": alloc["ministry_id"], "demand_id": alloc["demand_id"],
                "scheme_id": alloc["scheme_id"], "head_code": alloc["head_code"],
                "state_code": alloc["state_code"],
                "expenditure_crore": amount,
                "expenditure_lakh": round(amount * 100, 3),
                "revenue_exp_crore": amount if alloc["revenue_amount_crore"] else 0.0,
                "capital_exp_crore": amount if alloc["capital_amount_crore"] else 0.0,
                "recoveries_crore": round(amount * rng.uniform(0, 0.04), 4),
                "is_provisional": False, "is_revised": False,
                "reported_on": None, "reporting_lag_days": rng.randint(15, 75),
                "source_system": "PFMS", "notes": None,
            })
            rid += 1
    return rows


RECEIPT_KINDS = [
    ("tax", "Corporation Tax", False, True, False, 1020000),
    ("tax", "Taxes on Income", False, True, False, 1156000),
    ("tax", "Goods and Services Tax", False, True, False, 1061000),
    ("tax", "Customs", False, True, False, 231000),
    ("tax", "Union Excise Duties", False, True, False, 319000),
    ("non_tax", "Interest Receipts", False, True, False, 32000),
    ("non_tax", "Dividends and Profits", False, True, False, 290000),
    ("capital", "Recovery of Loans", False, False, True, 29000),
    ("capital", "Disinvestment Receipts", False, False, True, 50000),
    # Borrowings are a capital receipt and are NOT revenue. Whether they belong
    # in "total receipts" is the classic trap.
    ("capital", "Market Borrowings", True, False, True, 1416000),
    ("capital", "Small Savings", True, False, True, 434000),
]


def receipt_rows():
    rows, rid = [], 1
    for start, label in YEARS:
        scale = year_scale(start)
        for rtype, head, is_borrow, is_rev, is_cap, base in RECEIPT_KINDS:
            be = round(base * scale * rng.uniform(0.95, 1.05), 2)
            gross = round(be * rng.uniform(1.0, 1.10), 2)
            devolution = round(gross * 0.41, 2) if rtype == "tax" else 0.0
            rows.append({
                "receipt_id": rid, "fiscal_year": label, "receipt_type": rtype,
                "receipt_category": "union", "receipt_head": head, "head_code": None,
                "be_amount_crore": be,
                "re_amount_crore": (
                    round(be * RE_BE * rng.uniform(0.94, 1.06), 2)
                    if re_published(start) else None
                ),
                "actual_amount_crore": (
                    round(be * rng.uniform(0.88, 1.06), 2)
                    if start <= LATEST_YEAR - 2 else None
                ),
                "gross_amount_crore": gross,
                "net_amount_crore": round(gross - devolution, 2),
                "is_borrowing": is_borrow, "is_revenue_receipt": is_rev,
                "is_capital_receipt": is_cap,
                "devolution_to_states_crore": devolution,
                "net_to_centre_crore": round(gross - devolution, 2),
                "collection_cost_crore": round(gross * 0.006, 2), "notes": None,
            })
            rid += 1
    return rows


def transfer_rows():
    rows, tid = [], 1
    for start, label in YEARS:
        fc = "14th" if start <= 2019 else "15th"
        scale = year_scale(start)
        actives = active_ministries(start)
        for state in active_states(start):
            for ttype in ("devolution", "grant", "loan"):
                be = round(rng.uniform(200, 60000) * scale, 2)
                centre = round(be * 0.6, 2)
                m = rng.choice(actives)
                schemes_here = schemes_for(m.id, start)
                rows.append({
                    "transfer_id": tid, "fiscal_year": label, "state_code": state,
                    "ministry_id": m.id, "scheme_id": rng.choice(schemes_here).id,
                    "transfer_type": ttype, "finance_commission": fc,
                    "be_amount_crore": be,
                    "re_amount_crore": (
                        round(be * RE_BE * rng.uniform(0.9, 1.1), 2)
                        if re_published(start) else None
                    ),
                    "actual_amount_crore": (
                        round(be * rng.uniform(0.82, 1.05), 2)
                        if start <= LATEST_YEAR - 2 else None
                    ),
                    "centre_share_crore": centre,
                    "state_share_crore": round(be - centre, 2),
                    "is_tied_grant": ttype == "grant",
                    "is_untied": ttype == "devolution",
                    "released_on": None,
                    "utilisation_pct": round(rng.uniform(45, 99), 1),
                    "pending_uc_crore": round(be * rng.uniform(0, 0.3), 2),
                    "notes": None,
                })
                tid += 1
    return rows


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    build_schemes()

    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    heads = account_head_rows()
    departments = department_rows()          # fills DEPTS_BY_MINISTRY
    demands, demand_index = demand_rows()
    allocations = allocation_rows(demand_index)

    tables = {
        "fiscal_years": fy_rows(),
        "states": state_rows(),
        "ministries": ministry_rows(),
        "departments": departments,
        "demands": demands,
        "account_heads": heads,
        "schemes": scheme_rows(),
        "budget_allocations": allocations,
        "expenditure_actuals": actual_rows(allocations),
        "receipts": receipt_rows(),
        "transfers_to_states": transfer_rows(),
    }

    for name, rows in tables.items():
        frame = pd.DataFrame(rows)
        con.register("_staging", frame)
        cols = ", ".join(f'"{c}"' for c in frame.columns)
        con.execute(f"INSERT INTO {name} ({cols}) SELECT {cols} FROM _staging")
        con.unregister("_staging")
        print(f"  {name:22s} {len(rows):>9,} rows  {len(frame.columns):>3} cols")

    total_cols = con.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema='main'"
    ).fetchone()[0]

    real = len([m for m in MINISTRIES if m.is_real])
    print(f"\n  {len(tables)} tables, {total_cols} columns")
    print(f"  {len(MINISTRIES)} entities: {real} real from the reference file, "
          f"{len(MINISTRIES) - real} historical")

    check = con.execute(
        "SELECT sum(be_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"
    ).fetchone()[0]
    print(f"  2023-24 BE total: {check:,.0f} crore "
          f"(real 2024-25 grand total: 4,765,768)")
    con.close()

    print(f"\n  written to {DB_PATH.relative_to(ROOT)}")
    if total_cols < 150:
        print(f"\n  WARNING: {total_cols} columns is under the 150 the spec calls for")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
