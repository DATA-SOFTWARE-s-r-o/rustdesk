// ===========================================================================
// DataSoftware custom client - Google review invitation after a session
// ===========================================================================
//
// When a support session ends, the connection manager (the small window the
// customer sees during a session) stays open for a while and invites the
// customer to review DataSoftware on Google, instead of closing right away.
//
// Google's review policy does not allow asking only satisfied customers, so
// everybody who qualifies gets the same neutral invitation, with no rating
// step in front of it.
//
// The invitation is shown only when all of these hold:
//   * a review link is configured below,
//   * an accepted remote-control session (not a file transfer, terminal,
//     camera or port forward) of at least [_minSession] has ended,
//   * no other session is still running, and the window is not hidden by the
//     "hide connection manager" option,
//   * this Windows user has neither opened the review nor chosen "Don't ask
//     again", and was not invited during the last [_interval].
// It closes on its own after [_shownFor], so an unattended machine does not
// keep it open.
//
// Hooks into upstream code, each marked "DataSoftware custom client":
//   main.dart                       hideCmWindow(): keep the window
//   models/server_model.dart        timer callback: no close after 6 s
//   desktop/pages/server_page.dart  state constructor: follow the sessions
//                                   onRemoveId(): no close on the last tab
//                                   ConnectionManager: card instead of "Waiting"

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/consts.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:flutter_hbb/models/server_model.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:window_manager/window_manager.dart';

/// The "Ask for reviews" link of the DataSoftware Google profile ("Data
/// software,s.r.o."). It opens the review form straight away, unlike
/// `search.google.com/local/writereview?placeid=`, which only opens the
/// profile. The code is base64url of `09 <CID, 8 bytes LE> 10 01`, CID
/// 0xa84d25c856f9942c; Google redirects it to the profile with the review
/// action (`!12e1`). Empty turns the invitation off.
const String kDataSoftwareGoogleReviewUrl =
    'https://g.page/r/CSyU-VbIJU2oEAE/review';

const Duration _minSession = Duration(minutes: 3);
const Duration _interval = Duration(days: 30);
const Duration _shownFor = Duration(minutes: 10);

// Local options, so they are kept per Windows user.
const String _kDone = 'datasoftware-review-done';
const String _kLastShown = 'datasoftware-review-last-shown';

class DataSoftwareReview {
  DataSoftwareReview._();
  static final DataSoftwareReview instance = DataSoftwareReview._();

  /// Whether the invitation is on screen.
  final ValueNotifier<bool> shown = ValueNotifier(false);

  final Map<int, _Session> _sessions = {};
  ServerModel? _model;
  Timer? _timeout;

  /// Follow the sessions of the connection manager's [model].
  void attach(ServerModel model) {
    if (_model != null) return;
    _model = model;
    model.addListener(_observe);
  }

  void _observe() {
    final clients = _model?.clients ?? const <Client>[];
    final now = DateTime.now();
    for (final client in clients) {
      // The model replaces its Client objects when it resyncs with the
      // service, so keep the latest one but the first time it was seen.
      final session = _sessions[client.id];
      if (session == null) {
        _sessions[client.id] = _Session(now, client);
      } else {
        session.client = client;
      }
    }
    // A new connection takes the window back.
    if (shown.value && clients.isNotEmpty) {
      _hide(closeWindow: false);
    }
  }

  /// Called where the connection manager would hide or close itself after the
  /// last session. Decides whether to invite, and returns true while the
  /// invitation is shown, in which case the caller must leave the window be.
  bool holdsWindow() {
    if (shown.value) return true;
    final model = _model;
    if (model == null || model.clients.isNotEmpty || model.hideCm) {
      return false;
    }
    final now = DateTime.now();
    final qualifies = _sessions.values.any((s) =>
        _isRemoteControl(s.client) && now.difference(s.since) >= _minSession);
    _sessions.clear();
    if (!qualifies || !_invitationDue()) return false;
    _show();
    return true;
  }

  static bool _isRemoteControl(Client client) =>
      client.authorized &&
      !client.isFileTransfer &&
      !client.isViewCamera &&
      !client.isTerminal &&
      client.portForward.isEmpty;

