/* ============================================================================
   Ohmz AI — app name override for Open WebUI 0.10.2
   Installs as /app/backend/open_webui/static/loader.js, which index.html loads
   with `defer` in <head> (line 34) — before the SvelteKit entry (line 120), so
   this is in place before the app makes its first request.

   Why not WEBUI_NAME: env.py:842-844 appends " (Open WebUI)" to any WEBUI_NAME
   that isn't the default, so the env var can only ever produce
   "Ohmz AI (Open WebUI)". It also needs the container recreated rather than
   restarted, and WEBUI_SECRET_KEY is unset here — every restart would sign
   everyone out.

   GET /api/config returns {"name": ...} and is the single source the whole
   front-end reads for the app name: the sign-in heading, the sidebar wordmark
   and the document title all derive from it. Rewriting it once at the fetch
   boundary covers every one of them, including the four heading variants
   ("Sign in to X", "Get started with X", "Signing in to X", "... with LDAP")
   that a CSS text swap could not handle without breaking three of them.

   Open WebUI's licence permits removing its branding for deployments of 50
   users or fewer. This is a single-user instance. The version footer is left
   alone.
   ========================================================================= */

(() => {
	const BRAND = 'Ohmz AI';
	const GUEST_URL = 'https://aipublic.ohmz.cloud';

	// Sentinel, so "did the browser actually run this file?" is one lookup
	// rather than an afternoon. Cache layers make that question non-obvious.
	window.__ohmzLoader = BRAND;

	document.title = BRAND;

	// Read by compose/openwebui/fork/gen/04_guest_link.py's "Continue without an account" link on
	// /auth (docs/PUBLIC_INSTANCE.md). Guarded on hostname so the public instance itself — which
	// runs this same loader.js via branding/apply.sh — never gets a value and never links to
	// itself; its own sign-in page never renders the link because ENABLE_LOGIN_FORM=false skips it
	// entirely (docs/PUBLIC_INSTANCE.md), but the guard is what makes that not load-bearing.
	if (location.hostname !== 'aipublic.ohmz.cloud') {
		window.__ohmzGuestUrl = GUEST_URL;
	} else {
		// The public instance's guest arrival auto-signs-in on every page load (trusted-header
		// auth — docs/PUBLIC_INSTANCE.md) and that path fires the exact same
		// toast.success("You're now logged in.") a real user's deliberate signin does
		// (src/routes/auth/+page.svelte's setSessionUser). There's no prop or hook on OWUI's
		// <Toaster> to suppress one toast by content, and no permission flag for it either — it
		// isn't conditional on anything a config var reaches. A MutationObserver catching it by
		// text is the only angle available without patching compiled Svelte, which the public
		// instance's stock (unforked) image doesn't offer a mechanism for.
		//
		// English only — this instance ships no other locale (branding/i18n_brand.py only
		// touches en-US) — and disconnects after 10s since this toast only ever fires once, right
		// after the auto-signin that runs on every fresh page load; anything by then is a
		// different, real toast this must not swallow.
		const stopAt = Date.now() + 10000;
		const obs = new MutationObserver((mutations) => {
			if (Date.now() > stopAt) {
				obs.disconnect();
				return;
			}
			for (const m of mutations) {
				for (const node of m.addedNodes) {
					if (node.nodeType !== 1) continue;
					const toasts = node.matches?.('[data-sonner-toast]')
						? [node]
						: [...node.querySelectorAll?.('[data-sonner-toast]') ?? []];
					for (const t of toasts) {
						if (/now logged in/i.test(t.textContent || '')) {
							t.style.display = 'none';
						}
					}
				}
			}
		});
		obs.observe(document.documentElement, { childList: true, subtree: true });
	}

	const nativeFetch = window.fetch;

	window.fetch = async function (...args) {
		const response = await nativeFetch.apply(this, args);

		try {
			const input = args[0];
			const url = typeof input === 'string' ? input : (input && input.url) || '';

			if (!/\/api\/config(?:\?|$)/.test(url) || !response.ok) {
				return response;
			}

			const data = await response.clone().json();
			if (!data || typeof data.name !== 'string') {
				return response;
			}

			data.name = BRAND;

			// Fresh headers rather than response.headers: the original carries a
			// content-length for the old body, and re-sending it with a longer or
			// shorter payload is a mismatch waiting to bite.
			return new Response(JSON.stringify(data), {
				status: response.status,
				statusText: response.statusText,
				headers: { 'content-type': 'application/json' }
			});
		} catch (err) {
			// Never let branding break a request — hand back the real response.
			console.warn('[ohmz] config rewrite skipped:', err);
			return response;
		}
	};
})();
