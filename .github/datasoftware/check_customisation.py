#!/usr/bin/env python3
"""Verify that the DataSoftware customisation is intact.

This is the machine-checkable form of the "what to re-check after an upstream
merge" list in DATASOFTWARE_BUILD.md. It is cheap, needs no toolchain, and runs
both in the Windows build workflow (before the ~45 minute build) and in the
upstream sync workflow (to decide whether a merge is safe to propose).

Each check states what breaks if it fails, so a future maintainer does not have
to reverse-engineer the intent.

    python3 .github/datasoftware/check_customisation.py

Exit code 0 means every invariant holds.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

APP_NAME = "DataSoftware-Remote"
UPDATE_OWNER = "DATA-SOFTWARE-s-r-o"
UPDATE_REPO = "rustdesk"

results = []


def read(rel):
    p = ROOT / rel
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def check(name, ok, why):
    results.append((bool(ok), name, why))


# --------------------------------------------------------------------------
# 1. The customisation module and its three hooks
# --------------------------------------------------------------------------
ds = read("src/datasoftware.rs")
lib = read("src/lib.rs")
common = read("src/common.rs")

check(
    "src/datasoftware.rs exists",
    ds is not None,
    "the whole customisation lives in this file",
)
check(
    "src/lib.rs declares the module",
    lib and "pub mod datasoftware;" in lib,
    "without the module declaration nothing else compiles",
)

if common:
    # The branding hook must sit inside load_custom_client(), which is the one
    # entry point every process (core_main, the Windows service, the Flutter
    # FFI entry, the installer path) goes through.
    m = re.search(
        r"pub fn load_custom_client\(\)\s*\{(.*?)\n\}", common, re.S
    )
    check(
        "apply_builtin_config() is called from load_custom_client()",
        m and "crate::datasoftware::apply_builtin_config();" in m.group(1),
        "otherwise the client starts with RustDesk defaults; the auto-updater "
        "runs in the service process, which only goes through this function",
    )

    m = re.search(
        r"pub async fn do_check_software_update\(\).*?\n\}", common, re.S
    )
    check(
        "do_check_software_update() uses our release lookup",
        m and "crate::datasoftware::fetch_latest_release_url()" in m.group(0),
        "otherwise update checks go back to api.rustdesk.com and "
        "rustdesk/rustdesk",
    )
else:
    check("src/common.rs readable", False, "file missing")

# --------------------------------------------------------------------------
# 2. Nothing may reach the upstream update service
# --------------------------------------------------------------------------
callers = []
for p in sorted((ROOT / "src").rglob("*.rs")):
    text = p.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if "version_check_request" in line and not line.strip().startswith("//"):
            callers.append("%s:%d" % (p.relative_to(ROOT).as_posix(), i))
check(
    "no caller of hbb_common::version_check_request",
    not callers,
    "that function is the only way into api.rustdesk.com/version/latest"
    + (" -- found at %s" % ", ".join(callers) if callers else ""),
)

# --------------------------------------------------------------------------
# 3. The update source is our fork
# --------------------------------------------------------------------------
if ds:
    check(
        "UPDATE_OWNER is %s" % UPDATE_OWNER,
        'UPDATE_OWNER: &str = "%s"' % UPDATE_OWNER in ds,
        "updates must come from our fork only",
    )
    check(
        "UPDATE_REPO is %s" % UPDATE_REPO,
        'UPDATE_REPO: &str = "%s"' % UPDATE_REPO in ds,
        "updates must come from our fork only",
    )
    check(
        "the GitHub releases endpoint is used",
        "api.github.com/repos/{UPDATE_OWNER}/{UPDATE_REPO}/releases/latest" in ds,
        "the updater resolves the latest version through this endpoint",
    )

# --------------------------------------------------------------------------
# 4. The contract with upstream's updater
# --------------------------------------------------------------------------
# Since 1.5.3 (upstream #15914) a client reads the release's signed
# rustdesk-update.json and installs only what it lists. Every piece of that
# chain is pinned to rustdesk/rustdesk and RustDesk's key upstream; each check
# below is one place where an upstream merge silently strands every installed
# DataSoftware client, as #15529 did to 1.5.0-1.
def code_only(text):
    """The source before its #[cfg(test)] module: the tests in these files
    name rustdesk/rustdesk on purpose, as URLs that must be refused."""
    if text is None:
        return None
    return text.split("#[cfg(test)]", 1)[0]


updater = read("src/updater.rs")
artifact = code_only(read("src/updater/artifact.rs"))
common = read("src/common.rs")
metadata_rs = read("src/update_metadata.rs")
signer = read("res/update/generate_update_metadata.py")
windows_wf = read(".github/workflows/datasoftware-windows.yml")
if updater and artifact and common and metadata_rs:
    check(
        "updater still honours allow-auto-update",
        "OPTION_ALLOW_AUTO_UPDATE" in updater,
        "that is the option apply_builtin_config() enables",
    )
    check(
        "the update check parses our release URLs",
        '[UPDATE_OWNER, UPDATE_REPO, "releases", "tag", release_id]' in common
        and '"rustdesk", "rustdesk", "releases"' not in code_only(common),
        "release_id_from_update_url() in src/common.rs must match the URL "
        "fetch_latest_release_url() returns, or no update is ever found",
    )
    check(
        "release ids with our -N build suffix are accepted",
        "crate::datasoftware::upstream_base_version(display_version)" in common,
        "upstream's display_version_from_release_id() takes only X.Y.Z and "
        "refuses every 1.5.3-N release",
    )
    check(
        "the signed metadata is fetched from our release",
        "crate::datasoftware::release_download_prefix(&release_id)" in artifact
        and "github.com/rustdesk/rustdesk" not in artifact,
        "src/updater/artifact.rs must read rustdesk-update.json from, and "
        "accept installers only under, our own release",
    )
    # Upstream 1.5.0 (#15529) only lets the updater download from
    # rustdesk/rustdesk. Merged as is, every DataSoftware client rejects its own
    # update URL and never updates again (that is what happened to 1.5.0-1).
    check(
        "the updater's download URL check accepts our repository",
        "owner != crate::datasoftware::UPDATE_OWNER" in artifact
        and "repo != crate::datasoftware::UPDATE_REPO" in artifact,
        "get_update_download_file_from_url() must compare against "
        "UPDATE_OWNER/UPDATE_REPO from src/datasoftware.rs",
    )
    check(
        "the updater does not hard-code rustdesk/rustdesk as the owner",
        re.search(r'owner\s*!=\s*"rustdesk"', updater + artifact) is None,
        "an upstream merge brought the hard-coded owner back; clients would "
        "refuse every DataSoftware update",
    )
    # The unit tests in src/datasoftware.rs repeat these steps; if upstream
    # reshapes them, those tests no longer cover what the client really does.
    check(
        "check_update() still takes the installer only from the signed metadata",
        "verified_update_artifact_from_release_page_url(&update_url, &query)" in updater
        and "get_update_download_file_from_url(download_url)" in updater,
        "re-read check_update() and update the tests in src/datasoftware.rs",
    )
    check(
        "the metadata check is still built the way our tests rebuild it",
        "release_id_from_update_url(update_url)" in artifact
        and "display_version_from_release_id(&release_id)" in artifact
        and "rustdesk-update.json" in artifact
        and "verify_update_metadata(" in artifact,
        "re-read verified_update_artifact_from_release_page_url() and update "
        "this_build_accepts_the_signed_release_it_ships_with()",
    )
    # The key: ours in the client, the same id in the signer, the same bytes in
    # the workflow (whose check-key step ties the secret to them).
    key = re.search(
        r'TrustedUpdateKey\s*\{\s*key_id:\s*"([^"]+)".*?public_key:\s*\[([^\]]+)\]',
        metadata_rs,
        re.S,
    )
    key_id = key.group(1) if key else None
    key_b64 = None
    if key:
        import base64

        key_b64 = base64.b64encode(
            bytes(int(b) for b in key.group(2).split(",") if b.strip())
        ).decode("ascii")
    check(
        "the client trusts our update-signing key, and only ours",
        key_id == "datasoftware-2026-ed25519"
        and len(re.findall(r'TrustedUpdateKey\s*\{\s*key_id:\s*"', metadata_rs)) == 1
        and key_b64 == "rLXWSxGq4aISi4f/DjlcUIaqDcSWMkGExYlXYP/WqXo=",
        "with RustDesk's key (or none of ours) no DataSoftware release ever "
        "verifies; see DATASOFTWARE_BUILD.md, 'Update signing key'",
    )
    check(
        "the signer uses the key id the client trusts",
        signer is not None and ('KEY_ID = "%s"' % key_id) in signer,
        "res/update/generate_update_metadata.py would sign under an id the "
        "client does not know",
    )
    check(
        "the release workflow signs and publishes the update metadata",
        windows_wf is not None
        and "secrets.DATASOFTWARE_UPDATE_ED25519_SEED" in windows_wf
        and ("RUSTDESK_UPDATE_ED25519_PUBLIC_KEY: %s" % key_b64) in windows_wf
        and "generate_update_metadata.py check-key" in windows_wf
        and "--artifact windows x86_64 exe" in windows_wf
        and "--artifact windows x86_64 msi" in windows_wf
        and "files: ./SignOutput/rustdesk-update.json.sig" in windows_wf
        and "files: ./SignOutput/rustdesk-update.json\n" in windows_wf.replace("\r\n", "\n"),
        "a release without rustdesk-update.json(.sig) is invisible to every "
        "client from 1.5.3-1 on",
    )
    check(
        "the release workflow proves this build accepts its own signed release",
        windows_wf is not None
        and "datasoftware::tests::this_build_accepts_the_signed_release_it_ships_with" in windows_wf
        and ds is not None
        and "fn this_build_accepts_the_signed_release_it_ships_with" in ds,
        "the signer agreeing with itself does not prove the client agrees",
    )
    # Clients up to 1.5.0-3 never read the metadata: they append
    # rustdesk-<tag>-x86_64.<exe|msi> to the tag URL with "tag" replaced by
    # "download". Every release has to keep serving them.
    publish_step = re.search(
        r"- name: Publish Release\n(.*?)\n\s*- name:",
        (windows_wf or "").replace("\r\n", "\n"),
        re.S,
    )
    publish_step = publish_step.group(1) if publish_step else ""
    check(
        "releases still carry the installers clients up to 1.5.0-3 download",
        "./SignOutput/rustdesk-${{ env.VERSION }}-${{ env.ARCH }}.exe" in publish_step
        and "./SignOutput/rustdesk-${{ env.VERSION }}-${{ env.ARCH }}.msi" in publish_step
        and "prerelease: false" in publish_step
        and ds is not None
        and 'tag.contains("tag")' in ds,
        "the ~30 machines still on 1.5.0-x would never update again",
    )
    check(
        "a unit test proves this build accepts its next release",
        ds is not None and "fn this_build_can_update_to_its_next_release" in ds,
        "1.5.0-1 shipped without one and never updated again",
    )
else:
    check(
        "the updater sources exist",
        False,
        "src/updater.rs, src/updater/artifact.rs, src/common.rs or "
        "src/update_metadata.rs is gone; the Windows auto-updater changed shape",
    )

# --------------------------------------------------------------------------
# 5. The settings maps the built-in config writes into
# --------------------------------------------------------------------------
hbb_config = read("libs/hbb_common/src/config.rs")
if hbb_config:
    for sym in ("APP_NAME", "OVERWRITE_SETTINGS", "DEFAULT_SETTINGS"):
        check(
            "hbb_common::config::%s still exists" % sym,
            "pub static ref %s" % sym in hbb_config,
            "apply_builtin_config() writes into it",
        )
    check(
        "Config::get_option still prefers OVERWRITE_SETTINGS",
        re.search(
            r"pub fn get_option\(k: &str\) -> String \{\s*get_or\(\s*&OVERWRITE_SETTINGS",
            hbb_config,
            re.S,
        )
        is not None,
        "our server settings are enforced through that precedence",
    )

if common:
    check(
        "is_custom_client() is still derived from the app name",
        re.search(
            r'pub fn is_custom_client\(\) -> bool \{\s*get_app_name\(\) != "RustDesk"',
            common,
            re.S,
        )
        is not None,
        "the rebranded app name is what puts the client into custom-client mode",
    )

# --------------------------------------------------------------------------
# 5b. The keys:: constants apply_builtin_config() uses must live where we
#     import them from.
#
#     This is a real upstream move, not a hypothetical: in 1.4.9 these
#     constants are in libs/hbb_common/src/config.rs, and on later upstream
#     versions all but OPTION_RELAY_SERVER moved to libs/base/src/config/keys.rs
#     ("Only the keys hbb_common itself references" stayed behind). Merging such
#     a release without adjusting the `use` in src/datasoftware.rs produces a
#     compile error, so catch it here in a second rather than 40 minutes into
#     the Windows build.
# --------------------------------------------------------------------------
if ds:
    used = sorted(set(re.findall(r"keys::(OPTION_[A-Z0-9_]+)", ds)))
    if re.search(r"use\s+base::config::keys", ds) or re.search(
        r"use\s+base::\{[^}]*\bconfig::\{[^}]*\bkeys\b", ds, re.S
    ):
        crate, src_root = "base", ROOT / "libs" / "base" / "src"
    else:
        crate, src_root = "hbb_common", ROOT / "libs" / "hbb_common" / "src"

    if not src_root.is_dir():
        check(
            "the crate providing keys:: exists (%s)" % crate,
            False,
            "src/datasoftware.rs imports keys from %s" % crate,
        )
    else:
        roots = [src_root]
        # `base::config::keys` is a facade: it re-exports the keys hbb_common
        # still owns (`pub use hbb_common::config::keys::*`), so a key reached
        # through it is declared in the other crate. Follow the re-export, or
        # OPTION_RELAY_SERVER and OPTION_DISABLE_CHANGE_PERMANENT_PASSWORD read
        # as missing while the code compiles perfectly well.
        for p in src_root.rglob("*.rs"):
            for m in re.finditer(
                r"pub\s+use\s+([a-z_][a-z0-9_]*)::config::keys::\*",
                p.read_text(encoding="utf-8", errors="replace"),
            ):
                reexported = ROOT / "libs" / m.group(1) / "src"
                if reexported.is_dir() and reexported not in roots:
                    roots.append(reexported)
        blob = "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for root in roots
            for p in root.rglob("*.rs")
        )
        missing = [c for c in used if ("pub const %s:" % c) not in blob]
        elsewhere = {}
        for c in missing:
            for other in (ROOT / "libs").rglob("*.rs"):
                if ("pub const %s:" % c) in other.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    elsewhere[c] = other.relative_to(ROOT).as_posix()
                    break
        hint = ""
        if elsewhere:
            hint = "; now defined in " + ", ".join(
                "%s (%s)" % (k, v) for k, v in elsewhere.items()
            )
        check(
            "all keys::OPTION_* used by apply_builtin_config() exist in %s" % crate,
            not missing,
            "src/datasoftware.rs imports keys from %s but %s is not defined "
            "there%s -- update the `use` statement in src/datasoftware.rs"
            % (crate, ", ".join(missing) if missing else "", hint),
        )

# --------------------------------------------------------------------------
# 5c. The submodule working tree must match the pinned commit, otherwise the
#     checks above inspect the wrong hbb_common and pass for the wrong reason.
# --------------------------------------------------------------------------
import subprocess  # noqa: E402

try:
    out = subprocess.run(
        ["git", "submodule", "status", "libs/hbb_common"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    # A leading '+' means the checked-out commit differs from the pinned one.
    check(
        "libs/hbb_common is checked out at the pinned commit",
        out and not out.startswith("+"),
        "run `git submodule update --init --recursive`; otherwise these checks "
        "inspect a stale hbb_common (git submodule status: %r)" % out,
    )
except Exception as e:  # pragma: no cover
    check("libs/hbb_common submodule state readable", False, str(e))

# --------------------------------------------------------------------------
# 6. The app name must stay shell-safe
# --------------------------------------------------------------------------
if ds:
    m = re.search(r'pub const APP_NAME: &str = "([^"]*)";', ds)
    name = m.group(1) if m else None
    check("APP_NAME is defined", name is not None, "it drives all the branding")
    if name:
        check(
            "APP_NAME contains no space",
            " " not in name,
            "src/platform/windows.rs interpolates it unquoted into "
            "sc/taskkill commands and res/msi/preprocess.py runs "
            "'<dist>/<app name>.exe' through cmd.exe unquoted",
        )
        check(
            "APP_NAME is alphanumeric or hyphen",
            re.fullmatch(r"[A-Za-z0-9-]+", name) is not None,
            "upstream states this constraint in src/lang.rs",
        )
        wf = read(".github/workflows/datasoftware-windows.yml") or ""
        check(
            "APP_NAME matches the build workflow",
            ("APP_NAME: %s" % name) in wf,
            "the MSI installs '<app name>.exe' and the runtime looks for "
            "exactly that file name",
        )
        # platform::windows::install_me() only XCOPYs the payload folder;
        # unlike update_me() it never calls rename_exe_cmd(). Everything else
        # resolves the installed binary through get_install_info() as
        # "<app name>.exe", so the payload must already carry that name.
        # Upstream survives shipping rustdesk.exe only because "RustDesk"
        # differs from it by case alone, which Windows ignores.
        check(
            "the payload exe is renamed before packaging",
            "Name the main exe after the app" in wf
            and 'generate.py -f ../../rustdesk/ -o . -e "../../rustdesk/${APP_NAME}.exe"'
            in wf,
            "shipping rustdesk.exe produces an install with no <app name>.exe: "
            "is_installed() returns false, shortcuts and the service point at a "
            "missing binary, and the GUI fights the service for the IPC pipe",
        )

# --------------------------------------------------------------------------
# 7. Version consistency (the auto-update loop guard)
# --------------------------------------------------------------------------
cargo_toml = read("Cargo.toml")
cargo_lock = read("Cargo.lock")
if cargo_toml and cargo_lock:
    m = re.search(r'^version = "([^"]*)"', cargo_toml, re.M)
    toml_v = m.group(1) if m else None
    m = re.search(
        r'\[\[package\]\]\r?\nname = "rustdesk"\r?\nversion = "([^"]*)"', cargo_lock
    )
    lock_v = m.group(1) if m else None
    check(
        "Cargo.toml and Cargo.lock agree on the version",
        toml_v is not None and toml_v == lock_v,
        "build.py runs `cargo build --locked`, which fails when they differ "
        "(Cargo.toml=%s, Cargo.lock=%s)" % (toml_v, lock_v),
    )
    check(
        "the version has a numeric build suffix",
        toml_v is not None and re.fullmatch(r"\d+\.\d+\.\d+(-\d+)?", toml_v),
        "get_version_number() only reads a numeric -N suffix; anything else "
        "compares equal to the base version and would never be offered "
        "(version=%s)" % toml_v,
    )

# --------------------------------------------------------------------------
# 8. User-visible Windows branding
# --------------------------------------------------------------------------
rc = read("flutter/windows/runner/Runner.rc")
if rc:
    for key, value in (
        ("ProductName", "DataSoftware Remote"),
        ("CompanyName", "DataSoftware"),
        ("FileDescription", "DataSoftware Remote"),
    ):
        check(
            "Runner.rc %s is branded" % key,
            'VALUE "%s", "%s"' % (key, value) in rc,
            "this is what Explorer shows under Properties -> Details",
        )

# --------------------------------------------------------------------------
# 9. Enforced client behaviour
# --------------------------------------------------------------------------
if ds:
    # The opposite of what it looks like: we must NOT pin verification-method.
    # Upstream's default, use-both-passwords, already accepts the permanent
    # password. Pinning it to use-permanent-password only removes the one-time
    # password, and that is the only credential a silently installed machine
    # has until someone opens the UI and our generator runs.
    check(
        "verification-method is left at the upstream default",
        re.search(r"OPTION_VERIFICATION_METHOD\.to_owned\(\)\s*,", ds) is None,
        "pinning it would strip the one-time password fallback and an MSI "
        "install where nobody opens the window would have no usable "
        "credential at all",
    )
    check(
        "the Discovered tab is hidden",
        re.search(
            r'OPTION_DISABLE_DISCOVERY_PANEL\.to_owned\(\)\s*,\s*"Y"', ds, re.S
        )
        is not None,
        "LAN discovery is switched off for this deployment",
    )

# --------------------------------------------------------------------------
# 9b. A refused registration must stay diagnosable
# --------------------------------------------------------------------------
# Upstream answered five different server refusals with one "unknown
# RegisterPkResponse" line. A refusal is permanent - the client never
# regenerates its key pair - so without a hint there is nothing in the log to
# act on, and NOT_SUPPORT is exactly what a public-key mismatch looks like on
# our server. An upstream merge would silently restore the catch-all.
mediator = read("src/rendezvous_mediator.rs")
if mediator is not None:
    check(
        "a refused RegisterPkResponse names the result",
        re.search(r"Ok\(other\)\s*=>\s*\{", mediator)
        and "crate::datasoftware::REGISTER_PK_REFUSED_HINT" in mediator,
        "otherwise a rejected device only logs 'unknown RegisterPkResponse' "
        "every keep-alive, naming neither the cause nor the fix",
    )
    check(
        "upstream's uninformative catch-all is gone",
        not re.search(
            r'_\s*=>\s*\{\s*log::error!\("unknown RegisterPkResponse"\)',
            mediator,
        ),
        "it swallowed NOT_SUPPORT together with four unrelated results",
    )
else:
    check("src/rendezvous_mediator.rs readable", False, "file missing")

if ds is not None:
    check(
        "REGISTER_PK_REFUSED_HINT tells the operator what to do",
        "REGISTER_PK_REFUSED_HINT" in ds
        and "Device Enrollment Requests" in ds
        and "soft delete" in ds,
        "NOT_SUPPORT also covers a device merely awaiting approval, so the "
        "message must not send the operator straight to deleting it",
    )

# --------------------------------------------------------------------------
# 9c. An update that does not take must not loop
# --------------------------------------------------------------------------
# Seen on a Windows Server. `sc stop` returns before the service has exited, the
# XCOPY in update_me() runs with /C and silently skipped the librustdesk.dll the
# service still held, so the restarted service ran the old crate::VERSION, saw
# the release as new and relaunched the update - once a minute, three times,
# until a start timed out and the service stayed down for two days.
# Two independent fixes; an upstream merge would quietly drop either.
updater_src = read("src/updater.rs")
if updater_src is not None:
    check(
        "a launched update is not relaunched in a tight loop",
        re.search(r"if\s+!manually\s*&&\s*crate::datasoftware::update_recently_attempted\(&artifact\.version\)", updater_src)
        and "crate::datasoftware::record_update_attempt(version)" in updater_src,
        "an update that does not take would be relaunched on every service "
        "start, about once a minute",
    )
else:
    check("src/updater.rs readable", False, "file missing")

windows_src = read("src/platform/windows.rs")
if windows_src is not None:
    check(
        "update_me waits for the service to exit before copying",
        re.search(
            r"taskkill /F /IM \{app_name\}\.exe\{filter\}\s*\n\{wait_stopped_cmd\}\s*\n\{reg_cmd\}",
            windows_src,
        ),
        "XCOPY /C then silently skips librustdesk.dll while the stopping "
        "service still holds it, which is what started the loop",
    )
else:
    check("src/platform/windows.rs readable", False, "file missing")

if ds is not None:
    # Judge the function body only: the unit test that pins this string
    # contains it too, so a plain substring search would still pass after
    # the function itself lost its timeout.
    wait_fn = re.search(
        r"pub fn wait_for_service_stop_cmd\(service: &str\) -> String \{(.*?)\n\}",
        ds.replace("\r\n", "\n"),
        re.S,
    )
    wait_body = wait_fn.group(1) if wait_fn else ""
    check(
        "the service wait is bounded and ignores localised output",
        "WaitForStatus('Stopped','00:01:00')" in wait_body and "STOPPED" not in wait_body,
        "an unbounded wait can hang an update; parsing sc output breaks on "
        "Czech and Slovak Windows",
    )

# --------------------------------------------------------------------------
# 10. The Dart half: first-run permanent password
# --------------------------------------------------------------------------
dart = read("flutter/lib/datasoftware.dart")
home = read("flutter/lib/desktop/pages/desktop_home_page.dart")
check(
    "flutter/lib/datasoftware.dart exists",
    dart is not None,
    "it generates and displays the per-machine permanent password",
)
check(
    "the home page calls ensureInitialPermanentPassword()",
    home and "ensureInitialPermanentPassword()" in home,
    "without the hook no password is ever generated or shown, and the "
    "machine ends up unreachable",
)
check(
    "the home page imports the DataSoftware module",
    home and "package:flutter_hbb/datasoftware.dart" in home,
    "the hook would not compile otherwise",
)
if dart:
    # Anchor on the assignment: the same text appears in the comment above it,
    # so a plain substring search passes even after the real call is changed.
    check(
        "the password uses a cryptographic RNG",
        re.search(r"=\s*Random\.secure\(\)", dart) is not None,
        "the default Random() is predictable and would make every generated "
        "password guessable",
    )

    # mainIsInstalled() only says the machine HAS an install, not that this
    # process is it. Without the executable-name test the portable build
    # regenerates the password on every run and pushes it to the service.
    check(
        "the password generator only runs in the installed instance",
        "_isInstalledInstance()" in dart
        and "Platform.resolvedExecutable" in dart,
        "mainIsInstalled() alone is true for the portable build too, which "
        "would silently change the machine's permanent password on every run",
    )
    # This runs from a post-frame callback at startup; an escape would surface
    # as an unhandled async error far from its cause.
    check(
        "the first-run password path cannot throw into the UI",
        re.search(r"ensureInitialPermanentPassword\(\)\s*async\s*\{\s*(//[^\n]*\n\s*)*try\s*\{", dart)
        is not None,
        "the whole body must be wrapped in try/catch",
    )

    # Rust and Dart have to agree on this key by hand. If they drift, the lock
    # never engages (Dart writes a key Rust never reads) and the customer can
    # change the password, silently.
    rust_ack = re.search(r'INITIAL_PASSWORD_ACK: &str = "([^"]+)"', ds or "")
    dart_ack = re.search(
        r"kDataSoftwareInitialPasswordAck\s*=\s*'([^']+)'", dart
    )
    check(
        "Rust and Dart use the same acknowledgement key",
        rust_ack and dart_ack and rust_ack.group(1) == dart_ack.group(1),
        "the key gates both the dialog and the permanent-password lock; a "
        "mismatch disables the lock without any error (rust=%s, dart=%s)"
        % (
            rust_ack.group(1) if rust_ack else None,
            dart_ack.group(1) if dart_ack else None,
        ),
    )

if ds:
    # Must be conditional. Set unconditionally it would block
    # Config::set_permanent_password(), which is how the generator works.
    check(
        "the permanent-password lock is gated on the acknowledgement",
        re.search(
            r"if\s+LocalConfig::get_option\(INITIAL_PASSWORD_ACK\)\s*==\s*\"Y\"",
            ds,
        )
        is not None,
        "switching disable-change-permanent-password on unconditionally stops "
        "the client from ever setting a permanent password in the first place",
    )

# --------------------------------------------------------------------------
# 10b. The Dart half: Google review invitation after a session
# --------------------------------------------------------------------------
review = read("flutter/lib/datasoftware_review.dart")
cm_main = read("flutter/lib/main.dart")
cm_model = read("flutter/lib/models/server_model.dart")
cm_page = read("flutter/lib/desktop/pages/server_page.dart")
check(
    "flutter/lib/datasoftware_review.dart exists",
    review is not None,
    "it shows the Google review invitation when a session ends",
)
# Each of these is a place where the connection manager hides or closes
# itself. One missing hook and the window disappears under the invitation.
check(
    "hideCmWindow() keeps the window for the review invitation",
    cm_main and re.search(
        r"if\s*\(!isStartup\s*&&\s*DataSoftwareReview\.instance\.holdsWindow\(\)\)\s*return;",
        cm_main,
    ),
    "the window would be hidden right after the last session",
)
check(
    "the connection manager timer does not close under the invitation",
    cm_model and re.search(
        r"_clients\.isEmpty\s*&&\s*DataSoftwareReview\.instance\.holdsWindow\(\)",
        cm_model,
    ),
    "the window would close 6 seconds after the last session",
)
check(
    "closing the last tab does not close under the invitation",
    cm_page and re.search(
        r"tabs\.isEmpty\s*&&\s*DataSoftwareReview\.instance\.holdsWindow\(\)",
        cm_page,
    ),
    "the window closes at once when the last session's tab goes",
)
check(
    "the connection manager follows the sessions and shows the invitation",
    cm_page
    and "DataSoftwareReview.instance.attach(gFFI.serverModel)" in cm_page
    and "dataSoftwareReviewOr(" in cm_page,
    "without attach() no session is ever counted; without "
    "dataSoftwareReviewOr() the window stays open but empty",
)
if review:
    m = re.search(r"kDataSoftwareGoogleReviewUrl\s*=\s*'([^']*)'", review)
    check(
        "the review link is empty (off) or an https URL",
        m is not None and (m.group(1) == "" or m.group(1).startswith("https://")),
        "launchUrl() opens whatever is configured here in the customer's "
        "browser",
    )
for lang in ("en", "sk", "cs"):
    src = read("src/lang/%s.rs" % lang) or ""
    missing = [
        k
        for k in ("title", "tip", "open", "later", "never")
        if '("datasoftware_review_%s"' % k not in src
    ]
    check(
        "src/lang/%s.rs has the review invitation texts" % lang,
        not missing,
        "missing: %s; the customer would see the raw keys" % ", ".join(missing),
    )
# Customers are Slovak and Czech. A key missing from their file falls back to
# English, not to the other language, so both need the whole set.
for lang in ("sk", "cs"):
    src = read("src/lang/%s.rs" % lang) or ""
    missing = [
        k
        for k in (
            "datasoftware_initial_password_warning",
            "datasoftware_support_credentials_tip",
            "datasoftware_id_pending",
            "Access details for support",
            "Copy both",
            "I have sent these details",
        )
        if '("%s"' % k not in src
    ]
    check(
        "src/lang/%s.rs has the first-run password texts" % lang,
        not missing,
        "missing: %s; that part of the dialog would be in English"
        % ", ".join(missing),
    )

# --------------------------------------------------------------------------
# 11. Branding assets
# --------------------------------------------------------------------------
def png_size(rel):
    """Read a PNG's dimensions from its IHDR, without needing Pillow."""
    p = ROOT / rel
    if not p.exists():
        return None
    data = p.read_bytes()[:24]
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


