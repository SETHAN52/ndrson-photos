#!/usr/bin/env python3
"""Post family photos from a private email inbox to this static site.

All private data comes from environment variables (GitHub Actions secrets):
  AGENTMAIL_API_KEY   AgentMail API key (inbox-scoped is enough)
  NDRSON_INBOX_ID     the private inbox id
  NDRSON_ALLOWLIST    JSON: {"family": [{"name": "...", "phones": ["10digits"], "emails": ["..."]}]}
Nothing private is written to the repo or printed: logs contain counts only.

Usage (run from the repo root):
  post_photos.py fetch --pending FILE   process new mail -> photos/, posts.json, index.html;
                                        writes message ids to label into FILE (outside the repo)
  post_photos.py label --pending FILE   apply labels after the commit has been pushed
  post_photos.py render                 rebuild index.html from posts.json
"""
import argparse, hashlib, html, io, json, os, re, sys
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

ROOT = os.getcwd()
TZ = ZoneInfo("America/Chicago")
POSTED, IGNORED = "ndrson-posted", "ndrson-ignored"
MAX_PX, QUALITY = 1600, 82
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif"}
API = os.environ.get("AGENTMAIL_BASE_URL", "https://api.agentmail.to").rstrip("/") + "/v0"


# ---------------- config / api ----------------
def api_key():
    k = os.environ.get("AGENTMAIL_API_KEY", "").strip()
    if k.startswith("AGENTMAIL_API_KEY="):  # tolerate a pasted "NAME=value"
        k = k.split("=", 1)[1].strip()
    if not k:
        sys.exit("missing AGENTMAIL_API_KEY")
    return k


def inbox_id():
    v = os.environ.get("NDRSON_INBOX_ID", "").strip()
    if not v:
        sys.exit("missing NDRSON_INBOX_ID")
    return v


def family():
    try:
        return json.loads(os.environ["NDRSON_ALLOWLIST"])["family"]
    except Exception:
        sys.exit("missing or invalid NDRSON_ALLOWLIST")


class Mail:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Bearer " + api_key()
        self.base = f"{API}/inboxes/{quote(inbox_id(), safe='')}"

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"AgentMail {method} {path.split('/')[1] if '/' in path else path} -> HTTP {r.status_code}")
        return r.json() if r.content else {}

    def list_all(self):
        token, out = None, []
        while True:
            params = {"limit": 100, "ascending": "true"}
            if token:
                params["page_token"] = token
            j = self._req("GET", "/messages", params=params)
            out.extend(j.get("messages", []))
            token = j.get("next_page_token")
            if not token:
                return out

    def get(self, mid):
        return self._req("GET", f"/messages/{quote(mid, safe='')}")

    def attachment(self, mid, aid):
        meta = self._req("GET", f"/messages/{quote(mid, safe='')}/attachments/{quote(aid, safe='')}")
        r = requests.get(meta["download_url"], timeout=120)  # presigned URL: no auth header
        r.raise_for_status()
        return r.content

    def label(self, mid, lab):
        self._req("PATCH", f"/messages/{quote(mid, safe='')}", json={"add_labels": [lab]})


# ---------------- helpers ----------------
def sender_name(from_header, fam):
    addr = parseaddr(from_header or "")[1].strip().lower()
    if "@" not in addr:
        return None
    local = addr.rsplit("@", 1)[0]
    digits = re.sub(r"\D", "", local)
    for p in fam:
        if addr in [e.lower() for e in p.get("emails", [])]:
            return p["name"]
    for p in fam:
        for ph in p.get("phones", []):
            if ph and ph in digits and len(digits) <= 11:
                return p["name"]
    return None


PII_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+|\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}|https?://\S+")
JUNK = {"", "(no subject)", "no subject", "mms", "multimedia message", "photo", "image", "picture",
        "pic", "text message", "new message", "sent from my iphone"}


def clean_caption(*cands):
    for s in cands:
        s = (s or "").replace("\r", "")
        s = re.split(r"\n\s*(--\s*$|sent from my|get outlook|on .+ wrote:)", s, flags=re.I | re.M)[0]
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"^(fwd?|re):\s*", "", s, flags=re.I)
        s = PII_RE.sub("", s).strip(" -–:")
        if s and s.lower() not in JUNK and len(s) <= 140:
            return s
    return ""


def parse_ts(ts):
    if not ts:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def exif_taken(img):
    try:
        ex = img.getexif()
        v = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
        return datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S") if v else None
    except Exception:
        return None


