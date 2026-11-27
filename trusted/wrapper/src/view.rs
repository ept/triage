//! The web view that shows the app, and what it is not allowed to do.
//!
//! The app's web interface is served from the email sandbox, which has no network access. The
//! web view that renders it runs on the host and would have, so it is confined here, by the
//! wrapper itself and not by anything the app sends:
//!
//! * all its traffic goes through [`crate::proxy`], which reaches the app and nothing else;
//! * on macOS, where the web view does not use a proxy for addresses on the same machine, a
//!   content rule list makes WebKit itself refuse to load anything but the app's addresses
//!   ([`block_other_addresses`]), and the app is only loaded once that list is in place;
//! * it may navigate (as a whole, or in a frame) only within the app's origin;
//! * it cannot open windows, start downloads, or use the camera, microphone or location;
//! * the interface for direct connections between browsers (WebRTC), which does not go
//!   through proxies, is removed from every frame before any page script runs;
//! * there is no channel from its pages to this program.

use std::net::SocketAddr;

use wry::{NewWindowResponse, PermissionResponse, ProxyConfig, ProxyEndpoint, WebViewBuilder};

/// What a page tried that was stopped here (as opposed to by the proxy).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stopped {
    Navigation,
    NewWindow,
    Download,
    Permission,
}

/// Whether the web view may navigate to `url`: only within the app's own origin, written
/// exactly as `http://<address>:<port>`. `about:blank` and `about:srcdoc` are documents
/// without an address of their own, which the app uses for frames that show an email.
pub fn navigation_allowed(url: &str, app: SocketAddr) -> bool {
    if url == "about:blank" || url == "about:srcdoc" {
        return true;
    }
    let Some(rest) = url.strip_prefix("http://") else {
        return false;
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    rest[..end] == app.to_string()
}

/// The rules that keep the web view from loading anything but the app, in the format of
/// WebKit's content rule lists. Everything that would go over a network is blocked; then the
/// app's own origin is exempted. Addresses that involve no network (`data:`, `about:`) are
/// left alone: the app shows images inside emails that way.
pub fn content_rules(app: SocketAddr) -> String {
    let block = |filter: &str| serde_json::json!({"trigger": {"url-filter": filter}, "action": {"type": "block"}});
    // The address ends where the path begins, so that nothing longer can pass for it.
    let own = format!("^http://{}/", app.to_string().replace('.', "\\."));
    serde_json::json!([
        block("^https?://"),
        block("^wss?://"),
        block("^ftp://"),
        {"trigger": {"url-filter": own}, "action": {"type": "ignore-previous-rules"}},
    ])
    .to_string()
}

/// Makes the web view itself refuse every address but the app's, where the proxy does not
/// cover that (macOS). `ready` is called on the window thread once the rules are in place, or
/// with the reason if they could not be put in place; the app must not be loaded before that,
/// nor if it failed. Build the web view without an address, call this, and load the app's
/// address in `ready`.
#[cfg(target_os = "macos")]
pub fn block_other_addresses(
    webview: &wry::WebView,
    app: SocketAddr,
    ready: impl Fn(Result<(), String>) + 'static,
) {
    use block2::RcBlock;
    use objc2::MainThreadMarker;
    use objc2_foundation::{NSError, NSString};
    use objc2_web_kit::{WKContentRuleList, WKContentRuleListStore};
    use wry::WebViewExtMacOS;

    let Some(main_thread) = MainThreadMarker::new() else {
        return ready(Err("not called on the window thread".into()));
    };
    // SAFETY: called on the main thread, as the marker shows.
    let Some(store) = (unsafe { WKContentRuleListStore::defaultStore(main_thread) }) else {
        return ready(Err("WebKit has no store for content rule lists".into()));
    };
    let controller = webview.manager();
    let completion = RcBlock::new(move |list: *mut WKContentRuleList, error: *mut NSError| {
        // SAFETY: WebKit passes a valid rule list, or null together with an error (or null).
        match unsafe { list.as_ref() } {
            Some(list) => {
                // SAFETY: the rule list is valid, and this runs on the main thread.
                unsafe { controller.addContentRuleList(list) };
                ready(Ok(()));
            }
            None => {
                // SAFETY: as above.
                let reason = unsafe { error.as_ref() }
                    .map(|error| error.localizedDescription().to_string())
                    .unwrap_or_else(|| "no reason given".into());
                ready(Err(format!(
                    "WebKit did not accept the content rules: {reason}"
                )));
            }
        }
    });
    let identifier = NSString::from_str(&format!("email-triage-{}", app.port()));
    let rules = NSString::from_str(&content_rules(app));
    // SAFETY: all three arguments are valid objects and not null.
    unsafe {
        store.compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler(
            Some(&identifier),
            Some(&rules),
            Some(&completion),
        );
    }
}

/// See the macOS version: elsewhere the proxy covers every address, and there is nothing to do.
#[cfg(not(target_os = "macos"))]
pub fn block_other_addresses(
    _webview: &wry::WebView,
    _app: SocketAddr,
    ready: impl Fn(Result<(), String>) + 'static,
) {
    ready(Ok(()));
}

/// Runs in every frame before any script of the page. Proxies do not apply to WebRTC, so its
/// entry points are taken away. (The properties are also made unwritable; a page can still
/// not get them back, since they are gone from the frame it runs in and from every new one.)
const REMOVE_DIRECT_CONNECTIONS: &str = r#"
(() => {
  for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel",
                      "RTCIceTransport", "RTCSessionDescription", "RTCIceCandidate"]) {
    try {
      Object.defineProperty(window, name, { value: undefined, writable: false, configurable: false });
    } catch (error) {
      try { delete window[name]; } catch (ignored) {}
    }
  }
})();
"#;

