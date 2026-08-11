"""Org hierarchy, sellers, personas, quotas, fiscal-calendar helpers.

All names are synthetic, generated combinatorially from the pools below.
"""
from dataclasses import dataclass
from datetime import date, timedelta

PERSONAS = ("clean_operator", "sandbagger", "happy_ears", "ghost")

# Roughly: 40% clean operators, 20% each pathology persona.
PERSONA_WEIGHTS = {"clean_operator": 40, "sandbagger": 20, "happy_ears": 20, "ghost": 20}

FIRST_NAMES = [
    "Avery", "Jordan", "Riley", "Morgan", "Casey", "Quinn", "Rowan", "Sage",
    "Emerson", "Finley", "Harper", "Kendall", "Logan", "Marlow", "Noor",
    "Odell", "Parker", "Reese", "Skyler", "Tatum", "Vesper", "Wren", "Zion",
    "Blair", "Corin", "Darby", "Ellis", "Frankie", "Greer", "Hollis",
]
LAST_NAMES = [
    "Ashford", "Bramwell", "Calloway", "Denholm", "Eastvale", "Farrow",
    "Grantley", "Hollowell", "Ivesdale", "Jasperson", "Kirkwood", "Lindqvist",
    "Marchetti", "Northcote", "Oakhurst", "Pemberton", "Quillfeather",
    "Ristori", "Southgate", "Thistlewood", "Underhill", "Vantrease",
    "Westerley", "Yardley",
]

ACCOUNT_ADJECTIVES = [
    "Blue", "Iron", "Summit", "Nova", "Cascade", "Vertex", "Quartz", "Harbor",
    "Atlas", "Zenith", "Pioneer", "Crimson", "Silver", "Golden", "Northern",
    "Pacific", "Solar", "Lunar", "Granite", "Cedar",
]
ACCOUNT_NOUNS = [
    "Dynamics", "Systems", "Industries", "Logistics", "Analytics",
    "Manufacturing", "Networks", "Robotics", "Holdings", "Labs", "Foods",
    "Energy", "Medical", "Freight", "Software", "Retail", "Aerospace",
    "Materials", "Financial", "Media",
]
ACCOUNT_SUFFIXES = ["Inc", "LLC", "Group", "Corp", "Co"]

PRODUCT_LINES = [
    "CorePlatform", "DataSuite", "EdgeConnect", "SecureStack", "InsightHub",
    "FlowOps",
]
DEAL_MOTIONS = ["expansion", "renewal", "new logo", "upgrade", "pilot"]

# Segment -> regions, quarterly quota range, log-normal amount params.
SEGMENTS = {
    "enterprise": {
        "regions": ["NA-East", "EMEA"],
        "quota_range": (900_000, 1_500_000),
        "amount_mu": 11.9,   # median deal ~ $150k
        "amount_sigma": 0.8,
    },
    "mid_market": {
        "regions": ["NA-West", "APAC"],
        "quota_range": (300_000, 700_000),
        "amount_mu": 10.6,   # median deal ~ $40k
        "amount_sigma": 0.7,
    },
}

N_SELLERS = 60


@dataclass(frozen=True)
class Seller:
    name: str
    persona: str
    segment: str
    region: str
    team: str
    quota: float


@dataclass(frozen=True)
class Org:
    sellers: tuple

    def by_name(self, name):
        return next(s for s in self.sellers if s.name == name)


def build_org(rng):
    """60 sellers across 2 segments -> 4 regions -> 8-12 teams."""
    teams = []  # (segment, region, team_name)
    for segment, info in SEGMENTS.items():
        for region in info["regions"]:
            for t in range(rng.randint(2, 3)):
                teams.append((segment, region, f"Team {region}-{t + 1}"))

    names = set()
    while len(names) < N_SELLERS:
        names.add(f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}")
    ordered_names = sorted(names)
    rng.shuffle(ordered_names)

    persona_pool = [p for p, w in PERSONA_WEIGHTS.items() for _ in range(w * N_SELLERS // 100)]
    while len(persona_pool) < N_SELLERS:
        persona_pool.append("clean_operator")
    rng.shuffle(persona_pool)

    sellers = []
    for i, name in enumerate(ordered_names):
        segment, region, team = teams[i % len(teams)]
        lo, hi = SEGMENTS[segment]["quota_range"]
        quota = round(rng.uniform(lo, hi) / 10_000) * 10_000
        sellers.append(Seller(name, persona_pool[i], segment, region, team, float(quota)))
    return Org(tuple(sellers))


def sample_amount(rng, segment):
    info = SEGMENTS[segment]
    return round(max(rng.lognormvariate(info["amount_mu"], info["amount_sigma"]), 1_000.0), 2)


def _quarter_start_months(fy_start_month):
    return sorted((fy_start_month - 1 + 3 * i) % 12 + 1 for i in range(4))


def quarter_ends_between(lo, hi, fy_start_month):
    """Fiscal quarter-end dates d with lo <= d <= hi, ascending."""
    months = set(_quarter_start_months(fy_start_month))
    ends, y, m = [], lo.year, lo.month
    for _ in range(30):
        if m in months:
            qe = date(y, m, 1) - timedelta(days=1)
            if lo <= qe <= hi:
                ends.append(qe)
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return ends


def latest_quarter_start_on_or_before(d, fy_start_month):
    months = set(_quarter_start_months(fy_start_month))
    y, m = d.year, d.month
    for _ in range(13):
        if m in months and date(y, m, 1) <= d:
            return date(y, m, 1)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return None