def clean_image(data, dst):
    """Auto-orient, resize, re-encode as a fresh JPEG with no metadata (EXIF/GPS/XMP)."""
    with Image.open(io.BytesIO(data)) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        taken = exif_taken(im)
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
        fresh = Image.new("RGB", im.size)
        fresh.paste(im)
        fresh.save(dst, "JPEG", quality=QUALITY, optimize=True, progressive=True)
        return fresh.size, taken


def is_image_att(a):
    ct = (a.get("content_type") or "").lower()
    ext = os.path.splitext(a.get("filename") or "")[1].lower()
    return ct.startswith("image/") or ext in IMAGE_EXT


def is_text_att(a):
    ct = (a.get("content_type") or "").lower()
    return ct.startswith("text/plain") and (a.get("size") or 0) < 4000


def key_for(mid):
    return hashlib.sha256(("ndrson:" + mid).encode()).hexdigest()[:16]


def load_posts():
    p = os.path.join(ROOT, "posts.json")
    return json.load(open(p)).get("posts", []) if os.path.exists(p) else []


def save_posts(posts):
    posts.sort(key=lambda p: p["ts"], reverse=True)
    with open(os.path.join(ROOT, "posts.json"), "w") as f:
        json.dump({"posts": posts}, f, indent=1)
        f.write("\n")