/// Applies every restriction to a web view that is going to show the app at `app`, with the
/// proxy that listens on `proxy_port` as its only way out. `stopped` is told what was stopped
/// here and the address concerned.
pub fn restrict<'a>(
    builder: WebViewBuilder<'a>,
    app: SocketAddr,
    proxy_port: u16,
    stopped: impl Fn(Stopped, &str) + Clone + Send + Sync + 'static,
) -> WebViewBuilder<'a> {
    let (on_navigation, on_window, on_download, on_permission) =
        (stopped.clone(), stopped.clone(), stopped.clone(), stopped);
    builder
        .with_proxy_config(ProxyConfig::Http(ProxyEndpoint {
            host: "127.0.0.1".into(),
            port: proxy_port.to_string(),
        }))
        .with_navigation_handler(move |url| {
            let allowed = navigation_allowed(&url, app);
            if !allowed {
                on_navigation(Stopped::Navigation, &url);
            }
            allowed
        })
        .with_new_window_req_handler(move |url, _features| {
            on_window(Stopped::NewWindow, &url);
            NewWindowResponse::Deny
        })
        .with_download_started_handler(move |url, _path| {
            on_download(Stopped::Download, &url);
            false
        })
        .with_permission_handler(move |kind| {
            on_permission(Stopped::Permission, &format!("{kind:?}"));
            PermissionResponse::Deny
        })
        // `false`: not only in the main frame, but in every frame.
        .with_initialization_script_for_main_only(REMOVE_DIRECT_CONNECTIONS, false)
        .with_devtools(false)
}

/// The settings for one of the wrapper's own pages (see [`crate::pages`]): no scripts, no
/// navigation anywhere, nothing else either. `followed` is told the address of every link that
/// the user follows, which is how the buttons of the approval page work.
pub fn own_page<'a>(html: String, followed: impl Fn(&str) + 'static) -> WebViewBuilder<'a> {
    WebViewBuilder::new()
        .with_html(html)
        .with_javascript_disabled()
        .with_navigation_handler(move |url| {
            // The page itself is loaded as a document without an address.
            if url == "about:blank" {
                return true;
            }
            followed(&url);
            false
        })
        .with_new_window_req_handler(|_url, _features| NewWindowResponse::Deny)
        .with_download_started_handler(|_url, _path| false)
        .with_permission_handler(|_kind| PermissionResponse::Deny)
        .with_devtools(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_content_rules_exempt_only_the_app() {
        let rules: serde_json::Value =
            serde_json::from_str(&content_rules("127.0.0.1:8102".parse().unwrap())).unwrap();
        let rules = rules.as_array().unwrap();
        let filters: Vec<&str> = rules
            .iter()
            .map(|rule| rule["trigger"]["url-filter"].as_str().unwrap())
            .collect();
        assert_eq!(
            filters,
            [
                "^https?://",
                "^wss?://",
                "^ftp://",
                r"^http://127\.0\.0\.1:8102/"
            ]
        );
        let actions: Vec<&str> = rules
            .iter()
            .map(|rule| rule["action"]["type"].as_str().unwrap())
            .collect();
        // The exemption comes last: it lifts the rules before it for the app's addresses.
        assert_eq!(
            actions,
            ["block", "block", "block", "ignore-previous-rules"]
        );
    }

    #[test]
    fn navigation_stays_within_the_app() {
        let app: SocketAddr = "127.0.0.1:8102".parse().unwrap();
        for url in [
            "http://127.0.0.1:8102",
            "http://127.0.0.1:8102/",
            "http://127.0.0.1:8102/_triage/session?token=abc",
            "http://127.0.0.1:8102?x=http://evil.example/",
            "http://127.0.0.1:8102#chat/3",
            "about:blank",
            "about:srcdoc",
        ] {
            assert!(navigation_allowed(url, app), "{url}");
        }
        for url in [
            "http://127.0.0.1:8103/",
            "http://127.0.0.1/",
            "http://127.0.0.1:81020/",
            "http://localhost:8102/",
            "http://127.0.0.1:8102@evil.example/",
            "http://evil.example@127.0.0.1:8102/",
            "http://127.0.0.1:8102.evil.example/",
            "http://evil.example/http://127.0.0.1:8102/",
            "https://127.0.0.1:8102/",
            "HTTP://127.0.0.1:8102/",
            "//127.0.0.1:8102/",
            "file:///etc/passwd",
            "data:text/html,<script>1</script>",
            "javascript:alert(1)",
            "blob:http://127.0.0.1:8102/1234",
            "about:config",
            "mailto:kim@example.net",
            "decision:send",
            "",
        ] {
            assert!(!navigation_allowed(url, app), "{url}");
        }
    }
}
