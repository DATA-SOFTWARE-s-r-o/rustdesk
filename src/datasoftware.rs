// ===========================================================================
// DataSoftware custom client configuration
// ===========================================================================
//
// This module is the ONLY place that carries DataSoftware-specific behaviour.
// Everything else in the tree stays as close to upstream RustDesk as possible
// so that merging a new upstream release is a small, reviewable operation.
//
// Outside of this file the fork only contains these hooks:
//
//   * src/lib.rs      - `pub mod datasoftware;`
//   * src/common.rs   - `load_custom_client()` calls `apply_builtin_config()`
//   * src/common.rs   - `do_check_software_update()` asks this module for the
//                       latest release instead of `api.rustdesk.com`
//
// See DATASOFTWARE_BUILD.md for the full maintenance procedure.

use base::config::keys;
use hbb_common::{
    bail,
    config::{self, Config, LocalConfig},
    log,
    tls::{get_cached_tls_type, upsert_tls_cache, TlsType},
    ResultType,
};

use crate::hbbs_http::{create_http_client_async, get_url_for_tls};

/// Application name. Upstream derives a lot from this value: window title,
/// install directory, installed executable name, Windows service name, config
/// directory, the Add/Remove Programs entry, the strings produced by
/// `src/lang.rs`, and `is_custom_client()` (simply `get_app_name() != "RustDesk"`).
///
/// It must NOT contain a space. Upstream builds Windows shell commands with the
/// name interpolated unquoted - `sc create {app_name} ...`,
/// `sc stop/delete/start {app_name}`, `taskkill /F /IM {app_name}.exe` in
/// `src/platform/windows.rs` (17 places), and `res/msi/preprocess.py` runs the
/// executable through cmd.exe without quoting its path. A space silently splits
/// the service name and breaks installation. Upstream states the same
/// constraint in `src/lang.rs`: "app_name only contains alphanumeric and hyphen".
///
/// The nicer spaced form "DataSoftware Remote" is used where it is purely
/// cosmetic and safe: the Windows executable metadata in
/// `flutter/windows/runner/Runner.rc`.
pub const APP_NAME: &str = "DataSoftware-Remote";

/// Company website, used for user-visible "about" style information.
pub const WEBSITE: &str = "https://datasoftware.sk";

/// ID / rendezvous server (hbbs).
pub const ID_SERVER: &str = "api.datasoftware.sk";

/// API server (hbbs web API).
pub const API_SERVER: &str = "https://remote.datasoftware.sk";

/// Relay server. Empty means "automatic": the ID server tells the client which
/// relay to use. Do not hard-code a relay here unless the deployment needs one.
pub const RELAY_SERVER: &str = "";

/// Local option the Flutter side sets once the technician has confirmed they
/// stored the generated permanent password. Must stay identical to
/// `kDataSoftwareInitialPasswordAck` in flutter/lib/datasoftware.dart;
/// `.github/datasoftware/check_customisation.py` asserts that.
pub const INITIAL_PASSWORD_ACK: &str = "datasoftware-initial-password-acknowledged";

/// Public key of the self-hosted RustDesk server.
/// This is the PUBLIC half only - it is meant to be shipped inside the client.
/// The server's private key must never be added to this repository.
pub const PUBLIC_KEY: &str = "7yMWvosWrAbR2iUFsvbyL0YrMx9P839UfShu+bdwMGg=";

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

/// Appended to the log line when the ID server refuses to register this device.
///
/// Upstream collapses ID_EXISTS, TOO_FREQUENT, INVALID_ID_FORMAT, NOT_SUPPORT
/// and SERVER_ERROR into a single "unknown RegisterPkResponse", which then
/// repeats every keep-alive and says neither what was refused nor what to do.
///
/// A refusal is permanent. The client never regenerates its key pair, so it
/// retries with the same one indefinitely and cannot recover without someone
/// acting on the server. See DATASOFTWARE_BUILD.md, "Recovering a device whose
/// key changed".
pub const REGISTER_PK_REFUSED_HINT: &str = concat!(
    "The server does not say which of its checks failed. On a DataSoftware ",
    "server NOT_SUPPORT covers three cases, most likely first: the device is ",
    "waiting in Device Enrollment Requests and only needs approving; it was ",
    "deleted in the console, which blocks re-registration deliberately; or the ",
    "server holds a different public key for this ID, usually after this ",
    "machine's configuration was lost and rebuilt. The last two need the device ",
    "removed permanently - the console's delete is a soft delete and is not ",
    "enough. The client never recovers on its own: it keeps the same key pair ",
    "and retries with it indefinitely."
);

