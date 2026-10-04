#!/usr/bin/env python3
"""Email building blocks in the Ohmz Cloud visual language, shared by every HTML email we send.

Lifted from /usr/local/sbin/backup_report_html.py, the backup report the user already likes, so a
system alert and a backup report read as one family. That file is root-owned and renders one
report; this one exposes its pieces. The tokens are the SAME values (tests/test_ohmz_email.py
compares them against the backup renderer whenever it is readable), and the mechanics are the
same too, for the reasons that file gives:

  * Tables with inline styles only. A mail client is not a browser: no flexbox or grid (Outlook's
    Word engine), no <style> block to depend on, no webfonts (Space Grotesk is named first and
    degrades to the system stack), no external images.
  * Status colours stay real. ohmz.css re-points blue, sky and indigo at the amber family but
    leaves red and green alone, because "destructive and success still need to read as
    themselves". DOWN is red, RECOVERED is green, never two shades of amber.
  * Never pure white or pure black on warm greys: #f0edea and #1a1917.

Contrast measured by the backup renderer against --ohmz-panel: #86c06c 7.64, #f87171 5.94,
#edaa5f 8.23, all clearing AA.

Every function returns an HTML string. Functions named for a panel ROW (masthead, title_block,
stat_tiles, section, footer) return a `<tr>` for shell(); the rest are fragments to put inside a
section. Everything a caller passes as text is escaped here, so no caller has to remember to.

Stdlib only.
"""
import html

# --- tokens, verbatim from backup_report_html.py (itself verbatim from tokens.css) ----------
CANVAS, PANEL, RAISE, HOVER = "#1a1917", "#211f1d", "#262421", "#2d2a26"
LINE, LINE_SOFT = "#3a3733", "#302d2a"
TEXT, SECONDARY, MUTED = "#f0edea", "#cbc5be", "#8b857e"
AMBER, AMBER_BRIGHT, ON_AMBER = "#e0913f", "#edaa5f", "#241f18"
GREEN, RED = "#86c06c", "#f87171"
RADIUS = "12px"
FONT = ("'Space Grotesk',ui-sans-serif,system-ui,-apple-system,'Segoe UI',"
        "Helvetica,Arial,sans-serif")
MONO = "'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace"

# The backup renderer's phase bars use this for "not the long pole": --ohmz-line on a
# --ohmz-line-soft track is a 1.1:1 difference and disappears, tertiary reads.
TERTIARY = "#7d7770"

_TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'


def esc(v):
    """HTML-escape anything, None as empty. The one escape every block uses."""
    return html.escape(str(v if v is not None else ""))


def dur(sec):
    """Compact duration for a stat tile: 45s, 5m03s, 1h05m, 2d03h. Same shape as the backup
    report's, extended to days because an outage can outlast a backup."""
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h"
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def masthead(word, right_text=""):
    """The top row: "Ω Ohmz <word>" with <word> in amber, right_text in mono on the right.
    Backups use "Backups"; system alerts use "Stack"."""
    return f"""
      <tr><td style="padding:22px 28px;border-bottom:1px solid {LINE_SOFT};">
        <table {_TABLE} width="100%">
          <tr>
            <td align="left" style="font:600 15px/1 {FONT};color:{TEXT};letter-spacing:-.01em;">
              <span style="color:{AMBER};font-size:17px;">&#937;</span>&nbsp; Ohmz
              <span style="color:{AMBER};">{esc(word)}</span>
            </td>
            <td align="right" style="font:500 12px/1 {MONO};color:{MUTED};">
              {esc(right_text)}
            </td>
          </tr></table></td></tr>"""


def verdict_pill(text, colour):
    """The rounded status pill. ON_AMBER text reads on every status colour (it is a near-black
    warm grey), which is why the backup report uses it on green and red as well as amber."""
    return f"""
        <table {_TABLE}>
          <tr><td style="background:{colour};border-radius:999px;padding:5px 13px;
                         font:600 11px/1 {FONT};letter-spacing:.1em;text-transform:uppercase;
                         color:{ON_AMBER};white-space:nowrap;">{esc(text)}</td></tr>
        </table>"""


def title_block(title, subtitle="", mono="", pill=""):
    """Row: optional pill (pre-rendered verdict_pill html), the big title, then a secondary
    subtitle with an optional mono part after a middle dot (backup: host · target path)."""
    sub = esc(subtitle)
    if mono:
        sep = " &nbsp;&middot;&nbsp; " if subtitle else ""
        sub += (f'{sep}<span style="font-family:{MONO};font-size:12px;">{esc(mono)}</span>')
    sub_html = (f"""
        <div style="font:400 13px/1.5 {FONT};color:{SECONDARY};padding-top:6px;">
          {sub}
        </div>""" if sub else "")
    pad = "16px 0 0 0" if pill else "0"
    return f"""
      <tr><td style="padding:30px 28px 0 28px;">{pill}
        <div style="font:600 27px/1.2 {FONT};color:{TEXT};letter-spacing:-.02em;
                    padding:{pad};">{esc(title)}</div>{sub_html}</td></tr>"""


