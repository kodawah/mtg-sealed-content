"""Fetch the contested ASINs' product pages for the evidence that says which
listing is the canonical one: who sells it, how many ratings it carries, and
whether it is still a live listing."""
import json, re, sys, time
import requests
from bs4 import BeautifulSoup

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1",
})

def count(text):
    if not text:
        return 0
    t = text.strip().strip("()").replace(",", "").split()[0]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KM]?)", t)
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "K": 1000, "M": 1000000}[m.group(2)])

def scrape(asin):
    for attempt in range(3):
        r = session.get(f"https://www.amazon.com/dp/{asin}")
        if r.ok and b"bm-verify" not in r.content and b"api-services-support" not in r.content:
            break
        time.sleep(5 * (attempt + 1))
    else:
        return {"asin": asin, "error": f"blocked ({r.status_code})"}

    s = BeautifulSoup(r.content, "html.parser")
    body = r.content.decode("utf-8", "ignore")
    title = s.find(id="productTitle")
    reviews = s.find(id="acrCustomerReviewText")
    seller = s.find(id="sellerProfileTriggerId")
    avail = s.find(id="availability")
    manufacturer = None
    for label in s.find_all(["th", "span"]):
        if label.get_text(strip=True) in ("Manufacturer", "Brand"):
            nxt = label.find_next(["td", "span"])
            if nxt:
                manufacturer = nxt.get_text(strip=True)[:40]
                break
    return {
        "asin": asin,
        "title": title.get_text(strip=True)[:110] if title else None,
        "ratings": count(reviews.get_text(strip=True) if reviews else ""),
        # Absent seller markup is not evidence of an Amazon-sold listing -- it
        # is simply no offer info in the fetched HTML, so say so.
        "seller": (seller.get_text(strip=True)[:45] if seller
                   else ("Amazon.com" if re.search(
                       r"Ships from\s*</span>\s*<span[^>]*>\s*Amazon", body)
                       else None)),
        "manufacturer": manufacturer,
        "availability": avail.get_text(" ", strip=True)[:40] if avail else None,
        "unavailable": "Currently unavailable" in body,
    }

asins = json.load(open(sys.argv[1]))
out = []
for i, a in enumerate(asins, 1):
    rec = scrape(a)
    out.append(rec)
    print(f"[{i}/{len(asins)}] {a} {rec.get('error') or rec.get('seller')}", flush=True)
    time.sleep(1.5)
json.dump(out, open(sys.argv[2], "w"), indent=1)
