#!/usr/bin/env python3
"""The sign-in page: guest link, brand wordmark, centred logo.

Three changes, all to src/routes/auth/+page.svelte. The filename says "guest_link" because that is
what it started as; it stayed after the other two joined rather than churn the README, the patch and
every reference to it for a rename.

1. A "Continue without an account" link to the public, no-login instance (docs/PUBLIC_INSTANCE.md).
   The URL is not hardcoded here: branding/loader.js sets `window.__ohmzGuestUrl` before this
   component mounts, the same way it rewrites the app name — see that file for why (a stock,
   unbranded build then renders nothing, and the public instance itself never gets a value, so it
   can't link to itself).
2. The heading's app name wrapped in a span, so branding/ohmz.css can colour the "AI" of "Ohmz AI"
   amber to match the sidebar wordmark. The name arrives interpolated into the MIDDLE of a
   translated sentence — one text node — so there is nothing for CSS to select without this.
3. The logo moved out of the fixed top-left corner and into the card, above the heading.

Deliberately a vendored source patch rather than DOM injection into the running page, matching how
01-03 handle the composer. For (2) that is not a preference but a correctness requirement: the
heading is a REACTIVE text node, and replacing it with an element from outside Svelte breaks the
runtime's reference to it, so the heading silently stops updating the next time `mode` flips
between signin and signup. The general argument still applies to all three: `git apply` fails loudly
on a moved anchor, in the build, instead of quietly in someone's browser.

Run after 01-03; appends to the same combined patch.
"""
import os, subprocess, sys

SC = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SC, "auth+page.svelte")
s = open(SRC, encoding="utf-8").read()


def sub(old, new, why, count=1):
    global s
    n = s.count(old)
    assert n == count, f"anchor matched {n}x (expected {count}): {why}\n---\n{old[:180]}"
    s = s.replace(old, new)


# 1. Read the URL loader.js set on `window`, once, at component init. Absent (stock build, or the
#    public instance itself) it's '', and the {#if guestUrl} below renders nothing.
sub(
    """	let mode = $config?.features.enable_ldap ? 'ldap' : 'signin';""",
    """	let mode = $config?.features.enable_ldap ? 'ldap' : 'signin';

	// ai-stack: set by branding/loader.js from window.__ohmzGuestUrl, which is itself only set on
	// the branded (private) instance — see that file for the hostname guard. '' here renders
	// nothing, which is what a stock/unbranded build and the public instance itself both get.
	const guestUrl =
		(typeof window !== 'undefined' && window.__ohmzGuestUrl) || '';

	// ai-stack: split a "… {{WEBUI_NAME}} …" heading around the app name, so the wordmark can carry
	// its own styling (branding/ohmz.css colours the "AI" amber, matching the sidebar) while the
	// sentence around it stays an ordinary translated string.
	//
	// A sentinel split rather than {@html}, so a name that is ultimately admin-settable is never
	// interpolated as markup; and rather than hardcoding the "Sign in to " prefix, so the name
	// stays wherever the locale puts it instead of being forced to the end.
	const ohmzBrandSplit = (key) => $i18n.t(key, { WEBUI_NAME: '\\u0000' }).split('\\u0000');""",
    "guestUrl binding + wordmark splitter",
)

# 2. The link itself, between the sign-in form and the OAuth divider. Mirrors the OAuth section's
#    own divider markup so it reads as one more way in, not a bolted-on afterthought.
old_anchor = (
    "\t\t\t\t\t\t\t</form>\n\n"
    "\t\t\t\t\t\t\t{#if Object.keys($config?.oauth?.providers ?? {}).length > 0}"
)
new_anchor = (
    "\t\t\t\t\t\t\t</form>\n\n"
    "\t\t\t\t\t\t\t{#if guestUrl}\n"
    "\t\t\t\t\t\t\t\t<div class=\"inline-flex items-center justify-center w-full\">\n"
    "\t\t\t\t\t\t\t\t\t<hr class=\"w-32 h-px my-4 border-0 dark:bg-gray-100/10 bg-gray-700/10\" />\n"
    "\t\t\t\t\t\t\t\t\t<span class=\"px-3 text-sm font-medium text-gray-900 dark:text-white bg-transparent\"\n"
    "\t\t\t\t\t\t\t\t\t\t>{$i18n.t('or')}</span\n"
    "\t\t\t\t\t\t\t\t\t>\n"
    "\t\t\t\t\t\t\t\t\t<hr class=\"w-32 h-px my-4 border-0 dark:bg-gray-100/10 bg-gray-700/10\" />\n"
    "\t\t\t\t\t\t\t\t</div>\n\n"
    "\t\t\t\t\t\t\t\t<a\n"
    "\t\t\t\t\t\t\t\t\tid=\"auth-guest-button\"\n"
    "\t\t\t\t\t\t\t\t\thref={guestUrl}\n"
    "\t\t\t\t\t\t\t\t\tclass=\"flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 "
    "dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition "
    "w-full rounded-full font-medium text-sm py-2.5\"\n"
    "\t\t\t\t\t\t\t\t>\n"
    "\t\t\t\t\t\t\t\t\t{$i18n.t('Continue without an account')}\n"
    "\t\t\t\t\t\t\t\t</a>\n"
    "\t\t\t\t\t\t\t\t<div class=\"mt-2 text-xs text-center text-gray-500 dark:text-gray-400\">\n"
    "\t\t\t\t\t\t\t\t\t{$i18n.t('Chat only — nothing is saved.')}\n"
    "\t\t\t\t\t\t\t\t</div>\n"
    "\t\t\t\t\t\t\t{/if}\n\n"
    "\t\t\t\t\t\t\t{#if Object.keys($config?.oauth?.providers ?? {}).length > 0}"
)
sub(old_anchor, new_anchor, "guest link markup")

