#!/usr/bin/env python3
"""Find and download images from web pages, ready to attach to a report.

Usage:
  python fetch_images.py URL [URL ...] --out DIR [options]

A URL can be a web page, a direct image link, or a local .html file.
Writes the images plus manifest.json (and manifest.md) into DIR.
"""
import argparse, base64, hashlib, io, json, mimetypes, os, re, sys, time
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REPORT_SAFE = {"PNG", "JPEG", "GIF"}          # embeds cleanly in docx/pdf/html
LAZY_ATTRS = ["src", "data-src", "data-original", "data-lazy-src", "data-url",
              "data-hi-res-src", "data-full", "data-image"]
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")


class NetworkBlocked(Exception):
    pass


BLOCKED_HOSTS = set()


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.9"})
    return s


def get(s, url, referer=None, timeout=20):
    host = urlparse(url).netloc
    if host in BLOCKED_HOSTS:
        raise NetworkBlocked(host)
    headers = {"Referer": referer} if referer else {}
    try:
        r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        if any(k in msg for k in ("NameResolution", "resolve", "Name or service", "ProxyError", "Tunnel")):
            BLOCKED_HOSTS.add(host)
            raise NetworkBlocked(host)
        raise
    deny = r.headers.get("x-deny-reason")
    if deny:
        BLOCKED_HOSTS.add(host)
        raise NetworkBlocked(f"{host} ({deny})")
    return r


def parse_srcset(srcset):
    """Parse a srcset per the HTML spec, so URLs containing commas
    (e.g. Cloudinary 'w_400,h_300' or data: URIs) survive. Returns [(url, weight)]."""
    out, i, n = [], 0, len(srcset)
    while i < n:
        while i < n and (srcset[i].isspace() or srcset[i] == ","):
            i += 1
        j = i
        while j < n and not srcset[j].isspace():
            j += 1
        url = srcset[i:j]
        trailing_comma = url.endswith(",")
        url = url.rstrip(",")
        i = j
        desc = ""
        if not trailing_comma:
            k = srcset.find(",", i)
            k = n if k == -1 else k
            desc, i = srcset[i:k].strip(), k + 1
        w = 1.0
        m = re.match(r"([\d.]+)([wx])", desc)
        if m:
            w = float(m.group(1)) * (1000 if m.group(2) == "x" else 1)
        if url:
            out.append((url, w))
    return out


def best_from_srcset(srcset):
    """Pick the largest candidate in a srcset string."""
    c = parse_srcset(srcset)
    return max(c, key=lambda t: t[1])[0] if c else None


def find_images(html, base):
    """Return list of dicts {url, alt, found_in} in page order, de-duplicated."""
    soup = BeautifulSoup(html, "html.parser")
    b = soup.find("base", href=True)
    if b:
        base = urljoin(base, b["href"])
    out, seen = [], set()

    def add(u, alt="", where=""):
        if not u:
            return
        u = u.strip()
        full = u if u.startswith("data:") else urljoin(base, u)
        if not (full.startswith(("http://", "https://", "file://")) or full.startswith("data:image")):
            return
        if full not in seen:
            seen.add(full)
            out.append({"url": full, "alt": (alt or "").strip(), "found_in": where})

    for m in soup.find_all("meta"):
        if (m.get("property") or m.get("name") or "").lower() in (
                "og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            add(m.get("content"), "", "meta:" + (m.get("property") or m.get("name")))
    for img in soup.find_all("img"):
        alt = img.get("alt", "") or img.get("title", "")
        ss = img.get("srcset") or img.get("data-srcset")
        if ss:
            add(best_from_srcset(ss), alt, "img[srcset]")
        for a in LAZY_ATTRS:
            v = img.get(a)
            if v and not v.startswith("data:image/gif"):   # skip 1px lazy placeholders
                add(v, alt, f"img[{a}]")
    for src in soup.select("picture source[srcset], source[srcset]"):
        add(best_from_srcset(src["srcset"]), "", "picture>source")
    for v in soup.find_all("video", poster=True):
        add(v["poster"], "", "video[poster]")
    for a in soup.find_all("a", href=True):
        if re.search(r"\.(jpe?g|png|gif|webp|avif|bmp|tiff?|svg)(\?|$)", a["href"], re.I):
            add(a["href"], a.get_text(" ", strip=True)[:120], "a[href]")
    for tag in soup.find_all(style=True):
        for u in CSS_URL.findall(tag["style"]):
            add(u, "", "style:background")
    for st in soup.find_all("style"):
        for u in CSS_URL.findall(st.get_text()):
            if u.startswith("data:image") or re.search(r"\.(jpe?g|png|gif|webp|avif|svg)", u, re.I):
                add(u, "", "<style>")
    for l in soup.find_all("link", href=True):
        rel = " ".join(l.get("rel", [])).lower()
        if "image_src" in rel or "apple-touch-icon" in rel:
            add(l["href"], "", f"link[{rel}]")
    return out


def safe_name(url, idx, ext):
    stem = os.path.splitext(os.path.basename(unquote(urlparse(url).path)))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:50] or "image"
    return f"{idx:03d}-{stem}.{ext}"


