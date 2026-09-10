const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
vm.runInThisContext(fs.readFileSync(path.join(__dirname, '../src/prediction_market_agent/runtime/static/login-probe.js'), 'utf8'));
const redirect = 'http://localhost:1455/auth/callback';
const probe = {url: 'http://localhost:1455/.well-known/prediction-login/flow', expected_proof: 'this-container-this-flow'};

test('matched container mapping is accepted without sending an OAuth callback', async () => {
    const value = await probeLoginCallback(redirect, probe, async (url, options) => {
        const target = new URL(url);
        assert.equal(target.origin + target.pathname, probe.url);
        const challenge = target.searchParams.get('challenge');
        assert.match(challenge, /^[0-9a-f]{64}$/);
        assert.equal(options.mode, 'cors');
        assert.equal(options.credentials, 'omit');
        assert.equal(options.redirect, 'error');
        return {ok: true, json: async () => ({proof: probe.expected_proof, redirect_uri: redirect, challenge})};
    });
    assert.equal(value, probe.expected_proof);
});
test('every attempt has a fresh challenge and cannot reuse a previous response', async () => {
    let captured;
    const good = await probeLoginCallback(redirect, probe, async (url) => {
        captured = {proof: probe.expected_proof, redirect_uri: redirect, challenge: new URL(url).searchParams.get('challenge')};
        return {ok: true, json: async () => captured};
    });
    assert.equal(good, probe.expected_proof);
    const stale = await probeLoginCallback(redirect, probe, async (url) => {
        assert.notEqual(new URL(url).searchParams.get('challenge'), captured.challenge);
        return {ok: true, json: async () => captured};
    });
    assert.equal(stale, false);
});
test('another instance cannot pass even with the same port and fresh challenge', async () => {
    const value = await probeLoginCallback(redirect, probe, async (url) => ({ok: true, json: async () => ({
        proof: 'another-instance-current-flow', redirect_uri: redirect,
        challenge: new URL(url).searchParams.get('challenge')
    })}));
    assert.equal(value, false);
});
test('open unrelated listener, stale proof, different redirect and invalid JSON fail', async () => {
    for (const response of [
        {ok: false}, {ok: true, json: async () => ({})},
        {ok: true, json: async () => ({proof: 'old-flow', redirect_uri: redirect})},
        {ok: true, json: async () => ({proof: probe.expected_proof, redirect_uri: 'http://localhost:8888/callback'})},
        {ok: true, json: async () => {throw Error('HTML from another client');}}
    ]) assert.equal(await probeLoginCallback(redirect, probe, async () => response), false);
});
test('browser rejection and timeout are not success', async () => {
    assert.equal(await probeLoginCallback(redirect, probe, async () => {throw Error('CORS or refused');}), false);
    assert.equal(await probeLoginCallback(redirect, probe, (_, options) => new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => reject(Error('timeout')));
    }), 5), false);
});
test('only a probe on the actual captured callback origin may be requested', async () => {
    const never = async () => {throw Error('Must not fetch');};
    await assert.rejects(probeLoginCallback(redirect, {...probe, url: 'https://other.example/proof'}, never));
    await assert.rejects(probeLoginCallback('http://localhost:1455/admin', probe, never));
    await assert.rejects(probeLoginCallback(redirect, {...probe, url: 'http://localhost:8765/.well-known/prediction-login/x'}, never));
});
