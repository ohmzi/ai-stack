#!/usr/bin/env python3
"""ohmz_email: the backup report's email blocks, shared. Offline, no mail sent.

Why this file exists. The point of the module is that a health alert and a backup report look
like one family, and the user already likes the backup report. That promise breaks silently:
a token drifts, a block loses its inline style, and the alert looks "almost" right in one mail
client and broken in Outlook. So the tokens and the load-bearing markup are pinned against the
root-owned renderer itself (/usr/local/sbin/backup_report_html.py) whenever it is readable, and
the email-safety rules are pinned everywhere:

  * no <style> block, no flexbox or grid (Outlook's Word engine), no external images, no URLs;
  * everything a caller passes is escaped, because alert details quote log lines and a log
    line can contain markup;
  * the footer wraps prose by word (the backup footer's break-all is for a log path).

Usage:  python3 tests/test_ohmz_email.py
"""
import importlib.util
import os
import re
import sys

results = []

BACKUP_HTML = "/usr/local/sbin/backup_report_html.py"


def check(label, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def main():
    oe = load("/home/ohmz/StudioProjects/ai-stack/scripts/ohmz_email.py", "oe")

    print("--- tokens are the backup report's, value for value ---")
    names = ("CANVAS", "PANEL", "RAISE", "HOVER", "LINE", "LINE_SOFT", "TEXT", "SECONDARY",
             "MUTED", "AMBER", "AMBER_BRIGHT", "ON_AMBER", "GREEN", "RED", "RADIUS", "FONT",
             "MONO")
    pinned = {"CANVAS": "#1a1917", "PANEL": "#211f1d", "RAISE": "#262421", "HOVER": "#2d2a26",
              "LINE": "#3a3733", "LINE_SOFT": "#302d2a", "TEXT": "#f0edea",
              "SECONDARY": "#cbc5be", "MUTED": "#8b857e", "AMBER": "#e0913f",
              "AMBER_BRIGHT": "#edaa5f", "ON_AMBER": "#241f18", "GREEN": "#86c06c",
              "RED": "#f87171", "RADIUS": "12px"}
    for k, v in pinned.items():
        check(f"{k} = {v}", getattr(oe, k) == v, getattr(oe, k))
    check("FONT names Space Grotesk first", oe.FONT.startswith("'Space Grotesk'"), oe.FONT)
    check("MONO names IBM Plex Mono first", oe.MONO.startswith("'IBM Plex Mono'"), oe.MONO)

    b = None
    if os.access(BACKUP_HTML, os.R_OK):
        b = load(BACKUP_HTML, "backup_report_html")
        for k in names:
            check(f"{k} matches backup_report_html", getattr(oe, k) == getattr(b, k),
                  f"{getattr(oe, k)!r} vs {getattr(b, k)!r}")
    else:
        print(f"  (skip) {BACKUP_HTML} not readable; parity checks not run")

    if b:
        print("--- the load-bearing blocks render the backup report's markup ---")
        st = {"result": "ok", "job": "ai-stack", "host": "ohmz-homelab",
              "finished": "2026-09-29 03:12", "files_summary": "412 files", "duration_seconds": 100}
        h = norm(b.render(st, "/var/log/backup.log"))
        check("masthead('Backups') is the backup masthead",
              norm(oe.masthead("Backups", "2026-09-29 03:12")) in h)
        check("verdict_pill is the backup pill",
              norm(oe.verdict_pill("Succeeded", oe.GREEN)) in h)
        check("a 4-up stat tile is the backup tile",
              norm(oe._stat("1m40s", "took", None, 25)) in h)
        inner = f"""
          <div style="font:400 13px/1.6 {oe.FONT};color:{oe.SECONDARY};">412 files</div>"""
        check("section is the backup section", norm(oe.section("Files", inner)) in h)

    print("--- escaping ---")
    check("esc escapes markup and quotes", oe.esc("<b>&\"'") == "&lt;b&gt;&amp;&quot;&#x27;",
          oe.esc("<b>&\"'"))
    check("esc(None) is empty, not 'None'", oe.esc(None) == "")
    evil = "<script>alert(1)</script>"
    for name, html in [("masthead", oe.masthead(evil, evil)),
                       ("verdict_pill", oe.verdict_pill(evil, oe.RED)),
                       ("title_block", oe.title_block(evil, evil, evil)),
                       ("stat_tiles", oe.stat_tiles([(evil, evil)])),
                       ("section title", oe.section(evil, "")),
                       ("message_card", oe.message_card(evil, evil, oe.RED)),
                       ("check_list", oe.check_list([(evil, False, evil)], evil)),
                       ("kv_list", oe.kv_list([(evil, evil)], evil)),
                       ("note", oe.note(evil)),
                       ("pre_block", oe.pre_block([evil])),
                       ("footer", oe.footer([evil])),
                       ("shell", oe.shell(evil, [], evil))]:
        check(f"{name} escapes caller text", "<script>" not in html)

    print("--- blocks ---")
    m = oe.masthead("Stack", "2026-09-29 15:50")
    check("masthead carries the omega", "&#937;" in m)
    check("masthead word is amber", f'<span style="color:{oe.AMBER};">Stack</span>' in m, m)
    check("masthead right text in mono", oe.MONO in m and "2026-09-29 15:50" in m)
    p = oe.verdict_pill("Down", oe.RED)
    check("pill background is the status colour", f"background:{oe.RED}" in p)
    check("pill text is ON_AMBER", f"color:{oe.ON_AMBER}" in p)
    t = oe.title_block("Web search", "ohmz-homelab", "127.0.0.1:8888", p)
    check("title block carries pill, title, subtitle",
          "Down" in t and "Web search" in t and "ohmz-homelab" in t)
    check("title block mono part in MONO", f"font-family:{oe.MONO}" in t and "127.0.0.1:8888" in t)
    check("no subtitle div when there is no subtitle",
          f"color:{oe.SECONDARY};padding-top:6px" not in oe.title_block("x"))
    tiles = oe.stat_tiles([(str(i), f"l{i}") for i in range(6)])
    check("stat_tiles caps at 4", tiles.count("text-transform:uppercase") == 4,
          tiles.count("text-transform:uppercase"))
    check("4 tiles are 25% each", 'width="25%"' in tiles)
    check("2 tiles share the row at 50%", 'width="50%"' in oe.stat_tiles([("1", "a"), ("2", "b")]))
    check("an accent colours the value", f"color:{oe.RED}" in oe.stat_tiles([("1", "a", oe.RED)]))
    check("no tiles renders nothing", oe.stat_tiles([]) == "")
    full = oe.bar(150, oe.AMBER)
    check("bar clamps above 100", 'width="100%" style="width:100%;background:' + oe.AMBER in full
          and full.count("<td") == 1, full)
    check("bar clamps below 0", 'background:' + oe.AMBER not in oe.bar(-5, oe.AMBER))
    card = oe.message_card("Down · API", "no HTTP answer", oe.RED)
    check("message card has the left status border", f"border-left:3px solid {oe.RED}" in card)
    check("message card label in status colour", f"color:{oe.RED}" in card and "Down · API" in card)
    cl = oe.check_list([("bing", False, "timeout"), ("mojeek", True, "7"), ("x", None, "")])
    check("check list: green check for ok", f"color:{oe.GREEN};\">&#10003;" in cl, cl)
    check("check list: red cross for a failure", f"color:{oe.RED};\">&#10007;" in cl)
    check("check list: muted dash for not probed", "&ndash;" in cl)
    kv = oe.kv_list([("First failure", "15:17"), ("Recovered", None)])
    check("kv_list skips a None value", "First failure" in kv and "Recovered" not in kv)
    f = oe.footer(["Sent after 3 failed checks.", "Inspect: journalctl --user -u x"])
    check("footer joins lines with <br> in mono", "<br>" in f and oe.MONO in f)
    check("footer wraps by word, not mid-word", "break-all" not in f and "break-word" in f)
    pre = oe.pre_block("line one\nline two")
    check("pre_block keeps lines as lines without <pre>", "line one<br>line two" in pre
          and "<pre" not in pre)
    check("dur matches the backup shape", (oe.dur(45), oe.dur(303), oe.dur(3900))
          == ("45s", "5m03s", "1h05m"), (oe.dur(45), oe.dur(303), oe.dur(3900)))
    check("dur extends to days", oe.dur(2 * 86400 + 3 * 3600) == "2d03h", oe.dur(2 * 86400 + 3 * 3600))

    print("--- shell: an email, not a web page ---")
    doc = oe.shell("DOWN · Web search", [m, t, oe.section("What's wrong", card),
                                         oe.footer("x")], "Ohmz Cloud · automated health check")
    check("starts with a doctype", doc.startswith("<!doctype html>"))
    check("declares utf-8", '<meta charset="utf-8">' in doc)
    check("600px panel", 'width="600"' in doc and "max-width:600px" in doc)
    check("canvas background on the body and the outer table",
          f"<body style=\"margin:0;padding:0;background:{oe.CANVAS};\">" in doc
          and f'bgcolor="{oe.CANVAS}"' in doc)
    check("footnote sits UNDER the panel",
          doc.index("automated health check") > doc.index("</table>", doc.index('width="600"')))
    for bad, why in [("<style", "a <style> block"), ("display:flex", "flexbox"),
                     ("display:grid", "grid"), ("<img", "an image"), ("http://", "a URL"),
                     ("https://", "a URL"), ("<link", "a stylesheet link"),
                     ("<script", "a script")]:
        check(f"no {why}", bad not in doc.lower())
    check("rows may be one string", "Timeline" in oe.shell("t", oe.section("Timeline", ""), "f"))

    fails = results.count(False)
    print(f"\n{len(results)} checks — {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
    return 1 if fails else 0


sys.exit(main())