# ---------------- page ----------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ndrson.com</title>
<meta name="description" content="The Anderson family daily photo, taken at 12:34.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='80'%3E%F0%9F%93%B7%3C/text%3E%3C/svg%3E">
<style>
  :root {{ --bg:#faf8f5; --fg:#222; --muted:#8a847c; --accent:#c8553d; --card:#fff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#141312; --fg:#eee; --muted:#9a948c; --card:#1e1d1b; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  header {{ text-align:center; padding:3rem 1rem 1.5rem; }}
  h1 {{ margin:0; font-size:2.2rem; letter-spacing:.02em; font-weight:700; }}
  .clock {{ font-variant-numeric:tabular-nums; color:var(--accent); font-weight:600; letter-spacing:.08em; }}
  .tag {{ color:var(--muted); margin:.35rem 0 0; font-size:.95rem; }}
  main {{ max-width:720px; margin:0 auto; padding:0 1rem 4rem; }}
  article {{ background:var(--card); border-radius:14px; overflow:hidden; margin:0 0 2rem;
            box-shadow:0 1px 3px rgba(0,0,0,.08), 0 6px 20px rgba(0,0,0,.05); }}
  article img {{ display:block; width:100%; height:auto; background:#ddd; }}
  .meta {{ display:flex; justify-content:space-between; align-items:baseline; gap:1rem; padding:.8rem 1rem .2rem; }}
  .who {{ font-weight:600; }}
  .when {{ color:var(--muted); font-size:.9rem; white-space:nowrap; }}
  .badge {{ color:var(--accent); font-weight:600; font-size:.8rem; margin-left:.4rem; letter-spacing:.05em; }}
  .cap {{ padding:0 1rem .9rem; margin:0; }}
  article .pad {{ height:.6rem; }}
  .empty {{ text-align:center; color:var(--muted); padding:4rem 1rem; }}
  .empty .clock {{ font-size:3rem; display:block; margin-bottom:.5rem; }}
  footer {{ text-align:center; color:var(--muted); font-size:.85rem; padding:0 1rem 3rem; }}
</style>
</head>
<body>
<header>
  <h1>ndrson.com</h1>
  <p class="tag">a photo a day at <span class="clock">12:34</span></p>
</header>
<main>
{body}
</main>
<footer>{count}</footer>
</body>
</html>
"""


def render():
    posts = sorted(load_posts(), key=lambda p: p["ts"], reverse=True)
    if not posts:
        body = ('<div class="empty"><span class="clock">12:34</span>'
                'No photos yet. The first one is coming soon.</div>')
    else:
        parts = []
        for i, p in enumerate(posts):
            local = datetime.fromisoformat(p["ts"]).astimezone(TZ)
            date_str = local.strftime("%A, %B %-d, %Y")
            badge = '<span class="badge">12:34</span>' if p.get("at1234") else ""
            cap = html.escape(p.get("caption") or "")
            alt = html.escape(f"Photo from {p['name']}, {date_str}")
            parts.append(
                f'<article id="{p["id"]}">\n'
                f'  <img src="{html.escape(p["src"])}" width="{p.get("w", "")}" height="{p.get("h", "")}" '
                f'loading="{"eager" if i < 2 else "lazy"}" decoding="async" alt="{alt}">\n'
                f'  <div class="meta"><span class="who">{html.escape(p["name"])}{badge}</span>'
                f'<time class="when" datetime="{local.isoformat(timespec="minutes")}">{date_str}</time></div>\n'
                + (f'  <p class="cap">{cap}</p>\n' if cap else '  <div class="pad"></div>\n')
                + '</article>')
        body = "\n".join(parts)
    n = len(posts)
    with open(os.path.join(ROOT, "index.html"), "w") as f:
        f.write(PAGE.format(body=body, count=f"{n} photo{'s' if n != 1 else ''}" if n else ""))


# ---------------- commands ----------------
def cmd_fetch(a):
    fam, mail = family(), Mail()
    posts = load_posts()
    have = {p["id"] for p in posts}
    pending = {"posted": [], "ignored": []}
    stats = dict(seen=0, new=0, already_posted=0, posted_msgs=0, photos=0, ignored=0, errors=0)
    os.makedirs(os.path.join(ROOT, "photos"), exist_ok=True)
    for item in mail.list_all():
        stats["seen"] += 1
        labels = set(item.get("labels") or [])
        if POSTED in labels or IGNORED in labels or "sent" in labels:
            continue
        stats["new"] += 1
        mid = item["message_id"]
        name = sender_name(item.get("from"), fam)
        if not name:
            pending["ignored"].append(mid); stats["ignored"] += 1
            continue
        key = key_for(mid)
        if any(h.startswith(key + "-") for h in have):
            # Already on the site (labelling failed earlier): never re-post, just retry the label.
            pending["posted"].append(mid); stats["already_posted"] += 1
            continue
        try:
            msg = mail.get(mid)
            local = parse_ts(msg.get("timestamp")).astimezone(TZ)
            atts = msg.get("attachments") or []
            texts = []
            for t in [x for x in atts if is_text_att(x)][:2]:
                try:
                    texts.append(mail.attachment(mid, t["attachment_id"]).decode("utf-8", "ignore"))
                except Exception:
                    pass
            caption = clean_caption(msg.get("extracted_text"), msg.get("text"), *texts, msg.get("subject"))
            made = 0
            for idx, att in enumerate([x for x in atts if is_image_att(x)]):
                pid = f"{key}-{idx}"
                if pid in have:
                    made += 1
                    continue
                data = mail.attachment(mid, att["attachment_id"])
                rel = f"photos/{local.strftime('%Y-%m-%d-%H%M')}-{hashlib.sha256(pid.encode()).hexdigest()[:8]}.jpg"
                try:
                    (w, h), taken = clean_image(data, os.path.join(ROOT, rel))
                except Exception:
                    continue  # not a decodable image
                at1234 = (taken is not None and taken.hour in (0, 12) and taken.minute == 34) or \
                         (local.hour == 12 and local.minute in (34, 35))
                posts.append({"id": pid, "ts": local.isoformat(timespec="seconds"), "name": name, "src": rel,
                              "w": w, "h": h, "caption": caption if made == 0 else "", "at1234": bool(at1234)})
                have.add(pid); made += 1; stats["photos"] += 1
            if made:
                pending["posted"].append(mid); stats["posted_msgs"] += 1
            else:
                pending["ignored"].append(mid); stats["ignored"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"error processing a message: {type(e).__name__}")
    if stats["photos"]:
        save_posts(posts)
        render()
    with open(a.pending, "w") as f:
        json.dump(pending, f)
    print("summary " + " ".join(f"{k}={v}" for k, v in stats.items()))
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"photos={stats['photos']}\n")
    return 0


def cmd_label(a):
    """Best-effort labelling. Never fails the run: posts.json dedupe (by hashed message id)
    already guarantees nothing is posted twice if a label can't be applied."""
    if not os.path.exists(a.pending):
        print("summary labelled=0"); return 0
    pending = json.load(open(a.pending))
    mail, done, failed, reason = Mail(), 0, 0, ""
    for lab, ids in ((POSTED, pending.get("posted", [])), (IGNORED, pending.get("ignored", []))):
        for mid in ids:
            if reason == "HTTP 403":
                failed += 1; continue  # key lacks permission; don't hammer the API
            try:
                mail.label(mid, lab); done += 1
            except Exception as e:
                failed += 1
                m = re.search(r"HTTP \d+", str(e)); reason = m.group(0) if m else type(e).__name__
    print(f"summary labelled={done} label_failures={failed}")
    if failed:
        hint = " (API key lacks the message_update permission)" if reason == "HTTP 403" else ""
        print(f"::warning::Could not label {failed} message(s): {reason}{hint}. Dedupe prevents re-posting.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("fetch", "label"):
        sp.add_parser(c).add_argument("--pending", required=True)
    sp.add_parser("render")
    a = ap.parse_args()
    if a.cmd == "render":
        render(); return 0
    return {"fetch": cmd_fetch, "label": cmd_label}[a.cmd](a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # never let a traceback (URLs contain the inbox id) reach public logs
        print(f"fatal: {type(e).__name__}")
        sys.exit(1)