// ---------------------------------------------------------------------------
// Update source
// ---------------------------------------------------------------------------
// The stock client asks `https://api.rustdesk.com/version/latest`, which
// answers with a `rustdesk/rustdesk` release URL. This fork must never do that,
// so version discovery is replaced by a direct query against our own GitHub
// repository. There is deliberately NO fallback to `rustdesk/rustdesk`.

/// GitHub owner of the repository that serves DataSoftware Remote updates.
///
/// Until 1.5.0-2 this was `PeterLinuxOSS`, and clients built then still ask
/// that repository. Its latest release is the 1.5.0-3 bridge, which points
/// here, so `PeterLinuxOSS/rustdesk` and that release must stay. See
/// DATASOFTWARE_BUILD.md, "Moving the update source".
pub const UPDATE_OWNER: &str = "DATA-SOFTWARE-s-r-o";

/// GitHub repository name that serves DataSoftware Remote updates.
pub const UPDATE_REPO: &str = "rustdesk";

/// GitHub requires a User-Agent header on every API request.
const UPDATE_USER_AGENT: &str = "DataSoftware-Remote-Updater";

#[inline]
pub fn latest_release_api_url() -> String {
    format!("https://api.github.com/repos/{UPDATE_OWNER}/{UPDATE_REPO}/releases/latest")
}

#[inline]
pub fn release_tag_url(tag: &str) -> String {
    format!("https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}/releases/tag/{tag}")
}

/// Apply the built-in DataSoftware configuration.
///
/// This is the fork's replacement for upstream's signed `custom.txt` bundle
/// (`read_custom_client()`), which cannot be used here because it is verified
/// against a RustDesk-owned signing key. The end state is the same: the values
/// are layered into the in-memory settings maps.
///
/// Nothing is written to the user's configuration file, so unrelated user
/// preferences are never touched, on first start or on any later start.
pub fn apply_builtin_config() {
    *config::APP_NAME.write().unwrap() = APP_NAME.to_owned();

    // Enforced. These identify the DataSoftware infrastructure; a wrong value
    // makes the client unusable, so they go into OVERWRITE_SETTINGS - the same
    // map upstream fills from a custom client's "override-settings" block.
    // Being in this map also means `Config::set_option()` refuses to persist a
    // different value, so the UI cannot drift away from the deployment.
    {
        let mut overwrite = config::OVERWRITE_SETTINGS.write().unwrap();
        overwrite.insert(
            keys::OPTION_CUSTOM_RENDEZVOUS_SERVER.to_owned(),
            ID_SERVER.to_owned(),
        );
        overwrite.insert(keys::OPTION_API_SERVER.to_owned(), API_SERVER.to_owned());
        overwrite.insert(keys::OPTION_KEY.to_owned(), PUBLIC_KEY.to_owned());
        // Empty on purpose: keeps relay selection automatic and prevents a
        // stale relay from an earlier configuration from being used.
        overwrite.insert(
            keys::OPTION_RELAY_SERVER.to_owned(),
            RELAY_SERVER.to_owned(),
        );
    }

    // `verification-method` is deliberately left at upstream's default.
    //
    // The default is `use-both-passwords` (see
    // hbb_common::password_security::verification_method), under which the
    // permanent password this client generates already works. Pinning it to
    // `use-permanent-password` would not enable anything - it would only
    // switch the one-time password off, and that one-time password is the
    // safety net: upstream never generates a permanent password on its own,
    // and ours is created by the Flutter UI on first run. On a machine
    // installed silently by MSI where nobody ever opens the window, there
    // would then be no usable credential at all and the machine would be
    // unreachable.

    // Enforced. Hides the "Discovered" tab (LAN discovery) from the peer list.
    // This is a local, not a server, setting.
    {
        let mut overwrite_local = config::OVERWRITE_LOCAL_SETTINGS.write().unwrap();
        overwrite_local.insert(
            keys::OPTION_DISABLE_DISCOVERY_PANEL.to_owned(),
            "Y".to_owned(),
        );
    }

    // Not enforced, only a default: automatic updates are on out of the box,
    // but an administrator can still switch them off on a particular machine.
    {
        let mut defaults = config::DEFAULT_SETTINGS.write().unwrap();
        defaults.insert(keys::OPTION_ALLOW_AUTO_UPDATE.to_owned(), "Y".to_owned());
    }

    // Lock the permanent password against changes from the UI - but only once
    // one has actually been provisioned.
    //
    // This cannot be unconditional. `Config::set_permanent_password()` returns
    // false while `disable-change-permanent-password` is on, so switching it on
    // from the start would also block the generator in
    // flutter/lib/datasoftware.dart, and the machine would end up with no
    // permanent password and no way to set one.
    //
    // The gate is the acknowledgement the Flutter side writes after the
    // technician confirms the dialog, which is also what keeps "dismiss the
    // dialog and get a fresh password next start" working.
    //
    // Deliberately a *local* option, so it is evaluated per process:
    //   * the UI reads its own LocalConfig, so `Settings -> Security` hides the
    //     password control and `set_permanent_password_with_result()` refuses
    //     before it ever reaches the service,
    //   * the service does not have it, which leaves `rustdesk.exe --password`
    //     working as an administrative reset. Nothing else in the service
    //     changes the password on its own.
    if LocalConfig::get_option(INITIAL_PASSWORD_ACK) == "Y" {
        let mut builtin = config::BUILTIN_SETTINGS.write().unwrap();
        builtin.insert(
            keys::OPTION_DISABLE_CHANGE_PERMANENT_PASSWORD.to_owned(),
            "Y".to_owned(),
        );
    }

    log::info!(
        "DataSoftware built-in configuration applied (id: {}, api: {}, updates: {}/{})",
        ID_SERVER,
        API_SERVER,
        UPDATE_OWNER,
        UPDATE_REPO
    );
}

