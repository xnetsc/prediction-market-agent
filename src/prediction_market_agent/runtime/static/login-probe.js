/* Require a flow-specific proof at the actual callback origin, not just an open port. */
async function probeLoginCallback(redirectUri, probe, fetcher = fetch, timeoutMs = 2500) {
    const url = new URL(redirectUri);
    if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) ||
        !url.port || !['/callback', '/auth/callback'].includes(url.pathname) ||
        url.username || url.password || url.search || url.hash) {
        throw new Error('Invalid client callback address');
    }
    const target = new URL(probe.url);
    if (target.origin !== url.origin || !target.pathname.startsWith('/.well-known/prediction-login/') ||
        target.search || target.hash || target.username || target.password || !probe.expected_proof) {
        throw new Error('Invalid callback mapping probe');
    }
    const controller = new AbortController();
    const challenge = Array.from(crypto.getRandomValues(new Uint8Array(32)), value => value.toString(16).padStart(2, '0')).join('');
    target.searchParams.set('challenge', challenge);
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetcher(target.href, {
            method: 'GET', mode: 'cors', credentials: 'omit',
            cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
            signal: controller.signal
        });
        if (!response.ok) return false;
        const body = await response.json();
        return body.proof === probe.expected_proof && body.redirect_uri === redirectUri &&
            body.challenge === challenge ? body.proof : false;
    } catch (_) {
        // A browser policy rejection cannot be distinguished from a closed port.
        return false;
    } finally {
        clearTimeout(timer);
    }
}
