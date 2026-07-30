#!/usr/bin/env python3
"""What an alert SAYS — one place, driven by structured data rather than string-building at source.

Why this exists. Alerts used to be whatever sentence the emitting job happened to print, wrapped in
a subject line built by slicing that sentence. It produced texts like

    amazon B0DP6D3TRB price: 46.99, under your 50.00 target (high confidence) Link in email.

which names a job id and a source ID, and never names the thing the user is actually watching. The
person reading it at 7am wants to know WHAT got cheap, not which cron entry fired.

So a job now emits a payload describing what happened, and rendering lives here:

    {"to": "ohmz", "kind": "price_drop", "item": "Zakkart Cat Scratching Board",
     "value": 46.99, "prev": 49.99, "target": 50.0, "unit": "$", "url": "https://...",
     "confidence": "high", "monitor": "amazon B0DP6D3TRB price", "schedule": "every 6h"}

Three surfaces come out of it — an SMS (140 ASCII characters, no web addresses), an email subject,
and an HTML email with a plain-text alternative. They are deliberately consistent: a user holding
the text next to the email should see one event described twice, not two events.

Every field is OPTIONAL. A payload with only `kind` and `to` still renders something honest, because
the failure mode being designed against is an alert that cannot be sent at all. Nothing here invents
a value it was not given: `item` comes from the page's own <title>, never from a model.

Kinds are open-ended — an unknown kind falls back to a generic renderer rather than raising, so a
future job type reaches the user before anyone updates this file.
"""
import html as _html
import re

# ---------------------------------------------------------------- small helpers

_SITE_SUFFIX = re.compile(
    r"\s*[|–—-]\s*(?:amazon(?:\.\w+)*|walmart|best ?buy|ebay|etsy|newegg|costco|"
    r"books to scrape.*|sandbox|home ?depot|target|aliexpress|shop|store)\b.*$", re.I)


def item_label(title, limit=52):
    """A page <title> reduced to something a person would call the thing.

    Retail titles are keyword soup — "Zakkart 2-Pack Cat Scratching Board, 65cm Tall Cardboard L
    Shape Vertical Cat Scratchers for Indoor Cats, Cat Scratch Pad..." — written for search, not for
    a lock screen. The useful name is almost always the first clause, so cut at the first comma or
    separator and drop the site name. Deterministic: no model, no guessing, and if the result is
    empty the caller falls back to the monitor name.
    """
    if not title:
        return None
    t = re.sub(r"\s+", " ", str(title)).strip()
    t = _SITE_SUFFIX.sub("", t)
    t = re.split(r"\s*[|]\s*", t)[0]
    head = re.split(r",", t)[0].strip()
    # Only accept the comma-cut if it left something substantial; some titles are one long clause.
    if len(head) < 12:
        head = t
    if len(head) > limit:
        cut = head[:limit].rsplit(" ", 1)[0]
        head = (cut or head[:limit]).rstrip(" ,-") + "…"
    return head.strip() or None


def money(value, unit=None):
    if value is None:
        return None
    try:
        v = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)
    u = (unit or "").strip()
    if u in ("$", "£", "€", "¥"):
        return f"{u}{v}"
    return f"{v} {u}" if u else v


def _thing(p):
    """What to call the subject of this alert, or None when nothing usable was supplied.

    Returning None rather than a stock phrase matters: the SMS builds "the listing you're tracking
    - {thing} -", and a fallback phrase there produced "the listing you're tracking - the page
    you're tracking -". When there is no name, the clause is dropped instead.
    """
    return p.get("item") or p.get("monitor") or None


def _noun(p):
    """The word for what kind of thing this is, used in prose."""
    return {"fare": "fare", "inventory": "stock level", "availability": "availability",
            "back_in_stock": "item", "out_of_stock": "item"}.get(p.get("kind"), "listing")


def _conf_short(p):
    return "" if p.get("confidence") in (None, "high") else " (unconfirmed)"


def _conf_long(p):
    c = p.get("confidence")
    if c in (None, "high"):
        return None
    note = p.get("confidence_note")
    return f"Unconfirmed reading{' - ' + note if note else ''}."


# ---------------------------------------------------------------- per-kind copy
#
# Each entry returns (headline, sentence). `headline` is the short label at the top of the email and
# the lead of the subject; `sentence` is the human line used in the SMS and the email body.