#[inline]
async fn request_latest_release(
    client: &reqwest::Client,
    url: &str,
) -> Result<reqwest::Response, reqwest::Error> {
    client
        .get(url)
        .header(reqwest::header::USER_AGENT, UPDATE_USER_AGENT)
        .header(reqwest::header::ACCEPT, "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .send()
        .await
}

/// Ask our GitHub repository for the latest published release and return a URL
/// in the exact shape upstream's updater expects:
///
///     https://github.com/<owner>/<repo>/releases/tag/<tag>
///
/// `src/updater.rs` turns that into the download URL by replacing `tag` with
/// `download` and appending `rustdesk-<tag>-<arch>.<exe|msi>`, so the release
/// tag must equal the version used in the asset file names.
///
/// Note: `/releases/latest` ignores drafts and pre-releases, so DataSoftware
/// releases must be published as full releases.
pub async fn fetch_latest_release_url() -> ResultType<String> {
    let url = latest_release_api_url();

    // Mirrors upstream's TLS probing so that proxied or TLS-intercepted
    // corporate networks behave the same as for the stock update check.
    let proxy_conf = Config::get_socks();
    let tls_url = get_url_for_tls(&url, &proxy_conf);
    let tls_type = get_cached_tls_type(tls_url);
    let is_tls_not_cached = tls_type.is_none();
    let tls_type = tls_type.unwrap_or(TlsType::Rustls);
    let client = create_http_client_async(tls_type, false);

    let response = match request_latest_release(&client, &url).await {
        Ok(resp) => {
            upsert_tls_cache(tls_url, tls_type, false);
            resp
        }
        Err(err) => {
            if is_tls_not_cached && err.is_request() {
                let tls_type = TlsType::NativeTls;
                let client = create_http_client_async(tls_type, false);
                let resp = request_latest_release(&client, &url).await?;
                upsert_tls_cache(tls_url, tls_type, false);
                resp
            } else {
                return Err(err.into());
            }
        }
    };

    let status = response.status();
    let bytes = response.bytes().await?;
    if !status.is_success() {
        bail!("Update check failed, {} returned {}", url, status);
    }

    let release: serde_json::Value = serde_json::from_slice(&bytes)?;
    let tag = release
        .get("tag_name")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .trim();
    if tag.is_empty() {
        bail!("Update check failed, no tag_name in the response of {}", url);
    }
    // `updater.rs` builds the download URL with a plain
    // `update_url.replace("tag", "download")`, which would corrupt the URL if
    // the tag itself contained the substring "tag".
    if tag.contains("tag") {
        bail!("Release tag {} must not contain the substring \"tag\"", tag);
    }

    Ok(release_tag_url(tag))
}

// ---------------------------------------------------------------------------
// Updater safety
// ---------------------------------------------------------------------------
// Seen on a Windows Server: an update replaced the files on disk but the
// restarted service still ran the previous librustdesk.dll, so it saw the
// release as new again and relaunched the update - about once a minute, three
// times, until a service start exceeded the 30 s SCM limit and the service
// stayed down for two days. Two independent fixes, below.

/// Batch lines that block until `service` reports Stopped, for at most a
/// minute, and then give the kernel a moment to release the handles of the
/// processes that were just killed.
///
/// Needed because `sc stop` only asks: it returns before the service has
/// exited, and the XCOPY that follows it in `update_me()` runs with `/C`, which
/// silently skips any file it cannot open instead of failing. A still-running
/// service holds librustdesk.dll - the file that carries crate::VERSION - so it
/// was the one left behind.
///
/// PowerShell rather than `sc query | find "STOPPED"` because `sc` output is
/// localised and part of the fleet runs Czech and Slovak Windows; the service
/// controller API is not. The wait is bounded, so a service that never stops
/// costs a minute and the batch then carries on exactly as it always did.
/// `ping` is the sleep because `timeout` refuses to run without a console,
/// which this batch does not have.
#[cfg(windows)]
pub fn wait_for_service_stop_cmd(service: &str) -> String {
    format!(
        "powershell -NoProfile -NonInteractive -Command \"(Get-Service -Name '{service}' -ErrorAction SilentlyContinue).WaitForStatus('Stopped','00:01:00')\"\nping -n 3 127.0.0.1 >nul"
    )
}

/// Local option recording the last update this client launched, as
/// `<version>@<unix seconds>`.
const LAST_UPDATE_ATTEMPT: &str = "datasoftware-last-update-attempt";

/// How long to wait before launching the same update again.
///
/// A launched update restarts the service, and the updater checks again 30 s
/// after every start, so an update that does not take loops with a period of
/// about a minute. An hour makes a stuck update harmless while still retrying a
/// transient failure the same day. A check the user starts by hand ignores it.
pub const UPDATE_RETRY_AFTER_SECS: u64 = 60 * 60;

fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// True if an update to `version` was launched less than
/// [`UPDATE_RETRY_AFTER_SECS`] ago - which, since we are still running the old
/// version, means it did not take.
pub fn update_recently_attempted(version: &str) -> bool {
    recently_attempted(&LocalConfig::get_option(LAST_UPDATE_ATTEMPT), version, unix_now())
}

/// Call immediately before launching an update to `version`.
pub fn record_update_attempt(version: &str) {
    LocalConfig::set_option(
        LAST_UPDATE_ATTEMPT.to_owned(),
        format!("{}@{}", version, unix_now()),
    );
}

fn recently_attempted(stored: &str, version: &str, now: u64) -> bool {
    let Some((ver, ts)) = stored.rsplit_once('@') else {
        return false;
    };
    let Ok(ts) = ts.parse::<u64>() else {
        return false;
    };
    // A timestamp in the future means the clock moved. Do not let that hold
    // updates back for however far it jumped.
    if ts > now {
        return false;
    }
    ver == version && now - ts < UPDATE_RETRY_AFTER_SECS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_launched_update_is_not_retried_within_the_hour() {
        let now = 10_000_000;
        let v = "1.4.9-5";
        assert!(!recently_attempted("", v, now), "never attempted");
        assert!(recently_attempted(&format!("{v}@{}", now - 60), v, now), "one loop period later");
        assert!(
            recently_attempted(&format!("{v}@{}", now - UPDATE_RETRY_AFTER_SECS + 1), v, now),
            "just inside the hour"
        );
        assert!(
            !recently_attempted(&format!("{v}@{}", now - UPDATE_RETRY_AFTER_SECS), v, now),
            "an hour on, retry"
        );
    }

    #[test]
    fn the_retry_guard_never_holds_back_a_different_or_unreadable_record() {
        let now = 10_000_000;
        let v = "1.4.9-5";
        assert!(!recently_attempted(&format!("1.4.9-4@{}", now - 60), v, now), "older version");
        assert!(!recently_attempted(&format!("1.4.9-6@{}", now - 60), v, now), "newer version");
        assert!(!recently_attempted("garbage", v, now));
        assert!(!recently_attempted(&format!("{v}@notanumber"), v, now));
        assert!(!recently_attempted(&format!("{v}@{}", now + 3600), v, now), "clock went back");
    }

    #[cfg(windows)]
    #[test]
    fn waiting_for_the_service_is_bounded_and_does_not_parse_localised_output() {
        let c = wait_for_service_stop_cmd("DataSoftware-Remote");
        assert!(c.contains("Get-Service -Name 'DataSoftware-Remote'"));
        assert!(c.contains("WaitForStatus('Stopped','00:01:00')"), "must be bounded");
        assert!(!c.contains("STOPPED"), "sc output is localised");
        assert!(!c.contains("timeout "), "timeout needs a console");
    }

    #[test]
    fn update_urls_point_at_the_datasoftware_fork() {
        assert_eq!(
            latest_release_api_url(),
            "https://api.github.com/repos/DATA-SOFTWARE-s-r-o/rustdesk/releases/latest"
        );
        assert_eq!(
            release_tag_url("1.4.9-1"),
            "https://github.com/DATA-SOFTWARE-s-r-o/rustdesk/releases/tag/1.4.9-1"
        );
        assert!(!latest_release_api_url().contains("rustdesk/rustdesk"));
        assert!(!release_tag_url("1.4.9-1").contains("rustdesk/rustdesk"));
    }

    // The updater builds the download URL with a plain `replace("tag", ...)`,
    // so this must keep holding for our repository path.
    #[test]
    fn tag_url_survives_the_updater_rewrite() {
        let tag_url = release_tag_url("1.4.9-1");
        assert_eq!(
            tag_url.replace("tag", "download"),
            "https://github.com/DATA-SOFTWARE-s-r-o/rustdesk/releases/download/1.4.9-1"
        );
    }

    // The updater refuses any download URL its own validator rejects. Upstream
    // 1.5.0 hard-coded rustdesk/rustdesk there (#15529), so 1.5.0-1 rejected
    // its own updates; this fails if an upstream merge brings that back.
    #[test]
    fn the_updater_accepts_our_release_assets_and_only_ours() {
        let ours = format!(
            "{}/rustdesk-1.5.0-2-x86_64.msi",
            release_tag_url("1.5.0-2").replace("tag", "download")
        );
        assert!(
            crate::updater::get_download_file_from_url(&ours).is_some(),
            "{ours}"
        );
        let official =
            "https://github.com/rustdesk/rustdesk/releases/download/1.5.0/rustdesk-1.5.0-x86_64.msi";
        assert!(crate::updater::get_download_file_from_url(official).is_none());
    }

    // 1.5.0-1 passed every test and still could not take a single update. What
    // matters is whether the build about to ship accepts the release after it,
    // so this walks the steps of `do_check_software_update()` and
    // `updater::check_update()` with this build's own VERSION.
    #[cfg(target_os = "windows")]
    #[test]
    fn this_build_can_update_to_its_next_release() {
        use hbb_common::get_version_number;
        let current = crate::VERSION;
        let (base, build) = current
            .rsplit_once('-')
            .expect("a DataSoftware version ends in -N");
        let build: u32 = build.parse().expect("-N is a number");
        let arch = crate::platform::windows::release_arch_suffix().expect("a release arch");
        for next in [format!("{base}-{}", build + 1), "99.0.0-1".to_owned()] {
            assert!(
                get_version_number(&next) > get_version_number(current),
                "{next} is not newer than {current}"
            );
            let tag_url = release_tag_url(&next);
            assert_eq!(tag_url.rsplit('/').next(), Some(next.as_str()));
            for ext in ["exe", "msi"] {
                let asset = format!("rustdesk-{next}-{arch}.{ext}");
                let url = format!("{}/{asset}", tag_url.replace("tag", "download"));
                let file = crate::updater::get_download_file_from_url(&url);
                assert_eq!(
                    file.as_deref()
                        .and_then(|p| p.file_name())
                        .and_then(|n| n.to_str()),
                    Some(asset.as_str()),
                    "the updater refuses {url}"
                );
            }
        }
    }

    // A DataSoftware release must compare as newer than the upstream base
    // version, otherwise `do_check_software_update()` never offers it.
    #[test]
    fn datasoftware_build_suffix_is_newer_than_the_base_version() {
        use hbb_common::get_version_number;
        assert!(get_version_number("1.4.9-1") > get_version_number("1.4.9"));
        assert!(get_version_number("1.4.9-2") > get_version_number("1.4.9-1"));
    }

    // A space in the app name silently breaks `sc create {app_name} ...` and
    // `taskkill /F /IM {app_name}.exe` in src/platform/windows.rs, and the
    // unquoted cmd.exe invocation in res/msi/preprocess.py.
    #[test]
    fn app_name_is_safe_for_windows_shell_commands() {
        assert!(!APP_NAME.contains(' '), "APP_NAME must not contain a space");
        assert!(
            APP_NAME.chars().all(|c| c.is_ascii_alphanumeric() || c == '-'),
            "APP_NAME must be alphanumeric or hyphen only"
        );
    }

    #[test]
    fn builtin_config_is_applied() {
        apply_builtin_config();
        assert_eq!(config::APP_NAME.read().unwrap().as_str(), APP_NAME);
        assert_eq!(
            Config::get_option(keys::OPTION_CUSTOM_RENDEZVOUS_SERVER),
            ID_SERVER
        );
        assert_eq!(Config::get_option(keys::OPTION_API_SERVER), API_SERVER);
        assert_eq!(Config::get_option(keys::OPTION_KEY), PUBLIC_KEY);
        assert_eq!(Config::get_option(keys::OPTION_RELAY_SERVER), "");
        assert!(Config::get_bool_option(keys::OPTION_ALLOW_AUTO_UPDATE));
        // A rebranded app name is what makes upstream treat this as a custom client.
        assert!(crate::is_custom_client());
    }

    // Upstream's default is `use-both-passwords`, which already accepts the
    // permanent password and keeps the one-time password as a fallback for a
    // machine whose UI has never run. Overriding it would only remove that
    // fallback, so assert we leave it alone.
    #[test]
    fn verification_method_is_left_at_the_upstream_default() {
        apply_builtin_config();
        assert_eq!(Config::get_option(keys::OPTION_VERIFICATION_METHOD), "");
        assert!(hbb_common::password_security::permanent_enabled());
    }

    // The lock and the generator are mutually exclusive: with the lock on,
    // Config::set_permanent_password() refuses, so it must stay off until a
    // password has actually been provisioned and acknowledged.
    #[test]
    fn password_lock_waits_for_the_acknowledgement() {
        LocalConfig::set_option(INITIAL_PASSWORD_ACK.to_owned(), "".to_owned());
        config::BUILTIN_SETTINGS.write().unwrap().clear();
        apply_builtin_config();
        assert!(
            !Config::is_disable_change_permanent_password(),
            "the lock must be off before the first password is generated"
        );

        LocalConfig::set_option(INITIAL_PASSWORD_ACK.to_owned(), "Y".to_owned());
        apply_builtin_config();
        assert!(
            Config::is_disable_change_permanent_password(),
            "the lock must engage once the password has been acknowledged"
        );

        // Leave no global state behind for the other tests.
        LocalConfig::set_option(INITIAL_PASSWORD_ACK.to_owned(), "".to_owned());
        config::BUILTIN_SETTINGS.write().unwrap().clear();
    }

    #[test]
    fn discovery_panel_is_hidden() {
        apply_builtin_config();
        assert_eq!(
            LocalConfig::get_option(keys::OPTION_DISABLE_DISCOVERY_PANEL),
            "Y"
        );
    }
}