check(
    "the logo source is committed",
    (ROOT / ".github/datasoftware/logo-source.png").exists(),
    "generate_icons.py regenerates every icon from it",
)
check(
    "flutter/assets/icon.png is present",
    png_size("flutter/assets/icon.png") is not None,
    "it is both the in-app logo and, via src/tray.rs, the Windows tray icon; "
    "upstream does not ship this file, so a merge that removes it silently "
    "reverts the tray to the RustDesk logo",
)
for rel, expected in (
    ("res/32x32.png", (32, 32)),
    ("res/64x64.png", (64, 64)),
    ("res/128x128.png", (128, 128)),
):
    check(
        "%s is %dx%d" % (rel, expected[0], expected[1]),
        png_size(rel) == expected,
        "packaging expects this exact size",
    )

# --------------------------------------------------------------------------
# 12. Upstream workflows must not fire on their own
#
#     flutter-tag.yml is the dangerous one: its tag pattern
#     ('[0-9]+.[0-9]+.[0-9]+-[0-9]+') matches the DataSoftware release tags,
#     and the build it calls publishes with `prerelease: true` to that same
#     tag. Since the updater resolves versions through /releases/latest, which
#     skips pre-releases, letting it run silently stops every deployed client
#     from updating. fdroid.yml fires on the same tags, and flutter-nightly.yml
#     runs on a schedule; both only fail here.
#
#     ci.yml, flutter-ci.yml and wf-cliprdr-ci.yml are fine: they only push-
#     trigger on `master`, which this fork does not build from.
# --------------------------------------------------------------------------
for wf, why in (
    (
        "flutter-tag.yml",
        "it would publish a pre-release over the DataSoftware release tag and "
        "break updates for every deployed client",
    ),
    ("fdroid.yml", "it fires on the release tags and only ever fails here"),
    ("flutter-nightly.yml", "it fails nightly and publishes a tag we do not use"),
):
    text = read(".github/workflows/%s" % wf)
    if text is None:
        # Upstream removed it; nothing to guard.
        continue
    m = re.search(r"^on:\n((?:[ \t].*\n|\n)*)", text, re.M)
    triggers = m.group(1) if m else ""
    automatic = [
        t
        for t in ("push:", "schedule:", "pull_request:")
        if re.search(r"^\s+%s" % re.escape(t), triggers, re.M)
    ]
    check(
        "%s does not trigger automatically" % wf,
        not automatic,
        "%s -- found %s; re-apply the DataSoftware trigger change"
        % (why, ", ".join(automatic) if automatic else ""),
    )

