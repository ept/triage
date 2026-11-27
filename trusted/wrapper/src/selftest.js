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

  const workerURL = (kind, url) => '/worker.js?kind=' + kind + '&to=' + encodeURIComponent(url);
  const registerServiceWorker = (id, script) => {
    if (!navigator.serviceWorker) return note(id, 'not available');
    // A scope of its own, so that the worker never comes to control a page.
    navigator.serviceWorker.register(script, { scope: '/scope/' + id + '/' }).then(
      () => note(id, 'registered'), (e) => note(id, 'rejected: ' + e));
  };
  // What a worker says it has: the page's own check does not cover it.
  const askWorker = (start) => new Promise((resolve) => {
    try {
      start(resolve);
    } catch (e) {
      resolve('threw: ' + e);
    }
    setTimeout(() => resolve('no answer'), 2000);
  });

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
    video(id, url) {
      const v = el('video', { src: url, preload: 'auto' });
      v.onerror = () => note(id, 'onerror');
      document.body.appendChild(v);
      v.load();
    },
    poster(id, url) {
      document.body.appendChild(el('video', { poster: url, width: 10, height: 10 }));
    },
    srcset(id, url) {
      const i = el('img', { srcset: url + ' 2x' });
      i.onload = () => note(id, 'onload');
      i.onerror = () => note(id, 'onerror');
      document.body.appendChild(i);
    },
    svg(id, url) {
      const d = el('div');
      d.innerHTML = '<svg width="10" height="10"><image href="' + url + '" width="10" height="10"/></svg>';
      document.body.appendChild(d);
    },
    object(id, url) {
      document.body.appendChild(el('object', { data: url, width: 10, height: 10 }));
    },
    embed(id, url) {
      document.body.appendChild(el('embed', { src: url, width: 10, height: 10 }));
    },
    cssimport(id, url) {
      document.head.appendChild(el('style', { textContent: '@import url("' + url + '");' }));
    },
    cssbg(id, url) {
      document.body.appendChild(el('div', {
        style: 'width:10px;height:10px;background-image:url("' + url + '")',
      }));
    },
    font(id, url) {
      new FontFace('f' + id.replace(/-/g, ''), 'url("' + url + '")').load().then(
        () => note(id, 'loaded'), (e) => note(id, 'rejected: ' + e));
    },
    import(id, url) {
      import(url).then(() => note(id, 'resolved'), (e) => note(id, 'rejected: ' + e));
    },
    // Same-origin workers that try the address: off the page's thread, where no script of
    // the wrapper runs. Shared and service workers also run outside the page.
    worker(id, url) {
      const w = new Worker(workerURL('fetch', url));
      w.onmessage = (e) => note(id, 'worker: ' + e.data);
      w.onerror = () => note(id, 'worker onerror');
    },
    workerws(id, url) { new Worker(workerURL('ws', url)); },
    workerimport(id, url) { new Worker(workerURL('import', url)); },
    workerwt(id, url) { new Worker(workerURL('wt', url)); },
    shared(id, url) {
      if (typeof SharedWorker === 'undefined') return note(id, 'not available');
      new SharedWorker(workerURL('fetch', url));
    },
    sharedwt(id, url) {
      if (typeof SharedWorker === 'undefined') return note(id, 'not available');
      new SharedWorker(workerURL('wt', url));
    },
    service(id, url) { registerServiceWorker(id, workerURL('fetch', url)); },
    servicewt(id, url) { registerServiceWorker(id, workerURL('wt', url)); },
    // The hints with which a page asks for connections or loads ahead of time.
    preconnect(id, url) {
      document.head.appendChild(el('link', { rel: 'preconnect', href: url }));
    },
    prefetch(id, url) {
      document.head.appendChild(el('link', { rel: 'prefetch', href: url }));
    },
    preload(id, url) {
      document.head.appendChild(el('link', { rel: 'preload', as: 'fetch', href: url }));
    },
    aping(id, url) {
      // A link within the page that asks for a notification to be sent elsewhere when clicked.
      const a = el('a', { href: '#' + id, textContent: id });
      a.setAttribute('ping', url);
      document.body.appendChild(a);
      a.click();
    },
    webtransport(id, url) {
      // Runs over UDP, which no HTTP proxy carries.
      if (typeof WebTransport === 'undefined') return note(id, 'not available');
      const t = new WebTransport(url.replace(/^http:/, 'https:'));
      t.ready.then(() => note(id, 'ready'), (e) => note(id, 'rejected: ' + e));
    },
  };
  // Tried last, because each replaces the page if it is not stopped.
  const leaving = {
    // An ordinary link, clicked. (WebKit opens a connection to its address straight away.)
    aself(id, url) {
      const a = el('a', { href: url, textContent: id });
      document.body.appendChild(a);
      a.click();
    },
    toplevel(id, url) {
      location.href = url;
    },
  };
  // Tells the app's server which attempt comes next, so that a connection over which nothing
  // is sent can still be put down to the attempt that opened it.
  const mark = (id) => fetch('/mark/' + id).catch(() => {});

  const frameInfo = (w) => {
    try {
      return {
        RTCPeerConnection: typeof w.RTCPeerConnection,
        webkitRTCPeerConnection: typeof w.webkitRTCPeerConnection,
        WebTransport: typeof w.WebTransport,
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
        await mark(id);
        try {
          attempts[ch](id, target(id, host), host);
        } catch (e) {
          note(id, 'threw: ' + e);
        }
        await sleep(GAP);
      }
    }

    await mark('end');
    // Whether the interfaces that bypass proxies are gone, per kind of frame.
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

    frames.dedicatedWorker = await askWorker((resolve) => {
      const w = new Worker('/worker.js?kind=probe');
      w.onmessage = (e) => resolve(e.data);
      w.onerror = () => resolve('failed to start');
    });
    if (typeof SharedWorker !== 'undefined') {
      frames.sharedWorker = await askWorker((resolve) => {
        const w = new SharedWorker('/worker.js?kind=probe');
        w.port.onmessage = (e) => resolve(e.data);
        w.onerror = () => resolve('failed to start');
      });
    }
    const workers = {
      shared: typeof SharedWorker !== 'undefined',
      service: !!navigator.serviceWorker,
    };
    if (navigator.serviceWorker) {
      // Leave no registration behind (the control's web view keeps them on disk).
      await navigator.serviceWorker.getRegistrations().then(
        (all) => Promise.all(all.map((one) => one.unregister())), () => {});
    }

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
    const report = { userAgent: navigator.userAgent, frames, capture, workers };
    document.getElementById('status').textContent = 'reporting';
    await fetch('/report', { method: 'POST', body: JSON.stringify(report) });

    // Last: leaving the page. If one attempt is stopped, the page lives on and tries the next.
    for (const ch of Object.keys(leaving)) {
      for (const host of ['name', 'ip', 'ext']) {
        const id = ch + '-' + host;
        await mark(id);
        leaving[ch](id, target(id, host));
        await sleep(800);
      }
    }
    await mark('end');
    fetch('/finished', { method: 'POST', body: '' });
  }

  run();
})();