# 3. The heading. Each of the four variants is one interpolated string — "Sign in to Ohmz AI" as a
#    single text node — so the app name has to be lifted into its own element before CSS can reach
#    it. {@const} is legal as an immediate child of an {#if}/{:else if}/{:else} branch, which is
#    exactly where each of these sits.
#
#    The `{p[0]}<span…>` stays on ONE line on purpose: a newline between them is collapsible
#    whitespace, and while HTML would collapse it harmlessly today, keeping them adjacent means the
#    spacing cannot depend on that.
H = "\t" * 10          # the {#if}/{:else} chain
HI = "\t" * 11         # its branches
for _key, _cond in (
    ("Get started with {{WEBUI_NAME}}", "{#if $config?.onboarding ?? false}"),
    ("Sign in to {{WEBUI_NAME}} with LDAP", "{:else if mode === 'ldap'}"),
    ("Sign in to {{WEBUI_NAME}}", "{:else if mode === 'signin'}"),
    ("Sign up to {{WEBUI_NAME}}", "{:else}"),
):
    sub(
        f"{H}{_cond}\n"
        f"{HI}{{$i18n.t(`{_key}`, {{ WEBUI_NAME: $WEBUI_NAME }})}}\n",
        f"{H}{_cond}\n"
        f"{HI}{{@const p = ohmzBrandSplit(`{_key}`)}}\n"
        f"{HI}{{p[0]}}<span class=\"ohmz-wordmark\">{{$WEBUI_NAME}}</span>{{p[1] ?? ''}}\n",
        f"heading wordmark: {_key}",
    )

# 4. The logo, corner -> centre. Upstream ALREADY ships both layouts; it just picks between them on
#    $config.metadata.auth_logo_position, and `metadata` is only emitted at all when the instance
#    has an enterprise licence (main.py guards the whole block on license_metadata). So on this
#    instance the field is permanently undefined, which upstream reads as "corner" — the centred
#    markup below has simply never been reachable.
#
#    Flipping the two conditions is therefore the whole change: no new markup, and the `id="logo"`
#    the dark-mode swap in onMount looks up (favicon-dark.png / invert filter) keeps working,
#    because exactly one of the two blocks still renders.
sub(
    "{#if $config?.metadata?.auth_logo_position === 'center'}",
    "{#if ($config?.metadata?.auth_logo_position ?? 'center') === 'center'}",
    "centre logo: default to centred when unset",
)
sub(
    "{#if !$config?.metadata?.auth_logo_position}",
    "{#if $config?.metadata?.auth_logo_position === 'corner'}",
    "corner logo: only when explicitly asked for",
)

out = os.path.join(SC, "auth+page.patched.svelte")
open(out, "w", encoding="utf-8").write(s)

rel = "src/routes/auth/+page.svelte"
d = subprocess.run(["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", SRC, out],
                   capture_output=True, text=True)
dest = sys.argv[1]
mode = "a" if len(sys.argv) > 2 and sys.argv[2] == "--append" else "w"
with open(dest, mode, encoding="utf-8") as f:
    f.write(d.stdout)
print(f"{'appended to' if mode == 'a' else 'wrote'} {dest}: "
      f"{sum(1 for l in d.stdout.splitlines() if l.startswith('+') and not l.startswith('+++'))} added")
