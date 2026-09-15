"""Split the Amazon review entries into actionable buckets.

Amazon titles are verbose marketing strings ("Magic: The Gathering Foundations
Play Booster Box | 30 Packs | Collectible Trading Card Game") while catalog
names are terse and punctuation-stripped ("Foundations Play Booster Box"), so
the raw token_sort_ratio that check_product_fuzzy.py uses scores real matches
as low as 60. This strips the boilerplate first, then scores.

A high score alone cannot make a match sure. The catalog draws hard lines the
scorer barely notices -- a Box vs a Box Case vs a Pack, Collector vs Play vs
Set vs Draft boosters, and (across ~900 Secret Lair entries) Foil vs Non Foil
vs Rainbow/Galaxy/Raised Foil. A match must agree on that whole signature.
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

import yaml
from thefuzz import fuzz

AMAZON_URL = "https://www.amazon.com/dp/{}"

SURE_SCORE = 85
SURE_MARGIN = 6
CANDIDATE_FLOOR = 72

# Accessory wording that can never appear in an official product's contents
# blurb, so it is safe to match anywhere in the title.
IGNORE_ANYWHERE = [
    r"\brepack\b", r"\bprox(y|ies)\b", r"\bgraded\b", r"\bpsa\s?\d",
    r"\bcgc\b", r"\bbgs\b", r"\baltered\b", r"\bsleeve", r"\bplaymat\b",
    r"\bdeck box\b", r"\bbinder\b", r"\btoploader\b", r"\bdivider\b",
    r"\bplush\b", r"\bt-?shirt\b", r"\bposter\b", r"\bbackpack\b",
    r"\bdry erase\b", r"\bacrylic\b", r"\bcompatible with\b",
    r"\b\d+[- ]piece\b", r"\bcard game accessor", r"\bcase for\b",
    r"\bcard holder\b", r"\bdeck protector\b", r"\bscanner\b",
    r"\b(polyhedral|loyalty|counter|glitter|d6|sided) dice\b",
    r"\bdice (set|counters?|bundle)\b", r"\bland station\b",
    r"\bstorage (box|case)\b", r"\bdisplay stand\b",
]
IGNORE_ANYWHERE_RE = re.compile("|".join(IGNORE_ANYWHERE), re.I)

# Reseller-lot wording. Only checked against the head of the title (before the
# first "|" or "("), because contents blurbs legitimately list basic lands and
# card counts, and drop names legitimately contain words like "lot"
# ("Secret Lair x Avatar: The Last Airbender | A Lot to Learn").
IGNORE_HEAD = [
    r"\b(card|cards|bulk|mixed|huge|instant)\s+lot\b", r"\blots?\s+of\b",
    r"\brandom\s+(common|uncommon|rare|mythic|foil|cards?)\b",
    r"\bsingle cards?\b", r"\bsingles\b", r"\bassorted\b", r"\bbulk\b",
    r"\bbasic lands?\b", r"\bbox only\b", r"\bno cards\b", r"\bempty\b",
    r"\bvariety pack\b", r"\bgrab bag\b",
]
IGNORE_HEAD_RE = re.compile("|".join(IGNORE_HEAD), re.I)

BOILERPLATE = [
    r"magic:?\s*the\s*gathering", r"\bmtg\b", r"\bwizards of the coast\b",
    r"\btrading card game\b", r"\bcollectible\b", r"\bcard game\b",
    r"\bttrpg\b", r"\bccg\b", r"\btcg\b", r"\benglish\b", r"\bnew\b",
    r"\bsealed\b", r"\bfactory\b", r"\bofficial\b", r"\bages?\s*\d+\+?",
    r"\bfor \d+\+? players?\b", r"\b\d+\s*players?\b", r"\bedition\b",
    r"\bready[\s-]*to[\s-]*play\b", r"\bready to play\b", r"\bgreat gift\b",
    r"\bgift idea\b", r"\bfor kids\b", r"\bfamily game\b",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE), re.I)

# Container shape -- ordered longest-first so "Booster Box Case" reads as a
# case, not a box.
CONTAINERS = [
    ("booster box case", "case"), ("booster case", "case"),
    ("box case", "case"), ("display case", "case"), ("bundle case", "case"),
    (r"\bcases?\b", "case"),
    ("booster box", "box"), (r"\bdisplays?\b", "box"), (r"\bbox(es)?\b", "box"),
    ("booster pack", "pack"), (r"\bpacks?\b", "pack"),
    (r"\bbundles?\b", "bundle"),
    (r"\bdecks?\b", "deck"), (r"\bkits?\b", "kit"),
]

BOOSTER_TYPES = ["collector", "play", "set", "draft", "jumpstart", "theme",
                 "commander", "prerelease", "beginner", "starter", "scene",
                 "gift", "welcome", "arena", "challenger", "brawl", "intro",
                 "planeswalker", "tournament", "fat"]

# Foil treatments, longest-first: "non foil" must win over "foil", and the
# named foils ("rainbow foil") over the bare one.
FOILS = [
    ("non foil", "nonfoil"), ("nonfoil", "nonfoil"),
    ("traditional foil", "traditional"), ("rainbow foil", "rainbow"),
    ("galaxy foil", "galaxy"), ("raised foil", "raised"),
    ("textured foil", "textured"), ("gilded foil", "gilded"),
    ("halo foil", "halo"), ("surge foil", "surge"),
    ("confetti foil", "confetti"), ("etched foil", "etched"),
    ("step and compleat foil", "compleat"), ("neon ink", "neonink"),
    ("foil", "foil"),
]


def normalize(name, listing=False):
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[®™©℠]", "", name)
    name = name.replace("—", " ").replace("–", " ")
    name = re.sub(r"\|", " ", name)
    # "Pre-Release" and "Two-Player" are the same words the catalog closes up
    name = re.sub(r"\bpre[\s-]+release\b", "prerelease", name, flags=re.I)
    name = re.sub(r"\btwo[\s-]+player\b", "two player", name, flags=re.I)
    # Card/pack counts and parenthetical blurbs
    name = re.sub(r"\(\s*[^)]*\d+[^)]*\)", " ", name)
    name = re.sub(r"\b\d+\s*(packs?|cards?|boosters?|decks?|count|ct)\b", " ",
                  name, flags=re.I)
    # A blurb listing what is inside ("3 Foil Cards", "1 Alt-Art Foil Promo")
    # is not the product's own foil treatment. Only Amazon titles carry such
    # blurbs; a catalog name is authoritative text, and stripping it this way
    # ate the treatment off names that simply end in a number -- "Class of 87
    # Rainbow Foil" became "Class of", so the foil drop could never be found.
    if listing:
        name = re.sub(r"\b\d+\s+(?:[\w-]+\s+){0,2}foil\b", " ", name, flags=re.I)
        name = re.sub(r"\bfoil\s+(?:cards?|lands?|tokens?|promos?)\b", " ",
                      name, flags=re.I)
    name = BOILERPLATE_RE.sub(" ", name)
    name = re.sub(r"[^a-zA-Z0-9\s']", " ", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def shape(normalized, raw=None):
    """(container, booster types, foil treatment) -- the distinctions the
    catalog treats as separate products but fuzzy scoring barely registers."""
    low = " " + normalized + " "
    container = None
    for pattern, label in CONTAINERS:
        if re.search(pattern, low):
            container = label
            break
    if container is None and raw and SINGLE_PACK_RE.search(raw):
        container = "pack"
    # No catalog product is a bundle of loose packs, so this never matches one
    if (raw and container in ("pack", "bundle")
            and not SINGLE_PACK_RE.search(raw) and MULTI_PACK_RE.search(raw)):
        container = "multipack"
    kinds = frozenset(t for t in BOOSTER_TYPES if re.search(rf"\b{t}\b", low))
    foil = "plain"
    for phrase, label in FOILS:
        if phrase in low:
            foil = label
            break
    return container, kinds, foil


# The catalog's bare Secret Lair name IS the non-foil product (508 bare names,
# 353 of them carrying a "<name> Foil"/"Rainbow Foil" sibling; only 9 spell out
# "Non Foil"). So Amazon's "(Non-Foil Edition)" belongs on the bare name, and
# its "(Foil Edition)" on whichever foil sibling exists.
NONFOIL = {"plain", "nonfoil"}
NAMED_FOILS = {"traditional", "rainbow", "galaxy", "raised", "textured",
               "gilded", "halo", "surge", "confetti", "compleat", "neonink",
               "etched"}


def tier(query, candidate):
    """How well this candidate's kind of product matches the listing's.

    2 -- everything agrees, including which container it ships in
    1 -- agrees, but one side never said box/pack/case, so it stays ambiguous
         ("Duskmourn: House of Horror Collector Booster" is a single pack or a
         box depending on the seller, and only a human can tell which)
    0 -- a different product (a Play booster against a Collector one, a foil
         against a non-foil)
    """
    # Exact agreement, not subset: the empty set is a subset of everything, so
    # slack here let type-less entries ("Duskmourn House of Horror MTGO
    # Redemption") rank above the Collector Booster Box a listing really meant.
    if query[1] != candidate[1]:
        return 0
    if not foil_compatible(query[2], candidate[2]):
        return 0
    if query[0] == candidate[0]:
        return 2
    if query[0] is None or candidate[0] is None:
        return 1
    return 0


def foil_compatible(query, candidate):
    if query in NONFOIL:
        return candidate in NONFOIL
    if candidate in NONFOIL:
        return False
    # A generic "Foil" on either side can stand for any named foil; two named
    # foils have to be the same one.
    if query == "foil" or candidate == "foil":
        return True
    return query == candidate


# Amazon marks a single booster in the blurb ("(1 Pack of 15 Cards)"), which
# the parenthetical stripping would otherwise throw away -- and a lone
# "Collector Booster" then matches the Booster Box.
SINGLE_PACK_RE = re.compile(
    r"\b(1|one|single)\s+pack\b|\bpack of \d+\s+(magic\s+)?cards?\b", re.I)

# Sellers bundle several boosters and title it like the single ("Booster Pack
# Bundle (Includes 3 booster packs)", "Booster 3-Pack"). The catalog has no
# entry for those, and calling one the single pack is how a 3-pack ends up
# recorded as the product itself.
MULTI_PACK_RE = re.compile(
    r"\b(?!1\b)\d+[\s-]*pack\b|\bpack bundle\b|\bbundle of \d+\b"
    r"|\bincludes?\s+\d+\s+(?:booster\s+)?packs\b"
    r"|\b(?!1\b)\d+\s+booster\s+packs\b", re.I)


# Foil treatment is already decided by the shape signature, so leaving the
# words in the scored string only depresses the score of the right candidate:
# "Secret Lair Drop Garden Buds Rainbow Foil" against a listing's "Garden Buds
# (Foil Edition)" loses on "drop" and "rainbow" even though both are accounted
# for. "Drop" is likewise a constant the catalog puts on every Secret Lair.
COMPARE_NOISE = re.compile(
    r"\b(non\s*foil|nonfoil|traditional|rainbow|galaxy|raised|textured|gilded|"
    r"halo|surge|confetti|etched|neon ink|step and compleat|foil|drop)\b", re.I)


def title_set_codes(title, valid):
    """Set codes a listing names outright -- "Prerelease Pack : Core Set 2019
    (M19)" says which year it means far more reliably than the score does,
    where Core Set 2021 is three points behind. Only codes the catalog
    actually has count, so ordinary bracketed words are ignored."""
    found = set()
    for token in re.findall(r"[({\[]\s*([A-Za-z0-9]{2,5})\s*[)}\]]", title):
        if token.upper() in valid:
            found.add(token.upper())
    return found


def compare_key(normalized):
    return re.sub(r"\s+", " ", COMPARE_NOISE.sub(" ", normalized)).strip()


def ignore_reason(title):
    """Why this listing is not a catalog product, or None if it looks real."""
    hit = IGNORE_ANYWHERE_RE.search(title)
    if hit:
        return hit.group(0)
    head = re.split(r"[|(]", title)[0]
    hit = IGNORE_HEAD_RE.search(head)
    if hit:
        return hit.group(0)
    # "Collection" names a real product line (Commander Collection, Starter
    # Collection); a card count alongside it marks the reseller kind.
    if re.search(r"\bcollection\b", head, re.I) and re.search(r"\d{2,}", head):
        return "card-count collection"
    return None


def main():
    review = yaml.safe_load(Path("data/review.yaml").read_text())
    entries = review.get("amazon", {})

    known = []
    known_asins = {}
    for path in Path("data/products").glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        for pname, pbody in (data.get("products") or {}).items():
            pnorm = normalize(pname)
            known.append((pname, path.stem, compare_key(pnorm), shape(pnorm)))
            existing = (pbody.get("identifiers") or {}).get("asin")
            if existing:
                known_asins[str(existing)] = pname

    valid_codes = {k[1].upper() for k in known}

    print(f"{len(entries)} amazon entries vs {len(known)} known products",
          file=sys.stderr)

    buckets = {"ignore": [], "sure": [], "multiple": [], "nomatch": [],
               "conflict": []}

    for name, body in entries.items():
        asin = body["identifiers"]["asin"]

        if asin in known_asins:
            buckets["conflict"].append({"name": name, "asin": asin,
                                        "product": known_asins[asin]})
            continue

        reason = ignore_reason(name)
        if reason:
            buckets["ignore"].append({"name": name, "asin": asin,
                                      "reason": reason})
            continue

        query = normalize(name, listing=True)
        if not query:
            buckets["ignore"].append({"name": name, "asin": asin,
                                      "reason": "no content after normalization"})
            continue
        qshape = shape(query, name)
        query = compare_key(query)
        if not query:
            buckets["ignore"].append({"name": name, "asin": asin,
                                      "reason": "no content after normalization"})
            continue

        # Shape ranks candidates rather than only vetoing the winner: a
        # "(Foil Edition)" listing must reach its "Rainbow Foil" sibling
        # instead of losing by a token to the bare non-foil name.
        codes = title_set_codes(name, valid_codes)
        scored = sorted(
            ((tier(qshape, k[3]), k[1].upper() in codes,
              fuzz.token_sort_ratio(query, k[2]), k[0], k[1], k[3])
             for k in known),
            key=lambda s: (s[0], s[1], s[2]),
            reverse=True,
        )
        # Shape only outranks similarity among candidates that are plausible
        # on name alone; without this a tier-2 candidate scoring 53 beats the
        # tier-1 product the listing actually names at 97.
        plausible = [s for s in scored if s[2] >= CANDIDATE_FLOOR]
        if plausible:
            scored = plausible + [s for s in scored if s[2] < CANDIDATE_FLOOR]
        else:
            scored = sorted(scored, key=lambda s: -s[2])
        best = scored[0]
        # Compare only against runners-up of the same standing: a different
        # shape tier is a different product, and a candidate whose set code the
        # listing did not name is not the near-miss it looks like.
        peers = [s for s in scored[1:] if s[0] == best[0] and s[1] == best[1]]
        margin = best[2] - peers[0][2] if peers else 100
        shape_tier = best[0]
        shape_ok = shape_tier == 2
        best = (best[2], best[3], best[4], best[5])
        scored = [(s[2], s[3], s[4], s[5]) for s in scored]

        record = {
            "name": name, "asin": asin, "score": best[0], "margin": margin,
            "best": best[1], "code": best[2],
            "shape_query": [qshape[0], sorted(qshape[1]), qshape[2]],
            "shape_best": [best[3][0], sorted(best[3][1]), best[3][2]],
            "shape_ok": shape_ok, "shape_tier": shape_tier,
            "runners_up": [{"product": s[1], "code": s[2], "score": s[0]}
                           for s in scored[1:5]],
        }

        if best[0] >= SURE_SCORE and margin >= SURE_MARGIN and shape_ok:
            buckets["sure"].append(record)
        elif best[0] >= CANDIDATE_FLOOR:
            buckets["multiple"].append(record)
        else:
            buckets["nomatch"].append(record)

    # A product holds a single asin, so several listings resolving to the same
    # product is a choice for a human, not a sure match. Demote the whole
    # group -- picking the top scorer automatically is how a 3-pack ends up
    # recorded as the single pack.
    by_product = {}
    for record in buckets["sure"]:
        by_product.setdefault((record["best"], record["code"]), []).append(record)
    buckets["sure"] = [r for g in by_product.values() if len(g) == 1 for r in g]
    buckets["competing"] = sorted(
        (r for g in by_product.values() if len(g) > 1 for r in g),
        key=lambda r: (r["best"], -r["score"]),
    )

    # Matches that clear every bar except that the listing never said whether
    # it ships as a box or a pack, where the catalog carries both. Nothing can
    # settle that from the title alone, so they are flagged for a human with a
    # link to the listing rather than guessed at.
    # No margin requirement: a box and its pack score almost identically, so
    # the tie is the ambiguity itself, not a reason to skip flagging it.
    flagged = [r for r in buckets["multiple"] if r["shape_tier"] == 1
               and r["score"] >= SURE_SCORE]
    flagged_ids = {id(r) for r in flagged}
    buckets["multiple"] = [r for r in buckets["multiple"]
                           if id(r) not in flagged_ids]
    buckets["ambiguous"] = sorted(flagged, key=lambda r: -r["score"])

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "amazon_match.json")
    out.write_text(json.dumps(buckets, indent=1))
    write_report(buckets, out.with_suffix(".md"), len(entries), len(known))
    for key, items in buckets.items():
        print(f"{key:10s} {len(items):5d}", file=sys.stderr)
    print(f"wrote {out} and {out.with_suffix('.md')}", file=sys.stderr)


def write_report(buckets, path, n_entries, n_known):
    out = [
        "# Amazon review triage",
        "",
        f"{n_entries} Amazon listings matched against {n_known} known products.",
        "",
        "| bucket | count | meaning |",
        "|---|---|---|",
        f"| dead sure | {len(buckets['sure'])} | one listing, one product, every "
        "distinction agrees |",
        f"| multiple candidates | {len(buckets['multiple'])} | plausible, needs a "
        "human pick |",
        f"| competing listings | {len(buckets['competing'])} | several ASINs for "
        "the SAME product |",
        f"| ignore | {len(buckets['ignore'])} | accessories and reseller lots |",
        f"| box/pack unstated | {len(buckets['ambiguous'])} | would be sure, but "
        "the listing never said which |",
        f"| no match | {len(buckets['nomatch'])} | nothing in the catalog "
        "resembles it |",
        "",
        "## Dead sure (%d)" % len(buckets["sure"]),
        "",
    ]
    for r in sorted(buckets["sure"], key=lambda x: -x["score"]):
        out.append(f"- `{r['asin']}` **{r['best']}** [{r['code']}] "
                   f"— {r['score']}/+{r['margin']}")
        out.append(f"    - listing: {r['name']}")

    out += ["", "## Box or pack unstated — check the listing (%d)"
            % len(buckets["ambiguous"]), "",
            "Everything else agrees; the catalog has both a box and a pack and "
            "the title names neither.", ""]
    for r in buckets["ambiguous"]:
        out.append(f"- [{r['asin']}]({AMAZON_URL.format(r['asin'])}) — "
                   f"{r['name']}")
        out.append(f"    - {r['score']}: **{r['best']}** [{r['code']}]")
        for c in r["runners_up"][:3]:
            out.append(f"    - {c['score']}: {c['product']} [{c['code']}]")

    out += ["", "## Competing listings — pick one ASIN each (%d)"
            % len(buckets["competing"]), ""]
    last = None
    for r in buckets["competing"]:
        if r["best"] != last:
            out.append(f"- **{r['best']}** [{r['code']}]")
            last = r["best"]
        out.append(f"    - [{r['asin']}]({AMAZON_URL.format(r['asin'])}) "
                   f"({r['score']}) {r['name']}")

    out += ["", "## Multiple candidates (%d)" % len(buckets["multiple"]), ""]
    for r in sorted(buckets["multiple"], key=lambda x: -x["score"]):
        note = "" if r["shape_tier"] == 2 else "  (box/pack unstated)"
        out.append(f"- [{r['asin']}]({AMAZON_URL.format(r['asin'])}) {r['name']}")
        out.append(f"    - {r['score']}: {r['best']} [{r['code']}]{note}")
        for c in r["runners_up"][:3]:
            out.append(f"    - {c['score']}: {c['product']} [{c['code']}]")

    out += ["", "## Ignore (%d)" % len(buckets["ignore"]), ""]
    for r in sorted(buckets["ignore"], key=lambda x: x["reason"].lower()):
        out.append(f"- `{r['asin']}` ({r['reason']}) {r['name']}")

    out += ["", "## No match (%d)" % len(buckets["nomatch"]), ""]
    for r in sorted(buckets["nomatch"], key=lambda x: -x["score"]):
        out.append(f"- `{r['asin']}` [{r['score']}] {r['name']}")
        out.append(f"    - closest: {r['best']} [{r['code']}]")

    path.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