  bool _invitationDue() {
    if (kDataSoftwareGoogleReviewUrl.isEmpty) return false;
    if (bind.mainGetLocalOption(key: _kDone) == 'Y') return false;
    final last = int.tryParse(bind.mainGetLocalOption(key: _kLastShown)) ?? 0;
    final elapsed = DateTime.now().millisecondsSinceEpoch - last;
    return elapsed >= _interval.inMilliseconds;
  }

  void _show() {
    shown.value = true;
    bind.mainSetLocalOption(
        key: _kLastShown,
        value: DateTime.now().millisecondsSinceEpoch.toString());
    _timeout?.cancel();
    _timeout = Timer(_shownFor, () => _hide(closeWindow: true));
    _bringForward();
  }

  /// During the session the window sits in the top-right corner, and the
  /// technician may have minimised it. Restore it, centre it and put it in
  /// front of everything else.
  Future<void> _bringForward() async {
    try {
      await windowManager.setOpacity(1);
      // Windows does not let a background process take the foreground with a
      // plain focus(); minimising and restoring does, as showCmWindow() in
      // main.dart relies on too.
      await windowManager.minimize();
      await windowManager.restore();
      await windowManager.setSizeAlignment(
          kConnectionManagerWindowSizeClosedChat, Alignment.center);
      await windowManager.setAlwaysOnTop(true);
      await windowManager.show();
      await windowManager.focus();
    } catch (e) {
      debugPrint('DataSoftware: could not bring the invitation forward: $e');
    }
  }

  void _hide({required bool closeWindow}) {
    _timeout?.cancel();
    _timeout = null;
    if (!shown.value) return;
    shown.value = false;
    if (closeWindow && (_model?.clients.isEmpty ?? true)) {
      windowManager.close();
    } else if (!closeWindow) {
      // A new session took the window back. showCmWindow() only repositions a
      // window it finds hidden, so put it back in its corner here.
      windowManager.setSizeAlignment(
          kConnectionManagerWindowSizeClosedChat, Alignment.topRight);
    }
  }

  Future<void> _openReview() async {
    var opened = false;
    try {
      opened = await launchUrl(Uri.parse(kDataSoftwareGoogleReviewUrl),
          mode: LaunchMode.externalApplication);
    } catch (e) {
      debugPrint('DataSoftware: could not open the review page: $e');
    }
    if (opened) {
      await bind.mainSetLocalOption(key: _kDone, value: 'Y');
    }
    _hide(closeWindow: true);
  }

  Future<void> _never() async {
    await bind.mainSetLocalOption(key: _kDone, value: 'Y');
    _hide(closeWindow: true);
  }
}

class _Session {
  _Session(this.since, this.client);
  final DateTime since;
  Client client;
}

/// What the connection manager shows once no session is left: [child], or the
/// invitation while it is shown.
Widget dataSoftwareReviewOr(Widget child) {
  return ValueListenableBuilder<bool>(
    valueListenable: DataSoftwareReview.instance.shown,
    builder: (context, shown, _) => shown ? const _ReviewCard() : child,
  );
}

class _ReviewCard extends StatelessWidget {
  const _ReviewCard();

  @override
  Widget build(BuildContext context) {
    final review = DataSoftwareReview.instance;
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: List.generate(
              5,
              (_) => Icon(Icons.star_rounded,
                  size: 30, color: Colors.amber.shade600),
            ),
          ),
          const SizedBox(height: 14),
          Text(
            translate('datasoftware_review_title'),
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 10),
          Text(
            translate('datasoftware_review_tip'),
            textAlign: TextAlign.center,
            style: TextStyle(color: Theme.of(context).hintColor),
          ),
          const SizedBox(height: 20),
          ElevatedButton.icon(
            onPressed: review._openReview,
            icon: const Icon(Icons.open_in_new_rounded, size: 18),
            label: Text(translate('datasoftware_review_open')),
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: () => review._hide(closeWindow: true),
            child: Text(translate('datasoftware_review_later')),
          ),
          TextButton(
            onPressed: review._never,
            child: Text(translate('datasoftware_review_never')),
          ),
        ],
      ),
    );
  }
}
