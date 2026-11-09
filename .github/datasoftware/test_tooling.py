#!/usr/bin/env python3
"""Regression tests for the DataSoftware maintenance tooling.

    python3 .github/datasoftware/test_tooling.py

Two things are tested, because both have already gone wrong once:

1. `set_version.py` must work on a CRLF checkout. The Windows CI runner checks
   out with core.autocrlf=true, and an earlier version of the script anchored
   its regex with `$`, which never matched because of the stray \\r. The build
   failed at the version-pinning step.

2. `check_customisation.py` must actually fail when the customisation breaks.
   A check that cannot fail is worse than no check, because it is believed.

The repository must be clean before running: the second group mutates tracked
files and restores them with `git checkout --`.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SET_VERSION = Path(".github/datasoftware/set_version.py")
CHECKER = Path(".github/datasoftware/check_customisation.py")

failures = []


def note(msg):
    print(msg)


# ---------------------------------------------------------------------------
# 1. set_version.py on LF and CRLF
# ---------------------------------------------------------------------------
def test_set_version_line_endings():
    for style, eol in (("LF", b"\n"), ("CRLF", b"\r\n")):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / SET_VERSION.parent).mkdir(parents=True)
            shutil.copy(ROOT / SET_VERSION, tmp / SET_VERSION)
            for name in ("Cargo.toml", "Cargo.lock"):
                raw = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
                (tmp / name).write_bytes(raw.replace(b"\n", eol))

            r = subprocess.run(
                [sys.executable, str(tmp / SET_VERSION), "9.9.9-7"],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                failures.append(
                    "set_version on %s: exit %d: %s"
                    % (style, r.returncode, (r.stderr or r.stdout).strip())
                )
                continue

            toml = (tmp / "Cargo.toml").read_bytes()
            lock = (tmp / "Cargo.lock").read_bytes()
            if b'version = "9.9.9-7"' + eol not in toml:
                failures.append("set_version on %s: Cargo.toml not updated" % style)
            if b'name = "rustdesk"' + eol + b'version = "9.9.9-7"' not in lock:
                failures.append("set_version on %s: Cargo.lock not updated" % style)
            if eol == b"\n" and b"\r\n" in toml:
                failures.append("set_version on %s: line endings changed" % style)
            else:
                note("  ok   set_version preserves %s line endings" % style)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # A non-numeric build suffix must be refused: get_version_number() ignores
    # it, so such a release would never be offered as an update.
    r = subprocess.run(
        [sys.executable, str(ROOT / SET_VERSION), "1.4.9-ds1"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if r.returncode == 0:
        failures.append("set_version accepted the non-numeric suffix 1.4.9-ds1")
    else:
        note("  ok   set_version rejects a non-numeric build suffix")


# ---------------------------------------------------------------------------
# 2. check_customisation.py must detect real breakage
# ---------------------------------------------------------------------------
MUTATIONS = [
    (
        "the branding hook is dropped",
        "src/common.rs",
        "    crate::datasoftware::apply_builtin_config();\n",
        "",
        "apply_builtin_config() is called from load_custom_client()",
    ),
    (
        "the api.rustdesk.com lookup comes back",
        "src/common.rs",
        "let result = crate::datasoftware::fetch_latest_release_url()",
        "let (request, url) = hbb_common::version_check_request("
        "hbb_common::VER_TYPE_RUSTDESK_CLIENT.to_string()); let result = fetch(url)",
        "no caller of hbb_common::version_check_request",
    ),
    (
        "updates point back at the official repository",
        "src/datasoftware.rs",
        'pub const UPDATE_OWNER: &str = "DATA-SOFTWARE-s-r-o";',
        'pub const UPDATE_OWNER: &str = "rustdesk";',
        "UPDATE_OWNER is DATA-SOFTWARE-s-r-o",
    ),
    (
        "a space creeps back into the app name",
        "src/datasoftware.rs",
        'pub const APP_NAME: &str = "DataSoftware-Remote";',
        'pub const APP_NAME: &str = "DataSoftware Remote";',
        "APP_NAME contains no space",
    ),
    (
        "Cargo.lock drifts from Cargo.toml",
        "Cargo.lock",
        'name = "rustdesk"\nversion = ',
        'name = "rustdesk"\nversion = "0.0.0" # ',
        "Cargo.toml and Cargo.lock agree on the version",
    ),
    (
        "a release stops carrying the installers old clients download",
        ".github/workflows/datasoftware-windows.yml",
        "            ./SignOutput/rustdesk-${{ env.VERSION }}-${{ env.ARCH }}.exe\n",
        "            ./SignOutput/remote-${{ env.VERSION }}-${{ env.ARCH }}.exe\n",
        "releases still carry the installers clients up to 1.5.0-3 download",
    ),
    (
        "Runner.rc branding is lost in a merge",
        "flutter/windows/runner/Runner.rc",
        'VALUE "ProductName", "DataSoftware Remote"',
        'VALUE "ProductName", "RustDesk"',
        "Runner.rc ProductName is branded",
    ),
    (
        "the first-run password hook is dropped",
        "flutter/lib/desktop/pages/desktop_home_page.dart",
        "      ensureInitialPermanentPassword();\n",
        "",
        "the home page calls ensureInitialPermanentPassword()",
    ),
    (
        "the password generator falls back to a predictable RNG",
        "flutter/lib/datasoftware.dart",
        "rng = Random.secure()",
        "rng = Random()",
        "the password uses a cryptographic RNG",
    ),
    (
        "verification-method gets pinned, removing the one-time fallback",
        "src/datasoftware.rs",
        "    // Enforced. Hides the",
        "    overwrite.insert(keys::OPTION_VERIFICATION_METHOD.to_owned(), x);\n"
        "    // Enforced. Hides the",
        "verification-method is left at the upstream default",
    ),
    (
        "the password lock becomes unconditional, blocking the generator",
        "src/datasoftware.rs",
        'if LocalConfig::get_option(INITIAL_PASSWORD_ACK) == "Y" {',
        "{",
        "the permanent-password lock is gated on the acknowledgement",
    ),
    (
        "the Rust and Dart acknowledgement keys drift apart",
        "flutter/lib/datasoftware.dart",
        "'datasoftware-initial-password-acknowledged';",
        "'datasoftware-password-ack';",
        "Rust and Dart use the same acknowledgement key",
    ),
    (
        "a refused registration stops naming the cause and the fix",
        "src/rendezvous_mediator.rs",
        "crate::datasoftware::REGISTER_PK_REFUSED_HINT",
        '"see the server log"',
        "a refused RegisterPkResponse names the result",
    ),
    (
        "an update that did not take is relaunched on every start",
        "src/updater.rs",
        "crate::datasoftware::update_recently_attempted(&artifact.version)",
        "false",
        "a launched update is not relaunched in a tight loop",
    ),
    (
        "the updater copies files while the service is still running",
        "src/platform/windows.rs",
        "{wait_stopped_cmd}",
        "",
        "update_me waits for the service to exit before copying",
    ),
    (
        "the wait for the service becomes unbounded",
        "src/datasoftware.rs",
        "WaitForStatus('Stopped','00:01:00')",
        "WaitForStatus('Stopped')",
        "the service wait is bounded and ignores localised output",
    ),
    (
        "an upstream merge brings back the rustdesk/rustdesk download check",
        "src/updater/artifact.rs",
        "owner != crate::datasoftware::UPDATE_OWNER",
        'owner != "rustdesk"',
        "the updater's download URL check accepts our repository",
    ),
    (
        "an upstream merge brings back the rustdesk/rustdesk release URL",
        "src/common.rs",
        '[UPDATE_OWNER, UPDATE_REPO, "releases", "tag", release_id] =>',
        '["rustdesk", "rustdesk", "releases", "tag", release_id] =>',
        "the update check parses our release URLs",
    ),
    (
        "an upstream merge accepts only X.Y.Z release ids again",
        "src/common.rs",
        "crate::datasoftware::upstream_base_version(display_version)",
        "Some(display_version)",
        "release ids with our -N build suffix are accepted",
    ),
    (
        "the signed metadata is read from rustdesk/rustdesk again",
        "src/updater/artifact.rs",
        "crate::datasoftware::release_download_prefix(&release_id)",
        'format!("https://github.com/rustdesk/rustdesk/releases/download/{release_id}/")',
        "the signed metadata is fetched from our release",
    ),
    (
        "an upstream merge restores RustDesk's update key",
        "src/update_metadata.rs",
        'key_id: "datasoftware-2026-ed25519"',
        'key_id: "2026-ed25519-main"',
        "the client trusts our update-signing key, and only ours",
    ),
    (
        "the signer drifts to a different key id",
        "res/update/generate_update_metadata.py",
        'KEY_ID = "datasoftware-2026-ed25519"',
        'KEY_ID = "2026-ed25519-main"',
        "the signer uses the key id the client trusts",
    ),
    (
        "the release workflow stops signing the update metadata",
        ".github/workflows/datasoftware-windows.yml",
        "secrets.DATASOFTWARE_UPDATE_ED25519_SEED",
        "secrets.RUSTDESK_UPDATE_ED25519_SEED",
        "the release workflow signs and publishes the update metadata",
    ),
    (
        "the release workflow stops running the signed-release proof",
        ".github/workflows/datasoftware-windows.yml",
        "datasoftware::tests::this_build_accepts_the_signed_release_it_ships_with",
        "datasoftware::tests::something_else",
        "the release workflow proves this build accepts its own signed release",
    ),
    (
        "the review invitation no longer holds the timer's close",
        "flutter/lib/models/server_model.dart",
        "if (_clients.isEmpty && DataSoftwareReview.instance.holdsWindow()) {",
        "if (false) {",
        "the connection manager timer does not close under the invitation",
    ),
    (
        "the review link becomes a plain http URL",
        "flutter/lib/datasoftware_review.dart",
        "kDataSoftwareGoogleReviewUrl =",
        "kDataSoftwareGoogleReviewUrl = 'http://example.com/' +",
        "the review link is empty (off) or an https URL",
    ),
    (
        "the next-release unit test is dropped",
        "src/datasoftware.rs",
        "fn this_build_can_update_to_its_next_release()",
        "fn renamed_away()",
        "a unit test proves this build accepts its next release",
    ),
    (
        "upstream routes the download through a different check",
        "src/updater.rs",
        "get_update_download_file_from_url(download_url)",
        "get_other_download_file(download_url)",
        "check_update() still takes the installer only from the signed metadata",
    ),
    (
        "the release workflow is renamed and the e2e test stops following it",
        ".github/workflows/datasoftware-windows.yml",
        "name: DataSoftware Remote - Windows",
        "name: DataSoftware Remote - Windows build",
        "the update e2e test exists and follows every release build",
    ),
]


def run_checker():
    r = subprocess.run(
        [sys.executable, str(CHECKER)], cwd=str(ROOT), capture_output=True, text=True
    )
    return r.returncode, r.stdout + r.stderr


def test_checker_detects_breakage():
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        failures.append(
            "working tree has uncommitted changes; these tests mutate tracked "
            "files and restore them with `git checkout --`:\n%s" % dirty
        )
        return

    rc, out = run_checker()
    if rc != 0:
        failures.append("baseline check_customisation.py already fails:\n%s" % out)
        return
    note("  ok   baseline customisation checks pass")

    for desc, rel, old, new, expected in MUTATIONS:
        p = ROOT / rel
        original = p.read_text(encoding="utf-8", errors="replace")
        if old not in original:
            failures.append("%s: anchor not found in %s" % (desc, rel))
            continue
        try:
            p.write_text(
                original.replace(old, new, 1), encoding="utf-8", newline=""
            )
            rc, out = run_checker()
            if rc == 0:
                failures.append("%s: NOT detected" % desc)
            elif ("FAIL " + expected) not in out:
                failures.append(
                    "%s: detected, but not by the expected check %r" % (desc, expected)
                )
            else:
                note("  ok   detected: %s" % desc)
        finally:
            subprocess.run(["git", "checkout", "--", rel], cwd=str(ROOT), check=True)

    rc, _ = run_checker()
    if rc != 0:
        failures.append("the repository was not restored cleanly after the mutations")


print("set_version.py")
test_set_version_line_endings()
print("check_customisation.py")
test_checker_detects_breakage()

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All tooling tests passed.")