def decode_image(data):
    """Return (PIL image or None, detected format, is_svg)."""
    head = data[:500].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2000].lower()):
        return None, "SVG", True
    im = Image.open(io.BytesIO(data))
    im.load()
    return im, im.format, False


def svg_size(data):
    """Best-effort (width, height) of an SVG from its root attributes or viewBox."""
    head = data[:3000].decode("utf-8", "ignore")
    m = re.search(r"<svg[^>]*>", head, re.S)
    if not m:
        return None, None
    tag = m.group(0)
    def num(attr):
        v = re.search(attr + r'\s*=\s*["\']([\d.]+)(px)?["\']', tag)
        return float(v.group(1)) if v else None
    w, h = num(r"\bwidth"), num(r"\bheight")
    if w is None or h is None:
        vb = re.search(r'viewBox\s*=\s*["\'][\d.\-]+[\s,]+[\d.\-]+[\s,]+([\d.]+)[\s,]+([\d.]+)', tag)
        if vb:
            w, h = float(vb.group(1)), float(vb.group(2))
    return w, h


def to_report_safe(im, fmt):
    """Convert WEBP/AVIF/BMP/TIFF/ICO etc. to PNG (or JPEG for big photos)."""
    if fmt in REPORT_SAFE:
        return None, None
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    has_alpha = im.mode in ("RGBA", "LA", "P")
    if has_alpha:
        im = im.convert("RGBA")
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue(), "png"
    im = im.convert("RGB")
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90); return buf.getvalue(), "jpg"