def _stat(value, label, accent, width):
    return f"""
      <td width="{width}%" valign="top" style="width:{width}%;padding:0 6px;">
        <table {_TABLE} width="100%"
               style="width:100%;background:{RAISE};border:1px solid {LINE_SOFT};
                      border-radius:10px;">
          <tr><td style="padding:14px 12px;text-align:center;">
            <div style="font:600 20px/1.15 {FONT};color:{accent or TEXT};
                        letter-spacing:-.01em;white-space:nowrap;">{esc(value)}</div>
            <div style="font:500 10px/1 {FONT};letter-spacing:.1em;text-transform:uppercase;
                        color:{MUTED};padding-top:7px;">{esc(label)}</div>
          </td></tr></table></td>"""


def stat_tiles(tiles):
    """Row of up to 4 headline numbers. Each tile is (value, label) or (value, label, accent).
    More than 4 are dropped (the row is 600px; a fifth tile wraps its value). Fewer share the
    width evenly. An empty list renders nothing."""
    tiles = [t for t in (tiles or []) if t][:4]
    if not tiles:
        return ""
    width = 100 // len(tiles)
    cells = "".join(_stat(t[0], t[1], t[2] if len(t) > 2 else None, width) for t in tiles)
    return f"""
      <tr><td style="padding:24px 22px 0 22px;">
        <table {_TABLE} width="100%">
          <tr>{cells}
          </tr></table></td></tr>"""


def section(title, inner):
    """A titled row. The label is the one place MUTED is allowed: it is an 11px tracked heading,
    not body text, so the AA body-text bar does not apply. `inner` is trusted HTML."""
    return f"""
      <tr><td style="padding:28px 28px 0 28px;">
        <div style="font:600 11px/1 {FONT};letter-spacing:.14em;text-transform:uppercase;
                    color:{MUTED};padding-bottom:14px;">{esc(title)}</div>
        {inner}
      </td></tr>"""


def bar(pct, colour, height=6, track=LINE_SOFT):
    """A proportional bar as a two-cell table: the one bar that renders everywhere."""
    pct = max(0, min(100, int(round(pct or 0))))
    left = f"""<td width="{pct}%" style="width:{pct}%;background:{colour};
               border-radius:{height}px;font-size:0;line-height:0;">&nbsp;</td>""" if pct > 0 else ""
    right = """<td style="font-size:0;line-height:0;">&nbsp;</td>""" if pct < 100 else ""
    return f"""
      <table {_TABLE} width="100%"
             style="width:100%;background:{track};border-radius:{height}px;height:{height}px;">
        <tr style="height:{height}px;">{left}{right}</tr></table>"""


def message_card(label, text, colour):
    """The left-border card from the backup report's "Needs attention" block: a tracked label in
    the status colour over a line of body text. Self-contained, so cards stack by concatenation."""
    return f"""
      <table {_TABLE} width="100%" style="width:100%;">
        <tr><td style="padding:0 0 9px 0;">
          <table {_TABLE} width="100%"
                 style="width:100%;background:{RAISE};border-radius:9px;
                        border-left:3px solid {colour};">
            <tr><td style="padding:11px 13px;">
              <span style="font:600 10px/1.5 {FONT};letter-spacing:.1em;color:{colour};
                           text-transform:uppercase;">{esc(label)}</span>
              <div style="font:400 13px/1.55 {FONT};color:{SECONDARY};padding-top:4px;">
                {esc(text)}</div>
            </td></tr></table></td></tr></table>"""