# --------------------------------------------------------------------------
# 13. The update end-to-end test stays wired to the release build
#
#     It starts by workflow_run on the release workflow's *name*, so renaming
#     either side silently stops it from running.
# --------------------------------------------------------------------------
release_wf = read(".github/workflows/datasoftware-windows.yml") or ""
e2e_wf = read(".github/workflows/datasoftware-update-e2e.yml")
check(
    "the update e2e test exists and follows every release build",
    e2e_wf is not None
    and 'workflows: ["DataSoftware Remote - Windows"]' in e2e_wf
    and re.search(r"^name: DataSoftware Remote - Windows$", release_wf, re.M),
    "without it a release that clients cannot install goes unnoticed, as "
    "1.5.0-1 did",
)
check(
    "the e2e test installs the previous release and waits for the updater",
    e2e_wf is not None
    and "customer-pc:" in e2e_wf
    and "published-release:" in e2e_wf
    and "msiexec.exe" in e2e_wf,
    "see DATASOFTWARE_BUILD.md, 'Update end-to-end test'",
)

# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
failed = [r for r in results if not r[0]]
for ok, name, why in results:
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("       why it matters: %s" % why)

print("\n%d checks, %d failed" % (len(results), len(failed)))
if failed:
    print("\nThe DataSoftware customisation is broken. See DATASOFTWARE_BUILD.md")
    sys.exit(1)
print("The DataSoftware customisation is intact.")
