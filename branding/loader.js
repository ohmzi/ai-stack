/* ============================================================================
   OhmzAI — app name override for Open WebUI 0.10.2
   Installs as /app/backend/open_webui/static/loader.js, which index.html loads
   with `defer` in <head> (line 34) — before the SvelteKit entry (line 120), so
   this is in place before the app makes its first request.

   Why not WEBUI_NAME: env.py:842-844 appends " (Open WebUI)" to any WEBUI_NAME
   that isn't the default, so the env var can only ever produce
   "OhmzAI (Open WebUI)". It also needs the container recreated rather than
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
	const BRAND = 'OhmzAI';

	document.title = BRAND;

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
