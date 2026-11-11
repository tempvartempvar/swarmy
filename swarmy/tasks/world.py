"""Procedurally generated fictional world + knowledge-base documents.

Everything is derived from a seed, so models cannot answer from memory and every
question has an exact ground truth. Facts are deliberately spread across documents
(e.g. a company's CEO is only stated on the person's page), and noise documents add
distractors.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .base import Corpus, Doc

SYLLABLES = [
    "ka", "lo", "mi", "ve", "dra", "tor", "quin", "sel", "bar", "nu", "rho", "zen", "ul", "fa",
    "gri", "mor", "tas", "pel", "cor", "vin", "ash", "bel", "dor", "hal", "is", "jun", "lek",
    "mar", "nor", "os", "pra", "rul", "sav", "tem", "yor", "ce", "fen", "gal", "ith", "ko",
]
FIRST_NAMES = [
    "Ari", "Bex", "Cael", "Dara", "Eli", "Fen", "Gale", "Hollis", "Ira", "Jules", "Kit", "Lane",
    "Marlo", "Nico", "Oren", "Pax", "Quinn", "Remy", "Sage", "Tal", "Uma", "Vale", "Wren", "Yael",
    "Zuri", "Arden", "Blair", "Corin", "Devon", "Emery", "Finley", "Harper", "Indigo", "Jory",
]
COMPANY_SUFFIX = ["Systems", "Labs", "Works", "Dynamics", "Industries", "Group", "Instruments", "Collective"]
CATEGORIES = ["drone", "kettle", "bicycle", "headset", "lamp", "backpack", "camera", "speaker"]
TITLES = ["Chief Engineer", "Head of Sales", "Designer", "Analyst", "Logistics Lead", "Researcher"]
MODEL_CODES = ["X2", "S", "Pro", "Mini", "One", "GT", "Air", "Max", "3", "Neo", "Lite", "Prime"]
LOYALTY_LEVELS = ["none", "silver", "gold", "platinum"]


@dataclass
class Country:
    name: str
    vat_pct: int
    ship_base: float
    ship_per_kg: float


@dataclass
class City:
    name: str
    country: str
    population: int
    founded: int
    surcharge_pct: int


@dataclass
class Company:
    name: str
    hq: str
    founded: int
    employees: int
    ceo: str = ""


@dataclass
class Person:
    name: str
    employer: str
    title: str
    born: int
    hometown: str


@dataclass
class Product:
    name: str
    maker: str
    category: str
    launched: int
    price: float


@dataclass
class Category:
    name: str
    bulk_threshold: int
    bulk_discount_pct: int


@dataclass
class World:
    seed: int
    countries: dict[str, Country] = field(default_factory=dict)
    cities: dict[str, City] = field(default_factory=dict)
    companies: dict[str, Company] = field(default_factory=dict)
    people: dict[str, Person] = field(default_factory=dict)
    products: dict[str, Product] = field(default_factory=dict)
    categories: dict[str, Category] = field(default_factory=dict)
    loyalty_pct: dict[str, int] = field(default_factory=dict)
    corpus: Corpus | None = None

    # convenience lookups
    def maker_city(self, product: str) -> City:
        return self.cities[self.companies[self.products[product].maker].hq]


class _Names:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.used: set[str] = set()

    def word(self, n_min=2, n_max=3, suffix="") -> str:
        for _ in range(1000):
            n = self.rng.randint(n_min, n_max)
            w = "".join(self.rng.choice(SYLLABLES) for _ in range(n)) + suffix
            w = w.capitalize()
            if w.lower() not in self.used and 4 <= len(w) <= 12:
                self.used.add(w.lower())
                return w
        raise RuntimeError("name space exhausted")


def generate_world(seed: int, scale: float = 1.0, noise_docs: int = 12) -> World:
    rng = random.Random(f"world-{seed}")
    names = _Names(rng)
    w = World(seed=seed)
    n_countries, n_cities = 5, max(6, int(12 * scale))
    n_companies, n_people, n_products = max(6, int(14 * scale)), max(12, int(36 * scale)), max(8, int(24 * scale))

    for _ in range(n_countries):
        c = Country(
            name=names.word(2, 2, rng.choice(["ia", "and", "en", "or"])),
            vat_pct=rng.randint(5, 25),
            ship_base=rng.randint(7, 24) / 2,
            ship_per_kg=rng.randint(1, 8) / 4,
        )
        w.countries[c.name] = c
    country_names = list(w.countries)
    for _ in range(n_cities):
        c = City(
            name=names.word(2, 3),
            country=rng.choice(country_names),
            population=rng.randint(50, 5000) * 1000,
            founded=rng.randint(800, 1900),
            surcharge_pct=0 if rng.random() < 0.4 else rng.randint(3, 12),
        )
        w.cities[c.name] = c
    city_names = list(w.cities)
    for _ in range(n_companies):
        c = Company(
            name=f"{names.word(2, 3)} {rng.choice(COMPANY_SUFFIX)}",
            hq=rng.choice(city_names),
            founded=rng.randint(1900, 2015),
            employees=rng.randint(50, 90000),
        )
        w.companies[c.name] = c
    company_names = list(w.companies)
    first_pool = FIRST_NAMES[:]
    for i in range(n_people):
        first = rng.choice(first_pool)
        full = f"{first} {names.word(2, 3)}"
        p = Person(
            name=full,
            employer=company_names[i] if i < len(company_names) else rng.choice(company_names),
            title="Chief Executive Officer" if i < len(company_names) else rng.choice(TITLES),
            born=rng.randint(1950, 2000),
            hometown=rng.choice(city_names),
        )
        w.people[p.name] = p
        if p.title == "Chief Executive Officer":
            w.companies[p.employer].ceo = p.name
    cats = rng.sample(CATEGORIES, k=6)
    for cat in cats:
        w.categories[cat] = Category(cat, bulk_threshold=rng.randint(3, 12), bulk_discount_pct=rng.randint(5, 25))
    used_products = set()
    for _ in range(n_products):
        while True:
            pname = f"{names.word(2, 2)} {rng.choice(MODEL_CODES)}"
            if pname not in used_products:
                used_products.add(pname)
                break
        p = Product(
            name=pname,
            maker=rng.choice(company_names),
            category=rng.choice(cats),
            launched=rng.randint(1995, 2025),
            price=round(rng.randint(999, 89999) / 100, 2),
        )
        w.products[p.name] = p
    silver = rng.randint(2, 6)
    gold = rng.randint(7, 15)
    w.loyalty_pct = {"none": 0, "silver": silver, "gold": gold, "platinum": rng.randint(16, 22)}
    w.corpus = _build_corpus(w, rng, noise_docs)
    return w


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _build_corpus(w: World, rng: random.Random, noise_docs: int) -> Corpus:
    texts: list[tuple[str, str]] = []
    for c in w.countries.values():
        texts.append((c.name, (
            f"{c.name} is a country in the region covered by this knowledge base. "
            f"Its value-added tax (VAT) rate is {c.vat_pct}%. "
            f"Standard parcel shipping to addresses in {c.name} costs {_money(c.ship_base)} per order "
            f"plus {_money(c.ship_per_kg)} per kilogram of package weight."
        )))
    for c in w.cities.values():
        sur = (f"Deliveries to {c.name} carry a congestion surcharge of {c.surcharge_pct}% on shipping costs."
               if c.surcharge_pct else f"Deliveries to {c.name} carry no congestion surcharge.")
        texts.append((c.name, (
            f"{c.name} is a city in {c.country}. It was founded in {c.founded} and has a population of "
            f"{c.population:,}. {sur}"
        )))
    for c in w.companies.values():
        texts.append((c.name, (
            f"{c.name} was founded in {c.founded} and is headquartered in {c.hq}. "
            f"The company has {c.employees:,} employees."
        )))
    for p in w.people.values():
        surname = p.name.split()[-1]
        texts.append((p.name, (
            f"{p.name} was born in {p.born} in {p.hometown}. "
            f"{surname} works at {p.employer} as {p.title}."
        )))
    for p in w.products.values():
        texts.append((p.name, (
            f"The {p.name} is a {p.category} manufactured by {p.maker}. "
            f"It launched in {p.launched} with a list price of {_money(p.price)}."
        )))
    for cat in w.categories.values():
        texts.append((f"Bulk-order policy: {cat.name}s", (
            f"Orders of {cat.bulk_threshold} or more units of a {cat.name} receive a "
            f"{cat.bulk_discount_pct}% discount on the item subtotal. Smaller orders receive no bulk discount."
        )))
    lp = w.loyalty_pct
    texts.append(("Loyalty program", (
        f"Loyalty members receive a discount on the item subtotal: silver {lp['silver']}%, "
        f"gold {lp['gold']}%, platinum {lp['platinum']}%. Customers with membership level 'none' "
        f"receive no loyalty discount."
    )))
    # Distractor news items
    companies, cities = list(w.companies.values()), list(w.cities)
    for i in range(noise_docs):
        c = rng.choice(companies)
        other = rng.choice([x for x in cities if x != c.hq])
        kind = rng.randrange(3)
        if kind == 0:
            body = (f"In {rng.randint(2010, 2025)}, {c.name} opened a satellite office in {other} "
                    f"with {rng.randint(20, 400)} staff. The headquarters did not move.")
        elif kind == 1:
            body = (f"{c.name} announced a partnership with a logistics firm based in {other}. "
                    f"Analysts estimate the deal at {_money(rng.randint(1, 90) * 1e6)}.")
        else:
            body = (f"A trade fair in {other} featured exhibitors including {c.name}. "
                    f"Organisers reported {rng.randint(2, 60) * 1000:,} visitors.")
        texts.append((f"News: {c.name} ({i + 1})", body))
    rng.shuffle(texts)
    docs = [Doc(doc_id=f"doc-{i + 1:03d}", title=t, text=body) for i, (t, body) in enumerate(texts)]
    return Corpus(docs)