def check_list(items, caption=""):
    """Rows of (name, ok, note): a green check or red cross, the name, and the note in mono on
    the right. `ok` may also be None for "not probed" (a muted dash). Hairlines between rows,
    as in the backup report's phase list, so a long list stays scannable."""
    rows = ""
    for i, it in enumerate(items or []):
        name, ok = it[0], it[1]
        note = it[2] if len(it) > 2 else ""
        mark, col = (("&#10003;", GREEN) if ok else ("&#10007;", RED)) if ok is not None \
            else ("&ndash;", MUTED)
        top = f"border-top:1px solid {LINE_SOFT};" if i else ""
        rows += f"""
          <tr>
            <td width="18" valign="top" style="width:18px;{top}padding:8px 0;
                       font:600 13px/1.4 {FONT};color:{col};">{mark}</td>
            <td valign="top" style="{top}padding:8px 8px 8px 0;font:400 13px/1.4 {FONT};
                       color:{TEXT if ok is False else SECONDARY};">{esc(name)}</td>
            <td align="right" valign="top" style="{top}padding:8px 0 8px 12px;
                       font:500 12px/1.4 {MONO};color:{RED if ok is False else MUTED};">{esc(note)}</td>
          </tr>"""
    return f"""{_caption(caption)}
      <table {_TABLE} width="100%" style="width:100%;">{rows}
      </table>"""


def _caption(text):
    return (f"""<div style="font:500 12px/1.4 {FONT};color:{TEXT};padding:4px 0 8px 0;">
            {esc(text)}</div>""" if text else "")


def kv_list(pairs, caption=""):
    """Label on the left in secondary, value on the right in mono: a timeline or a fact sheet.
    Each pair is (label, value) or (label, value, colour); a pair whose value is None is skipped
    so callers can list optional facts without branching."""
    rows = ""
    for p in pairs or []:
        if p is None or p[1] is None:
            continue
        col = p[2] if len(p) > 2 and p[2] else TEXT
        rows += f"""
          <tr>
            <td valign="top" style="padding:0 0 9px 0;font:400 13px/1.4 {FONT};
                       color:{SECONDARY};">{esc(p[0])}</td>
            <td align="right" valign="top" style="padding:0 0 9px 12px;font:500 12px/1.4 {MONO};
                       color:{col};">{esc(p[1])}</td>
          </tr>"""
    return f"""{_caption(caption)}
      <table {_TABLE} width="100%" style="width:100%;">{rows}
      </table>"""


def note(text, mark=None, colour=None):
    """A line of body text, optionally led by a coloured mark (the backup report's
    "&#10003; Nothing to report" line). `mark` is an HTML entity, trusted."""
    lead = (f'<span style="color:{colour or GREEN};">{mark}</span>&nbsp; ' if mark else "")
    return f"""
      <div style="font:400 13px/1.6 {FONT};color:{SECONDARY};">{lead}{esc(text)}</div>"""


def pre_block(lines):
    """Verbatim log lines in mono on a raised panel. <pre> is avoided because several clients
    ignore its wrapping and blow the 600px panel out sideways; explicit <br> keeps each line a
    line and lets long ones wrap."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    body = "<br>".join(esc(l) if l else "&nbsp;" for l in (lines or []))
    return f"""
      <table {_TABLE} width="100%"
             style="width:100%;background:{RAISE};border:1px solid {LINE_SOFT};border-radius:9px;">
        <tr><td style="padding:12px 13px;font:400 11px/1.65 {MONO};color:{SECONDARY};
                       word-wrap:break-word;overflow-wrap:break-word;word-break:break-word;">
          {body}</td></tr></table>"""


def footer(lines):
    """Closing row in mono, one entry per line. The backup report puts its log path here;
    prose goes here too, so words wrap whole (break-word) rather than mid-word (break-all),
    and only an unbreakable token such as a path is split."""
    if isinstance(lines, str):
        lines = [lines]
    body = "<br>".join(esc(l) for l in (lines or []) if l)
    return f"""
      <tr><td style="padding:26px 28px 24px 28px;">
        <div style="border-top:1px solid {LINE_SOFT};padding-top:16px;
                    font:400 11px/1.7 {MONO};color:{MUTED};
                    word-wrap:break-word;overflow-wrap:break-word;word-break:break-word;">
          {body}
        </div></td></tr>"""


def shell(title, rows, footnote):
    """The full document: canvas table, the 600px panel holding `rows` (a list of row strings or
    one string), and the small footnote line under the panel. `title` is the <title>, which
    some clients show as the preview heading."""
    if not isinstance(rows, str):
        rows = "".join(r for r in rows if r)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};">
<table {_TABLE} width="100%"
       bgcolor="{CANVAS}" style="background:{CANVAS};margin:0;padding:0;">
  <tr><td align="center" style="padding:28px 14px;">
    <table {_TABLE} width="600"
           bgcolor="{PANEL}" style="width:600px;max-width:600px;background:{PANEL};
                  border:1px solid {LINE_SOFT};border-radius:{RADIUS};overflow:hidden;">
      {rows}
    </table>
    <div style="font:400 11px/1.6 {FONT};color:{MUTED};padding-top:14px;">
      {esc(footnote)}
    </div>
  </td></tr></table></body></html>"""
