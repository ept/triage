//! The web view that shows the app, and what it is not allowed to do.
//!
//! The app's web interface is served from the email sandbox, which has no network access. The
//! web view that renders it runs on the host and would have, so it is confined here, by the
//! wrapper itself and not by anything the app sends:
//!
//! * all its traffic goes through [`crate::proxy`], which reaches the app and nothing else;
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
