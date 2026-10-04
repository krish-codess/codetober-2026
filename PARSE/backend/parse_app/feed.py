"""Feed producers. Both emit the same JSONL envelope, so ingestion cannot tell them apart.

* `mabsa_records`  - real M-ABSA review sentences (pinned revision) wrapped in a feed envelope.
* `synthetic_records` - offline generator with the same shape, for tests/CI and as the seed fallback.
* `inject_defects` - the defects a real feedback firehose has (see docs/DATA_PROFILE.md), applied
  deterministically so a failure reproduces exactly.

Envelope (one JSON object per line):
  id, source, text, lang, created_at, group, eval, gold: {domain, categories[]}
`gold` is the reference label. For eval records it becomes the test set; for pool records it is
never read by training - only by the simulated annotator (the "oracle").
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .fetch import DOMAINS, mabsa_path

Record = dict[str, Any]

# Language mix of the unlabelled pool: a head of high-resource languages and a thin tail.
POOL_LANG_WEIGHTS = {
    "en": 34, "es": 14, "de": 10, "fr": 10, "ja": 7, "zh": 7,
    "ru": 5, "ar": 4, "tr": 3, "hi": 3, "th": 1.5, "sw": 1.5,
}  # fmt: skip
LOW_RESOURCE = ("th", "sw", "hi", "tr")
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# Tolerant of the unescaped apostrophes that make ~0.4% of non-English label lines invalid Python
# literals (['Dr. Ng's', 'faculty general', 'positive']): only the last two fields are needed.
_TRIPLET_TAIL = re.compile(r"""['"]([^'"\[\]]*)['"]\s*,\s*['"]([^'"\[\]]*)['"]\s*\]""")


def parse_mabsa_line(line: str) -> tuple[str, list[str]] | None:
    """'text####[[term, category, polarity], ...]' -> (text, sorted distinct categories)."""
    if not line.strip():
        return None
    text, sep, labels = line.rpartition("####")
    if not sep:
        return None
    return text, sorted({m.group(1).strip() for m in _TRIPLET_TAIL.finditer(labels)})


def _stable_rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 - reproducibility, not security


def _read(data_dir: Path, domain: str, lang: str, split: str) -> list[str]:
    return mabsa_path(data_dir, domain, lang, split).read_text(encoding="utf-8").split("\n")


def mabsa_records(data_dir: Path, test_langs: Iterable[str], seed: int = 0) -> Iterator[Record]:
    """Pool: every train/dev sentence once, in ONE language drawn from POOL_LANG_WEIGHTS.
    Test: every test sentence in every `test_langs` language (parallel -> paired comparison).

    Languages are line-aligned translations of the same sentence (verified in the profile), so
    the group key is language-independent and gold comes from the English line.
    """
    langs, weights = zip(*POOL_LANG_WEIGHTS.items(), strict=True)
    n = 0
    for domain in DOMAINS:
        for split in ("train", "dev"):
            english = _read(data_dir, domain, "en", split)
            by_lang = {lang: _read(data_dir, domain, lang, split) for lang in langs}
            for i, en_line in enumerate(english):
                en = parse_mabsa_line(en_line)
                if en is None:
                    continue
                lang = _stable_rng(seed, domain, split, i).choices(langs, weights)[0]
                own = parse_mabsa_line(by_lang[lang][i]) if i < len(by_lang[lang]) else None
                if own is None:  # translation missing upstream: keep the sentence, in English
                    lang, own = "en", en
                n += 1
                yield _envelope(domain, split, i, lang, own[0], en[1], eval_=False, seq=n)
        english = _read(data_dir, domain, "en", "test")
        for lang in test_langs:
            lines = _read(data_dir, domain, lang, "test")
            for i, en_line in enumerate(english):
                en = parse_mabsa_line(en_line)
                own = parse_mabsa_line(lines[i]) if i < len(lines) else None
                if en is None or own is None:
                    continue
                n += 1
                yield _envelope(domain, "test", i, lang, own[0], en[1], eval_=True, seq=n)


def _envelope(
    domain: str, split: str, i: int, lang: str, text: str, cats: list[str], *, eval_: bool, seq: int
) -> Record:
    # M-ABSA carries no timestamps; the envelope's event time is synthetic (one record every ~6 min).
    ts = EPOCH + timedelta(minutes=6 * seq + _stable_rng("ts", domain, split, i, lang).randint(0, 5))
    return {
        "id": f"mabsa-{domain}-{split}-{i:05d}-{lang}",
        "source": "mabsa",
        "text": text,
        "lang": lang,
        "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group": f"{domain}/{split}/{i}",
        "eval": eval_,
        "gold": {"domain": domain, "categories": cats},
    }


# --- synthetic generator -----------------------------------------------------------------------

# (domain, raw category) -> per-language phrase. Small on purpose: it reproduces the *shape*
# (hierarchy, multi-label, language skew, no-aspect sentences), not the difficulty, of real data.
_BANK: dict[tuple[str, str], dict[str, list[str]]] = {
    ("hotel", "rooms cleanliness"): {
        "en": ["the room was dirty", "spotless clean room"],
        "es": ["la habitación estaba sucia", "habitación muy limpia"],
        "de": ["das Zimmer war schmutzig", "sehr sauberes Zimmer"],
        "sw": ["chumba kilikuwa kichafu", "chumba safi sana"],
    },
    ("hotel", "rooms comfort"): {
        "en": ["the bed was uncomfortable", "very comfortable bed"],
        "es": ["la cama era incómoda", "cama muy cómoda"],
        "de": ["das Bett war unbequem", "sehr bequemes Bett"],
        "sw": ["kitanda hakikuwa na starehe", "kitanda kizuri sana"],
    },
    ("hotel", "service general"): {
        "en": ["staff were rude at reception", "friendly helpful staff"],
        "es": ["el personal fue grosero", "personal amable y atento"],
        "de": ["das Personal war unhöflich", "freundliches Personal"],
        "sw": ["wafanyakazi walikuwa wakali", "wafanyakazi wakarimu"],
    },
    ("hotel", "location general"): {
        "en": ["too far from the city centre", "great location near the station"],
        "es": ["demasiado lejos del centro", "excelente ubicación"],
        "de": ["zu weit vom Zentrum entfernt", "tolle Lage am Bahnhof"],
        "sw": ["mbali sana na mji", "eneo zuri karibu na kituo"],
    },
    ("restaurant", "food quality"): {
        "en": ["the soup was cold and bland", "delicious fresh pasta"],
        "es": ["la sopa estaba fría", "pasta fresca deliciosa"],
        "de": ["die Suppe war kalt", "köstliche frische Pasta"],
        "sw": ["supu ilikuwa baridi", "chakula kitamu sana"],
    },
    ("restaurant", "food prices"): {
        "en": ["the dishes are overpriced", "cheap and generous portions"],
        "es": ["los platos son muy caros", "barato y abundante"],
        "de": ["die Gerichte sind überteuert", "günstig und reichlich"],
        "sw": ["chakula ni ghali mno", "bei nafuu sana"],
    },
    ("restaurant", "service general"): {
        "en": ["the waiter ignored us for an hour", "quick attentive waiters"],
        "es": ["el camarero nos ignoró", "camareros rápidos y atentos"],
        "de": ["der Kellner hat uns ignoriert", "schnelle aufmerksame Kellner"],
        "sw": ["mhudumu alitupuuza", "wahudumu wa haraka"],
    },
    ("laptop", "BATTERY#OPERATION_PERFORMANCE"): {
        "en": ["battery dies after two hours", "battery lasts all day"],
        "es": ["la batería dura dos horas", "la batería dura todo el día"],
        "de": ["der Akku hält nur zwei Stunden", "der Akku hält den ganzen Tag"],
        "sw": ["betri inaisha baada ya saa mbili", "betri inadumu siku nzima"],
    },
    ("laptop", "DISPLAY#QUALITY"): {
        "en": ["the screen has dead pixels", "bright sharp display"],
        "es": ["la pantalla tiene píxeles muertos", "pantalla nítida y brillante"],
        "de": ["der Bildschirm hat Pixelfehler", "helles scharfes Display"],
        "sw": ["skrini ina madoa", "skrini angavu na safi"],
    },
    ("laptop", "KEYBOARD#USABILITY"): {
        "en": ["keys are cramped and hard to type on", "typing on this keyboard is a joy"],
        "es": ["las teclas son incómodas", "da gusto escribir en este teclado"],
        "de": ["die Tasten sind zu eng", "das Tippen macht Spaß"],
        "sw": ["vitufe ni vigumu kutumia", "kibodi ni rahisi kutumia"],
    },
}
_FILLER = {
    "en": ["we went last week", "ok", "my friend told me about it"],
    "es": ["fuimos la semana pasada", "vale", "me lo recomendó un amigo"],
    "de": ["wir waren letzte Woche dort", "ok", "ein Freund hat es empfohlen"],
    "sw": ["tulienda wiki iliyopita", "sawa", "rafiki aliniambia"],
}
_SYN_LANGS, _SYN_WEIGHTS = ("en", "es", "de", "sw"), (55, 22, 18, 5)


def synthetic_records(n_pool: int, n_test: int, seed: int = 0) -> Iterator[Record]:
    rng = random.Random(seed)  # noqa: S311
    keys = sorted(_BANK)
    domains = sorted({d for d, _ in keys})

    def sentence(lang: str) -> tuple[str, str, list[str]]:
        domain = rng.choice(domains)
        if rng.random() < 0.15:  # no-aspect sentence, like 14% of the real data
            return domain, rng.choice(_FILLER[lang]) + f" ({domain})", []
        cats = rng.sample([c for d, c in keys if d == domain], k=rng.choice((1, 1, 1, 2)))
        return domain, ", ".join(rng.choice(_BANK[(domain, c)][lang]) for c in cats), cats

    for i in range(n_pool + n_test):
        is_test = i >= n_pool
        # test sentences are emitted in every language, like the parallel real test set
        for lang in _SYN_LANGS if is_test else (rng.choices(_SYN_LANGS, _SYN_WEIGHTS)[0],):
            state = rng.getstate()
            domain, text, cats = sentence(lang)
            text = f"{text} (ref {rng.randrange(10**6):06d})"  # tiny phrase bank: keep texts distinct
            if is_test and lang != _SYN_LANGS[-1]:
                rng.setstate(state)  # same draw for each language of one test group
            ts = EPOCH + timedelta(minutes=6 * i)
            yield {
                "id": f"syn-{i:06d}-{lang}",
                "source": "synthetic",
                "text": text,
                "lang": lang,
                "created_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "group": f"syn/{i}",
                "eval": is_test,
                "gold": {"domain": domain, "categories": cats},
            }


# --- defects -----------------------------------------------------------------------------------


def inject_defects(records: Iterable[Record], seed: int = 0, rate: float = 1.0) -> Iterator[bytes]:
    """Serialise records to JSONL bytes, corrupting a deterministic few-percent of pool records.

    Eval records are left intact so the per-language test sets stay parallel and comparable.
    Every defect here has a named handling rule in `ingest.validate`.
    """
    rng = random.Random(seed)  # noqa: S311
    held_back: list[Record] = []

    def dumps(r: Record) -> bytes:
        return json.dumps(r, ensure_ascii=False).encode("utf-8")

    for rec in records:
        if rec["eval"]:
            yield dumps(rec)
            continue
        r = dict(rec)
        roll = rng.random() / rate if rate > 0 else 1.0
        if roll < 0.002:
            yield dumps(r)[: rng.randint(10, 40)]  # truncated write -> malformed JSON
            continue
        if roll < 0.004:
            yield dumps(r).replace(b'"text"', b'"text\xff"', 1)  # invalid UTF-8 byte
            continue
        if roll < 0.007:
            r["text"] = rng.choice(["", "   ", "\n\t"])
        elif roll < 0.009:
            r["text"] = (r["text"] + " ") * 400  # pasted log / runaway form
        elif roll < 0.011:
            del r["text"]
        elif roll < 0.014:
            r["text"] = r["text"].encode("utf-8").decode("latin-1")  # double-decoded upstream
        elif roll < 0.044:
            r.pop("lang")
        elif roll < 0.074:
            r["lang"] = rng.choice([r["lang"].upper(), f"{r['lang']}-XX", f"{r['lang']}_xx"])
        elif roll < 0.084:
            r.pop("created_at")
        elif roll < 0.094:
            dt = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            r["created_at"] = rng.choice([int(dt.timestamp()), dt.strftime("%Y/%m/%d %H:%M:%S")])
        elif roll < 0.096:
            r["created_at"] = "2099-01-01T00:00:00Z"  # device clock nonsense
        elif roll < 0.098:
            r["created_at"] = "yesterday"
        elif roll < 0.103:
            r.pop("id")
        elif roll < 0.113:
            held_back.append(r)  # late arrival: delivered after the rest of the feed
            continue
        yield dumps(r)
        if roll < 0.123 and roll >= 0.113:
            yield dumps(r)  # at-least-once delivery: exact redelivery
        elif roll < 0.128 and roll >= 0.123:
            yield dumps({**r, "id": r.get("id", "x") + "-resubmit"})  # double submit, new id
        elif roll < 0.130 and roll >= 0.128:
            yield dumps({**r, "text": r["text"] + " (edited)"})  # same id, different text
    for r in held_back:
        yield dumps(r)


def write_feed(lines: Iterable[bytes], path: Path) -> Path:
    """Write once; a feed file is immutable after that (re-running is a no-op)."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with tmp.open("wb") as f:
        for line in lines:
            f.write(line + b"\n")
    tmp.replace(path)
    return path