def _price_drop(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    prev = money(p.get("prev"), p.get("unit"))
    if not v:
        return "Price drop", ("dropped under your " + t + " target" if t else "hit your target")
    s = f"is {v}"
    if t:
        s += f", under your {t} target"
    if prev and prev != v:
        s += f" (was {prev})"
    return "Price drop", s


def _price_rise(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    prev = money(p.get("prev"), p.get("unit"))
    if not v:
        return "Price rise", ("rose past your " + t + " threshold" if t else "passed your threshold")
    s = f"is {v}"
    if t:
        s += f", over your {t} threshold"
    if prev and prev != v:
        s += f" (was {prev})"
    return "Price rise", s


def _back_in_stock(p):
    return "Back in stock", "is available again"


def _out_of_stock(p):
    return "Out of stock", "has just gone out of stock"


def _fare(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    if not v:
        return "Fare drop", ("dropped under your " + t + " target" if t else "hit your target")
    s = f"is {v}"
    if t:
        s += f", under your {t} target"
    return "Fare drop", s


def _inventory(p):
    v, prev = p.get("value"), p.get("prev")
    s = f"is down to {v} left" if v is not None else "changed"
    if prev is not None and v is not None:
        s += f" (was {prev})"
    return "Stock level", s


def _availability(p):
    return "Now available", "has availability"


def _threshold(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    op = p.get("op") or "past"
    s = f"is {v}" if v else "met your condition"
    if t:
        s += f", {op} your {t} threshold"
    return "Threshold met", s


def _change(p):
    return "Changed", "has changed since the last check"


def _unreachable(p):
    err = p.get("error")
    s = "can't be reached any more"
    return "Can't reach it", s + (f" ({err})" if err else "")


def _blocked(p):
    return "Blocked by the site", "is refusing automated checks"


def _no_value(p):
    return "Can't read it any more", "still loads, but no longer shows a readable value"


def _recovered(p):
    v = money(p.get("value"), p.get("unit"))
    return "Back to normal", (f"is readable again, now {v}" if v else "is reachable again")


KINDS = {
    "price_drop": _price_drop, "price_rise": _price_rise,
    "back_in_stock": _back_in_stock, "out_of_stock": _out_of_stock,
    "fare": _fare, "inventory": _inventory, "availability": _availability,
    "threshold": _threshold, "change": _change,
    "unreachable": _unreachable, "blocked": _blocked, "no_value": _no_value,
    "recovered": _recovered,
}
# Kinds that report a PROBLEM with the monitor rather than a result from it. They read differently
# (something needs your attention, rather than something you asked for happened) and they carry an
# instruction, because an error the user cannot act on is just noise.
PROBLEM_KINDS = {"unreachable", "blocked", "no_value"}

ADVICE = {
    "unreachable": "Double-check the link still opens in a browser. If the page moved, ask me to "
                   "set the monitor up again with the new address.",
    "blocked": "The site is blocking automated checks, so this monitor can't keep working. Ask me "
               "to watch a different page for it.",
    "no_value": "The page layout has probably changed. Ask me to set this monitor up again and "
                "I'll re-read it.",
}


def describe(payload):
    """(headline, sentence) for any payload, including kinds this file has never heard of."""
    fn = KINDS.get(payload.get("kind"))
    if fn:
        return fn(payload)
    return "Update", "met the condition you set"


# ---------------------------------------------------------------- the three surfaces

# render_sms must measure the string the HANDSET will get, not a prettier one. alert_transports
# folds to ASCII on the way out ("…" becomes "..."), so measuring here with typographic characters
# under-counts and lets the transport's own truncation fire — which cut the pointer to the email,
# the one part that must always survive. Fold first, then count.
_SMS_ASCII = {"\u2026": "...", "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
              "\u201c": '"', "\u201d": '"', "\u00a0": " "}


def _ascii(text):
    for bad, good in _SMS_ASCII.items():
        text = (text or "").replace(bad, good)
    return text.encode("ascii", "ignore").decode("ascii")


def render_sms(payload, limit=140):
    """One ASCII segment, no web addresses, greeting the user by name.

    Degrades in a fixed order when it will not fit: the item name is shortened first, then the
    greeting is dropped, and the pointer to the email is never dropped — it is the only thing
    telling a first-time user where the link went. The caller (alert_transports.sms_body) still
    performs the final address-strip and ASCII fold; this only has to be sensible.
    """
    who = _ascii((payload.get("to") or "").strip())
    headline, sentence = describe(payload)
    sentence = _ascii(sentence)
    thing, noun = _ascii(_thing(payload) or "") or None, _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    pointer = "Details in email." if not payload.get("url") else "Link in email."

    # The previous value belongs in the email, not the buzz — it cost 13 characters and pushed the
    # item name, which is the whole point of the message, into truncation.
    sentence = re.sub(r"\s*\(was [^)]*\)", "", sentence)

    def build(name, greet=True):
        lead = f"Hi {who}, " if (greet and who) else ""
        named = f" - {name} -" if name else ""
        head = "heads up - " if problem else ""
        conf = "" if problem else _conf_short(payload)
        core = f"{head}the {noun} you're tracking{named} {sentence}{conf}."
        return f"{lead}{core} {pointer}"

    name = thing or ""
    out = build(name)
    if len(out) <= limit:
        return out
    # 1. shorten the item name
    over = len(out) - limit
    if len(name) - over > 16:
        return build(name[:len(name) - over - 3].rstrip(" ,-") + "...")
    # 2. drop the greeting
    name = name[:22].rstrip(" ,-") + "..." if len(name) > 22 else name
    out = build(name, greet=False)
    if len(out) <= limit:
        return out
    # 3. last resort: keep the pointer, trim the middle
    return out[:limit - len(pointer) - 2].rstrip() + " " + pointer


def render_subject(payload, limit=120):
    headline, sentence = describe(payload)
    thing = _thing(payload)
    if not thing:
        subj = f"{headline}: your monitor {sentence}"
    elif payload.get("kind") in PROBLEM_KINDS:
        subj = f"{headline}: {thing}"
    else:
        subj = f"{headline}: {thing} {sentence}"
    subj = re.sub(r"\s{2,}", " ", subj).strip()
    return subj[:limit - 1] + "…" if len(subj) > limit else subj


def render_plain(payload):
    """Plain-text alternative. Same information, same order as the HTML."""
    who = (payload.get("to") or "").strip()
    headline, sentence = describe(payload)
    thing, noun = _thing(payload), _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    lines = [f"Hi {who}," if who else "Hi,", ""]
    lines.append(f"The {noun} you're tracking {sentence}." if not problem
                 else f"One of your monitors needs a look - the {noun} {sentence}.")
    lines[-1] = re.sub(r"\s{2,}", " ", lines[-1])
    if thing:
        lines += ["", f"  {thing}"]
    v = money(payload.get("value"), payload.get("unit"))
    if v:
        lines.append(f"  {v}")
    cl = _conf_long(payload)
    if cl:
        lines.append(f"  {cl}")
    if payload.get("url"):
        lines += ["", f"  {payload['url']}"]
    advice = ADVICE.get(payload.get("kind"))
    if advice:
        lines += ["", advice]
    lines += [""] + _footer_lines(payload)
    return "\n".join(lines)


def _footer_lines(payload):
    bits = []
    sched = payload.get("schedule")
    if sched:
        bits.append(f"Checked {sched}")
    if payload.get("texted_to"):
        bits.append(f"a text went to {payload['texted_to']}")
    tail = ". ".join(x[0].upper() + x[1:] for x in [", ".join(bits)] if x)
    out = [tail] if tail else []
    out.append("Reply in the assistant to change or cancel this monitor.")
    return out


def render_html(payload):
    """A self-contained HTML email: inline styles only, no external assets, mobile-friendly.

    Inline styles and a table shell because email clients strip <style> blocks and do not implement
    flexbox. Colours are chosen to stay legible if a client force-inverts for dark mode.
    """
    e = _html.escape
    who = (payload.get("to") or "").strip()
    headline, sentence = describe(payload)
    thing, noun = _thing(payload), _noun(payload)
    problem = payload.get("kind") in PROBLEM_KINDS
    accent = "#b45309" if problem else "#047857"
    v = money(payload.get("value"), payload.get("unit"))
    prev = money(payload.get("prev"), payload.get("unit"))
    target = money(payload.get("target"), payload.get("unit"))

    big = ""
    if v:
        delta = ""
        try:
            if payload.get("prev") is not None and float(payload["prev"]) != float(payload["value"]):
                arrow = "▼" if float(payload["value"]) < float(payload["prev"]) else "▲"
                delta = (f'<span style="font-size:14px;color:#6b7280;font-weight:400;">'
                         f'&nbsp;&nbsp;{arrow} was {e(prev)}</span>')
        except (TypeError, ValueError):
            pass
        sub = f"under your {e(target)} target" if (target and payload.get("kind") in
                                                  ("price_drop", "fare")) else ""
        if target and payload.get("kind") == "price_rise":
            sub = f"over your {e(target)} threshold"
        big = (f'<div style="margin:18px 0 4px;font-size:34px;line-height:1.1;font-weight:700;'
               f'color:{accent};">{e(v)}{delta}</div>'
               + (f'<div style="font-size:14px;color:#6b7280;">{sub}</div>' if sub else ""))

    cl = _conf_long(payload)
    conf_html = (f'<div style="margin-top:10px;font-size:13px;color:#92400e;background:#fffbeb;'
                 f'border-left:3px solid #f59e0b;padding:8px 10px;">{e(cl)}</div>' if cl else "")

    btn = ""
    if payload.get("url"):
        btn = (f'<div style="margin:22px 0 6px;"><a href="{e(payload["url"])}" '
               f'style="display:inline-block;background:{accent};color:#ffffff;text-decoration:none;'
               f'padding:11px 20px;border-radius:6px;font-size:15px;font-weight:600;">'
               f'View the {e(noun)} &rarr;</a></div>')

    advice = ADVICE.get(payload.get("kind"))
    advice_html = (f'<div style="margin-top:16px;font-size:14px;color:#374151;">{e(advice)}</div>'
                   if advice else "")

    lead = (f"One of your monitors needs a look." if problem
            else f"The {e(noun)} you're tracking just hit your condition.")
    footer = " ".join(_footer_lines(payload))

    return f"""<div style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:10px;padding:28px 26px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
<tr><td>
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{accent};font-weight:700;">{e(headline)}</div>
<div style="margin-top:14px;font-size:15px;color:#374151;">Hi {e(who) or 'there'} &mdash; {lead}</div>
{f'<div style="margin-top:14px;font-size:17px;font-weight:600;line-height:1.35;">{e(thing)}</div>' if thing else ''}
{big}{conf_html}{btn}{advice_html}
<div style="margin-top:26px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;line-height:1.6;">{e(footer)}</div>
</td></tr></table>
</td></tr></table></div>"""
