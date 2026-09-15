"""Resolve the contested Amazon listings.

For each product several ASINs claim, keep the live listing carrying the most
ratings and retire the rest: they are duplicate listings of a product that now
has its identifier, so leaving them in review would resurface them on every
scrape. Multi-pack listings are not duplicates at all -- the catalog has no
entry for them -- so they are retired the same way, without claiming a product.
"""
import json, re, sys, collections
from pathlib import Path
import yaml

SP = Path(sys.argv[1])
pages = {}
for p in json.load(open(SP / "competing_pages.json")):
    if not p.get("error") and p.get("seller") == "Amazon.com":
        p["seller"] = None
    pages[p["asin"]] = p
for p in json.load(open(SP / "competing_retry.json")):
    pages[p["asin"]] = p

buckets = json.load(open(SP / "amazon_remaining.json"))
groups = collections.OrderedDict()
for r in buckets["competing"]:
    groups.setdefault((r["best"], r["code"]), []).append(r)

review_path = Path("data/review.yaml")
review = yaml.safe_load(review_path.read_text())
ignore_path = Path("data/ignore.yaml")
ignore = yaml.safe_load(ignore_path.read_text()) or {}
ignore_section = ignore.setdefault("amazon", {})

applied, ignored, left = [], [], []
for (prod, code), rs in groups.items():
    multipack = [r for r in rs
                 if re.search(r"\b\d+[- ]pack\b", r["name"], re.I)
                 and not re.search(r"\b1[- ]pack\b", r["name"], re.I)]
    candidates = [r for r in rs if r not in multipack]

    live = [r for r in candidates
            if not pages.get(r["asin"], {}).get("unavailable")
            and not pages.get(r["asin"], {}).get("error")]
    live.sort(key=lambda r: -pages.get(r["asin"], {}).get("ratings", 0))

    # A multi-pack has no catalog home either way, so retire it regardless.
    for r in multipack:
        ignore_section[r["asin"]] = r["name"]
        ignored.append((r, "multi-pack, no catalog entry"))

    if not live or pages.get(live[0]["asin"], {}).get("ratings", 0) == 0:
        left.extend(candidates)
        continue

    winner = live[0]
    target = Path("data/products") / f"{code}.yaml"
    data = yaml.safe_load(target.read_text())
    entry = data["products"][prod]
    identifiers = entry.setdefault("identifiers", {})
    if identifiers.get("asin") and identifiers["asin"] != winner["asin"]:
        print(f"CONFLICT {prod}: already {identifiers['asin']}")
        left.extend(candidates)
        continue
    identifiers["asin"] = winner["asin"]
    target.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))
    applied.append((winner, prod, code,
                    pages.get(winner["asin"], {}).get("ratings", 0)))

    for r in candidates:
        if r is winner:
            continue
        ignore_section[r["asin"]] = r["name"]
        ignored.append((r, f"duplicate listing of {prod}"))

retired = {r["asin"] for r, _ in ignored} | {w["asin"] for w, *_ in applied}
review["amazon"] = {name: body for name, body in review["amazon"].items()
                    if body["identifiers"]["asin"] not in retired}

review_path.write_text(yaml.safe_dump(review, allow_unicode=True))
ignore_path.write_text(yaml.safe_dump(ignore, allow_unicode=True))

print(f"applied {len(applied)} identifiers")
for w, prod, code, n in applied:
    print(f"  {w['asin']} -> {prod} [{code}]  ({n} ratings)")
print(f"ignored {len(ignored)} listings, {len(left)} left in review")
