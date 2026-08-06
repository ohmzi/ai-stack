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
    """A number formatted the way its unit is actually written.

    Not everything watched is money: a CPU threshold rendered "91.00 %" reads like a machine wrote
    it. Currency keeps two decimals because $46.9 is wrong; a percentage or a bare count does not.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    u = (unit or "").strip()
    plain = f"{f:,.0f}" if f == int(f) else f"{f:,.2f}".rstrip("0").rstrip(".")
    if u == "%":
        return f"{plain}%"
    if u in ("$", "£", "€", "¥"):
        return f"{u}{f:,.2f}"
    if len(u) == 3 and u.isalpha():          # CAD, USD, GBP
        return f"{f:,.2f} {u}"
    return f"{plain} {u}" if u else plain


def _thing(p):
    """What to call the subject of this alert, or None when nothing usable was supplied.

    Returning None rather than a stock phrase matters: the SMS builds "the listing you're tracking
    - {thing} -", and a fallback phrase there produced "the listing you're tracking - the page
    you're tracking -". When there is no name, the clause is dropped instead.
    """
    return p.get("item") or p.get("monitor") or None


def _noun(p):
    """The word for what kind of thing this is, used in prose.

    "listing" is wrong for half of what a person can ask to be watched, and a message that calls a
    flight a listing reads like it was written by something that did not understand the request.
    Anything genuinely generic falls back to "task you assigned me", which is true of everything.
    """
    return {"fare": "fare", "inventory": "stock level", "availability": "availability",
            "back_in_stock": "item", "out_of_stock": "item", "price_drop": "listing",
            "price_rise": "listing", "unreachable": "page", "blocked": "page",
            "no_value": "page", "not_found": "item",
            "recovered": "page"}.get(p.get("kind"), "task you assigned me")


def _phrase(noun):
    """The noun as a sentence subject.

    "task you assigned me" already contains its own clause, so the generic wrapper produced "the
    task you assigned me you're tracking". A noun that already says what the user did to it does
    not get told again.
    """
    return f"the {noun}" if "you" in noun else f"the {noun} you're tracking"


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
    v = p.get("value")
    s = "is available again"
    # "Only 3 left" is the whole reason someone set a restock watch on a hard-to-get item, so when
    # the page said a number, the text says it too.
    if p.get("state") == "limited" and v is not None:
        s += f" - only {v} left"
    return "Back in stock", s


def _out_of_stock(p):
    # "has just gone out of stock" claims a TRANSITION, and firing is a predicate on the current
    # reading: a first-ever reading of a page that was already sold out would be asserting something
    # about timing that nobody observed. Say it only when a previous reading was available.
    if p.get("prev_state") in ("in_stock", "limited"):
        return "Out of stock", "has just gone out of stock"
    return "Out of stock", "is out of stock"


def _fare(p):
    v, t = money(p.get("value"), p.get("unit")), money(p.get("target"), p.get("unit"))
    if not v:
        return "Fare drop", ("dropped under your " + t + " target" if t else "hit your target")
    s = f"is {v}"
    if t:
        s += f", under your {t} target"
    return "Fare drop", s


def _inventory(p):
    # money() with an empty unit, not the raw value: a count arrives as a number and "3.0 left" is
    # what a bare float looks like once it has been through JSON.
    v, prev, t = p.get("value"), p.get("prev"), p.get("target")
    n = money(v, "")
    if v is None:
        s = "changed"
    elif prev is None:
        s = f"has {n} left"
    elif v < prev:
        s = f"is down to {n} left"
    else:
        s = f"is back up to {n} left"
    if prev is not None and v is not None:
        s += f" (was {money(prev, '')})"
    if t is not None:
        s += f", at or below your {money(t, '')} target" if v is not None and v <= t \
            else f", against your {money(t, '')} target"
    return "Stock level", s


def _availability(p):
    v = p.get("value")
    return "Now available", ("has availability" if v is None else f"has {money(v, '')} available")


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


def _not_found(p):
    return "Can't find it", "couldn't be found by an online search yet"


def _recovered(p):
    v = money(p.get("value"), p.get("unit"))
    return "Back to normal", (f"is readable again, now {v}" if v else "is reachable again")


KINDS = {
    "price_drop": _price_drop, "price_rise": _price_rise,
    "back_in_stock": _back_in_stock, "out_of_stock": _out_of_stock,
    "fare": _fare, "inventory": _inventory, "availability": _availability,
    "threshold": _threshold, "change": _change,
    "unreachable": _unreachable, "blocked": _blocked, "no_value": _no_value,
    "not_found": _not_found, "recovered": _recovered,
}
# Kinds that report a PROBLEM with the monitor rather than a result from it. They read differently
# (something needs your attention, rather than something you asked for happened) and they carry an
# instruction, because an error the user cannot act on is just noise.
PROBLEM_KINDS = {"unreachable", "blocked", "no_value", "not_found"}

ADVICE = {
    "unreachable": "Double-check the link still opens in a browser. If the page moved, ask me to "
                   "set the monitor up again with the new address.",
    "blocked": "The site is blocking automated checks, so this monitor can't keep working. Ask me "
               "to watch a different page for it.",
    "no_value": "The page layout has probably changed. Ask me to set this monitor up again and "
                "I'll re-read it.",
    "not_found": "I searched the web but couldn't find a page for this item. Ask me to set the "
                 "monitor up again with a direct link, or a better description of what to "
                 "look for.",
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

    # Who is texting? The message arrives from a mail-to-SMS gateway, so the handset shows an
    # email address the user has no reason to recognise. Naming the assistant answers the first
    # question a text from an unknown sender raises, which is worth its characters — and it is
    # configured, not hard-coded, so renaming the assistant never means editing this file.
    assistant = _ascii((payload.get("assistant") or "").strip())

    def build(name, greet=True, ident=True):
        lead = f"Hi {who}, " if (greet and who) else ""
        if ident and assistant:
            lead += f"{assistant} here! "
        head = "Heads up - " if problem else ""
        conf = "" if problem else _conf_short(payload)
        # With a greeting and an identity in front, "the listing you're tracking" is ceremony the
        # 140 characters cannot afford — the user knows why they are being texted. The item name
        # carries the meaning on its own.
        subject = name if name else _phrase(noun)
        core = f"{head}{subject} {sentence}{conf}."
        if lead.endswith("! ") or not lead:
            core = core[:1].upper() + core[1:]
        return f"{lead}{core} {pointer}"

    name = thing or ""
    out = build(name)
    if len(out) <= limit:
        return out
    # 1. shorten the item name
    over = len(out) - limit
    if len(name) - over > 16:
        return build(name[:len(name) - over - 3].rstrip(" ,-") + "...")
    name = name[:22].rstrip(" ,-") + "..." if len(name) > 22 else name
    # 2. drop the personal greeting before the identity: on a text from an address the user does
    #    not recognise, "who is this" matters more than "hello by name".
    for kw in ({"greet": False}, {"greet": False, "ident": False}):
        out = build(name, **kw)
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
    assistant = (payload.get("assistant") or "").strip()
    hello = f"Hi {who}," if who else "Hi,"
    lines = [f"{hello} {assistant} here!" if assistant else hello, ""]
    lines.append(f"{_phrase(noun).capitalize()} {sentence}." if not problem
                 else f"One of your monitors needs a look - the {noun} {sentence}.")
    lines[-1] = re.sub(r"\s{2,}", " ", lines[-1])
    if thing:
        lines += ["", f"  {thing}"]
    v = money(payload.get("value"), payload.get("unit"))
    if v:
        lines.append(f"  {v}")
    # The page's own words, when it was an availability that was read rather than a number. This is
    # the "never report a state you didn't read" evidence: a doubtful reading can be checked against
    # the page without anyone having to trust the wording above it.
    if payload.get("state_text"):
        lines.append(f"  the page says: {payload['state_text']}")
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
        if target and payload.get("kind") == "inventory":
            sub = f"at or below your {e(target)} left"
        big = (f'<div style="margin:18px 0 4px;font-size:34px;line-height:1.1;font-weight:700;'
               f'color:{accent};">{e(v)}{delta}</div>'
               + (f'<div style="font-size:14px;color:#6b7280;">{sub}</div>' if sub else ""))

    # e() because a phrase lifted off a page is untrusted input, exactly like a page title.
    said = (f'<div style="margin:6px 0 0;font-size:14px;color:#6b7280;">the page says: '
            f'{e(payload["state_text"])}</div>' if payload.get("state_text") else "")

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

    assistant = (payload.get("assistant") or "").strip()
    lead = ("One of your monitors needs a look." if problem
            else f"{e(_phrase(noun)).capitalize()} just hit your condition.")
    footer = " ".join(_footer_lines(payload))

    return f"""<div style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:10px;padding:28px 26px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
<tr><td>
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{accent};font-weight:700;">{e(headline)}</div>
<div style="margin-top:14px;font-size:15px;color:#374151;">Hi {e(who) or 'there'}{f", {e(assistant)} here!" if assistant else ""} {lead}</div>
{f'<div style="margin-top:14px;font-size:17px;font-weight:600;line-height:1.35;">{e(thing)}</div>' if thing else ''}
{big}{said}{conf_html}{btn}{advice_html}
<div style="margin-top:26px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:12px;color:#9ca3af;line-height:1.6;">{e(footer)}</div>
</td></tr></table>
</td></tr></table></div>"""