def svg_to_png(data):
    """Render SVG to PNG at 2x for crisp printing. Tries cairosvg, then ImageMagick."""
    try:
        import cairosvg
        return cairosvg.svg2png(bytestring=data, scale=2)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["convert", "-density", "192", "svg:-", "png:-"], input=data,
                           capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout[:8] == b"\x89PNG\r\n\x1a\n":
            return r.stdout
    except Exception:
        pass
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--min-width", type=int, default=100, help="skip images narrower than this (px); 0 keeps all")
    p.add_argument("--min-height", type=int, default=100)
    p.add_argument("--max", type=int, default=50, help="max images per page")
    p.add_argument("--match", default="", help="regex; keep only images whose URL or alt text matches")
    p.add_argument("--types", default="", help="comma list of formats to keep, e.g. jpeg,png")
    p.add_argument("--no-convert", action="store_true", help="keep WEBP/AVIF etc. as-is")
    p.add_argument("--list-only", action="store_true", help="find image URLs but don't download")
    p.add_argument("--delay", type=float, default=0.2, help="seconds between downloads")
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    s = session()
    match = re.compile(a.match, re.I) if a.match else None
    types = {t.strip().upper().replace("JPG", "JPEG") for t in a.types.split(",") if t.strip()}
    manifest, hashes, blocked, idx = [], set(), [], 0

    for page in a.urls:
        # 1. Get candidate list
        if not os.path.exists(page) and not page.startswith(("http://", "https://")):
            if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", page):
                page = "https://" + page            # "example.com/page" -> https://
            else:
                print(f"[page] {page}: not a web address or an existing file, skipped", file=sys.stderr)
                continue
        if os.path.exists(page):
            html = open(page, encoding="utf-8", errors="replace").read()
            cands, page_base = find_images(html, "file://" + os.path.abspath(page)), page
        else:
            try:
                r = get(s, page)
            except NetworkBlocked as e:
                blocked.append(str(e)); continue
            if r.status_code >= 400:
                print(f"[page] {page} -> HTTP {r.status_code}", file=sys.stderr); continue
            ctype = r.headers.get("content-type", "")
            if ctype.startswith("image/"):
                cands = [{"url": r.url, "alt": "", "found_in": "direct link", "_data": r.content}]
            else:
                cands = find_images(r.text, r.url)
            page_base = r.url
        if match:
            cands = [c for c in cands if match.search(c["url"]) or match.search(c["alt"])]
        print(f"[page] {page}: {len(cands)} candidate image(s)", file=sys.stderr)

        if a.list_only:
            for c in cands[: a.max]:
                c.pop("_data", None); c["source_page"] = page; manifest.append(c)
            continue

        kept = 0
        for c in cands:
            if kept >= a.max:
                break
            url = c["url"]
            rec = {"source_page": page, "image_url": url[:200] if url.startswith("data:") else url,
                   "alt": c["alt"], "found_in": c["found_in"]}
            try:
                if "_data" in c:
                    data = c["_data"]
                elif url.startswith("data:"):
                    head, payload = url.split(",", 1)
                    data = base64.b64decode(payload) if ";base64" in head else unquote(payload).encode()
                elif url.startswith("file://"):
                    data = open(unquote(urlparse(url).path), "rb").read()
                else:
                    time.sleep(a.delay)
                    r = get(s, url, referer=page_base)
                    if r.status_code >= 400:
                        rec["skipped"] = f"HTTP {r.status_code}"; manifest.append(rec); continue
                    data = r.content
                h = hashlib.sha1(data).hexdigest()
                if h in hashes:
                    continue
                im, fmt, is_svg = decode_image(data)
            except NetworkBlocked as e:
                blocked.append(str(e)); rec["skipped"] = "network blocked"; manifest.append(rec); continue
            except Exception as e:
                rec["skipped"] = f"not a readable image ({type(e).__name__})"; manifest.append(rec); continue

            if types and fmt not in types:
                continue
            if im is not None:
                w, h = im.width, im.height
            elif is_svg:
                w, h = svg_size(data)
            else:
                w = h = None
            if w is not None and h is not None and (w < a.min_width or h < a.min_height):
                continue                            # icons, badges, spacers, tracking pixels
            hashes.add(h); idx += 1; kept += 1
            rec["original_format"] = fmt
            ext = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "SVG": "svg"}.get(fmt, (fmt or "bin").lower())
            if not a.no_convert:
                if is_svg:
                    png = svg_to_png(data)
                    if png:
                        data, ext = png, "png"; im = Image.open(io.BytesIO(png)); rec["converted_to"] = "PNG"
                    else:
                        rec["note"] = "SVG kept as-is (no converter found; pip install cairosvg); fine for web pages, convert before Word/PDF"
                elif im is not None:
                    conv, cext = to_report_safe(im, fmt)
                    if conv:
                        data, ext = conv, cext; rec["converted_to"] = cext.upper()
            name = safe_name(url if not url.startswith("data:") else "inline", idx, ext)
            path = os.path.join(a.out, name)
            with open(path, "wb") as f:
                f.write(data)
            if im is not None:
                rec["width"], rec["height"] = im.width, im.height
            rec["file"] = path
            rec["bytes"] = len(data)
            manifest.append(rec)

    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump({"images": manifest, "blocked": sorted(set(blocked))}, f, indent=2)
    with open(os.path.join(a.out, "manifest.md"), "w") as f:
        f.write("| # | File | Size | Alt text | Source |\n|---|---|---|---|---|\n")
        for i, m in enumerate([m for m in manifest if "file" in m], 1):
            dims = f"{m.get('width','?')}x{m.get('height','?')}"
            f.write(f"| {i} | {os.path.basename(m['file'])} | {dims} | {m['alt'][:60]} | {'(embedded in page)' if m['image_url'].startswith('data:') else m['image_url']} |\n")

    saved = sum(1 for m in manifest if "file" in m)
    print(f"[done] saved {saved} image(s) to {a.out}" if not a.list_only
          else f"[done] listed {len(manifest)} image URL(s)", file=sys.stderr)
    if blocked:
        hosts = sorted(set(blocked))
        print(f"[blocked] This computer's network refused {len(hosts)} host(s): " + ", ".join(hosts)
              + "\n          (network settings, not the website; see SKILL.md 'When things go wrong')", file=sys.stderr)
        sys.exit(3 if saved == 0 and not a.list_only else 0)


if __name__ == "__main__":
    main()
