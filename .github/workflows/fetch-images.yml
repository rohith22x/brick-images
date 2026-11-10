import csv, json, os, re, sys, time, requests
from urllib.parse import urlparse
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
summary = {}
for r in rows:
    slug, url = r["slug"].strip(), (r.get("url") or "").strip()
    if not url:
        continue
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    out = f"shots/{slug}"; os.makedirs(out, exist_ok=True)
    info = {"origin": origin, "products": [], "og_image": None}
    try:
        j = requests.get(origin + "/products.json?limit=10", headers=UA, timeout=20).json()
        for i, p in enumerate(j.get("products", [])[:4]):
            if not p.get("images"):
                continue
            src = p["images"][0]["src"]
            img = requests.get(src, headers=UA, timeout=30)
            ext = ".png" if ".png" in src.lower() else ".jpg"
            fn = f"{out}/shop-{i+1}-{re.sub(r'[^a-z0-9-]', '', p['handle'])[:40]}{ext}"
            open(fn, "wb").write(img.content)
            price = p["variants"][0].get("price") if p.get("variants") else None
            info["products"].append({"title": p["title"], "price": price, "file": fn, "image_url": src})
            time.sleep(0.3)
    except Exception as e:
        info["shopify_error"] = str(e)[:120]
    try:
        html = requests.get(url, headers=UA, timeout=20).text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.I)
        if m:
            src = m.group(1); src = "https:" + src if src.startswith("//") else src
            img = requests.get(src, headers={**UA, "Referer": url}, timeout=30)
            if img.ok and img.headers.get("content-type", "").startswith("image"):
                fn = f"{out}/og-image.jpg"; open(fn, "wb").write(img.content)
                info["og_image"] = {"file": fn, "image_url": src}
    except Exception as e:
        info["og_error"] = str(e)[:120]
    summary[slug] = info
    print(slug, "->", len(info["products"]), "shop images,", "og" if info["og_image"] else "no og")
json.dump(summary, open("shots/summary.json", "w"), indent=1)
