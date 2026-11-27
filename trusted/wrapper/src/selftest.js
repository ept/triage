// The page of the self-test (see selftest.rs). It plays a web interface that has turned
// hostile: it tries to reach a second server (the "canary") in every way a page has, under
// two names for this machine and under a name that stands for a machine elsewhere.
(() => {
  const CANARY = {
    ip: 'http://127.0.0.1:__CANARY_PORT__',
    name: 'http://localhost:__CANARY_PORT__',
    // Does not exist. If the web view asks its proxy for it, the proxy's refusal shows that
    // traffic for other machines goes through the proxy. The attempt's name is in the host
    // name, because a proxy that is asked for a tunnel sees nothing else.
    ext: null,
  };
  const target = (id, host) => host === 'ext'
    ? 'http://' + id + '.canary.invalid/c/' + id
    : CANARY[host] + '/c/' + id;
  const GAP = __GAP_MS__;
  const results = {};
  const note = (id, v) => { results[id] = (results[id] ? results[id] + '; ' : '') + v; };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const el = (tag, props) => Object.assign(document.createElement(tag), props || {});

  const attempts = {
    fetch(id, url) {
      fetch(url, { mode: 'no-cors' }).then(
        (r) => note(id, 'resolved type=' + r.type + ' status=' + r.status),
        (e) => note(id, 'rejected: ' + e));
    },
    xhr(id, url) {
      const x = new XMLHttpRequest();
      x.onload = () => note(id, 'onload status=' + x.status);
      x.onerror = () => note(id, 'onerror');
      x.open('GET', url);
      x.send();
    },
    img(id, url) {
      const i = new Image();
      i.onload = () => note(id, 'onload');
      i.onerror = () => note(id, 'onerror');
      i.src = url;
    },
    css(id, url) {
      const l = el('link', { rel: 'stylesheet', href: url });
      l.onload = () => note(id, 'onload');
      l.onerror = () => note(id, 'onerror');
      document.head.appendChild(l);
    },
    script(id, url) {
      const s = el('script', { src: url });
      s.onload = () => note(id, 'onload');
      s.onerror = () => note(id, 'onerror');
      document.head.appendChild(s);
    },
    iframe(id, url) {
      const f = el('iframe', { src: url });
      f.onload = () => note(id, 'onload');
      f.onerror = () => note(id, 'onerror');
      document.body.appendChild(f);
    },
    beacon(id, url) {
      note(id, 'sendBeacon returned ' + navigator.sendBeacon(url, 'beacon'));
    },
    ws(id, url) {
      const w = new WebSocket(url.replace(/^http:/, 'ws:'));
      w.onopen = () => note(id, 'onopen');
      w.onerror = () => note(id, 'onerror');
      w.onclose = (e) => note(id, 'onclose code=' + e.code);
    },
    es(id, url) {
      const e = new EventSource(url);
      e.onopen = () => { note(id, 'onopen'); e.close(); };
      e.onerror = () => { note(id, 'onerror'); e.close(); };
    },
    form(id, url) {
      const f = el('iframe', { name: 'target-' + id });
      f.onload = () => note(id, 'target frame onload');
      document.body.appendChild(f);
      const form = el('form', { method: 'post', action: url, target: 'target-' + id });
      form.appendChild(el('input', { name: 'k', value: 'v' }));
      document.body.appendChild(form);
      form.submit();
    },
    open(id, url) {
      const w = window.open(url);
      note(id, 'window.open returned ' + (w === null ? 'null' : typeof w));
    },
    ablank(id, url) {
      const a = el('a', { href: url, target: '_blank', textContent: id });
      document.body.appendChild(a);
      a.click();
      note(id, 'clicked');
    },
    refresh(id, url, host) {
      // The app serves a same-origin document that <meta refresh>es itself to the canary.
      const f = el('iframe', { src: '/refresh/' + host + '?to=' + encodeURIComponent(url) });
      let loads = 0;
      f.onload = () => note(id, 'frame onload #' + (++loads));
      document.body.appendChild(f);
    },
    audio(id, url) {
      const a = new Audio();
      a.onerror = () => note(id, 'onerror');
      a.onloadeddata = () => note(id, 'onloadeddata');
      a.src = url;
      a.load();
    },
  };

  const frameInfo = (w) => {
    try {
      return {
        RTCPeerConnection: typeof w.RTCPeerConnection,
        webkitRTCPeerConnection: typeof w.webkitRTCPeerConnection,
      };
    } catch (e) {
      return 'threw: ' + e;
    }
  };
  const loaded = (frame) => new Promise((r) => { frame.onload = r; setTimeout(r, 1500); });

  async function run() {
    for (const ch of Object.keys(attempts)) {
      for (const host of ['ip', 'name', 'ext']) {
        const id = ch + '-' + host;
        try {
          attempts[ch](id, target(id, host), host);
        } catch (e) {
          note(id, 'threw: ' + e);
        }
        await sleep(GAP);
      }
    }

    // Whether the interface for direct connections is gone, per kind of frame.
    const frames = { main: frameInfo(window) };
    const blank = el('iframe');
    document.body.appendChild(blank);
    frames.freshAboutBlankIframe = frameInfo(blank.contentWindow);
    const same = el('iframe', { src: '/blank' });
    document.body.appendChild(same);
    await loaded(same);
    frames.sameOriginIframe = frameInfo(same.contentWindow);
    const srcdoc = el('iframe', { srcdoc: '<p>srcdoc</p>' });
    document.body.appendChild(srcdoc);
    await loaded(srcdoc);
    frames.srcdocIframe = frameInfo(srcdoc.contentWindow);

    await sleep(1000);
    for (const ch of Object.keys(attempts)) {
      for (const host of ['ip', 'name', 'ext']) {
        if (!results[ch + '-' + host]) results[ch + '-' + host] = 'no event';
      }
    }
    // Media capture must be refused without asking.
    let capture = 'not available';
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      capture = await Promise.race([
        navigator.mediaDevices.getUserMedia({ audio: true }).then(
          () => 'GRANTED', (e) => 'refused: ' + e.name),
        sleep(1500).then(() => 'no answer'),
      ]);
    }
    const report = { userAgent: navigator.userAgent, frames, capture };
    document.getElementById('status').textContent = 'reporting';
    await fetch('/report', { method: 'POST', body: JSON.stringify(report) });

    // Last: top-level navigation. If the first is stopped, the page lives on and tries the second.
    for (const host of ['ext', 'name', 'ip']) {
      location.href = target('toplevel-' + host, host);
      await sleep(800);
    }
    fetch('/finished', { method: 'POST', body: '' });
  }

  run();
})();
