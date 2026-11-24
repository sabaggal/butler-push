# Butler Push for Hermes

Optional push notifications and Live Activity updates for Butler. Hermes owns
pairing, the Butler app registers its devices with the relay you configure, and
that relay forwards notifications to Apple. The package does not contain a relay,
a real server address, APNs credentials, or any production pairing keys.

## Install and pair

1. Check the release checksum. Prefer a release publisher you trust. For a Git
   source, install at its full published commit SHA:

   ```sh
   hermes plugins install <release-git-url> --ref <full-commit-sha> --no-enable
   ```

   A private GitHub source requires credentials already configured on the
   **Hermes host** (`gh auth login`, GitHub token configured in Hermes, or Git's
   credential helper). You can also unpack the verified archive so
   `~/.hermes/plugins/butler-push/plugin.yaml` exists. Neither path requires
   giving Butler a GitHub or APNs key. Use Hermes 0.23.0 or later on macOS/Linux.
2. In the **root Hermes home**, set the single relay URL in `~/.hermes/.env`:

   ```dotenv
   BUTLER_PUSH_RELAY_URL=https://your-butler-relay.example
   ```

   There is no default. The root file setting takes precedence over a
   process-env fallback and is shared across bot profiles. Use HTTPS and do not
   add userinfo, a query, or a fragment. HTTP is accepted only on loopback for
   local development. Changing or removing the relay may require clearing
   the Butler app's old notification pairing before pairing again.
3. Check the Hermes dashboard is protected by its normal access controls. If
   you used Hermex Push before, keep its plugin installed for unrelated
   companion routes you still need. Butler refuses pairing and sending while
   the old relay is set in the root file or its process environment. Remove
   or clear **every** old
   `HERMEX_PUSH_RELAY_URL` setting used by the host, its plugin processes and
   active profiles before enabling Butler Push. Confirm the old authenticated
   `/api/plugins/hermex-push/pairing` returns **409** (not configured). That
   stops the old push sender without removing its quota, Git or other routes.
4. Enable `butler-push` in the root Hermes profile:

   ```sh
   hermes plugins enable butler-push
   ```

   Reload the Hermes gateway and dashboard using your normal supervisor, if
   their running processes haven't loaded the new plugin/configuration.
   Keep only one configured push sender. Check that the new authenticated
   `/api/plugins/butler-push/capabilities` lists `GET /pairing` and the old
   pairing is still unconfigured. Reload both plugins or their hosts if the
   old sender might still have queued events or cached settings after cutover.
   Do not print or share the contents of either
   pairing response.
5. Open **Butler → Settings → Notifications** with the intended Hermes
   connection selected and enable push. Check one completed turn in Butler.
   End-to-end APNs delivery requires a relay configured for this Butler app's
   signing team, app topic and APNs environment. A successful plugin install
   or a pairing HTTP response does not establish delivery.

The separate platform/resident hooks are installed once in the root home.
Where Hermes supports the source-compatible profile dispatch seam, all host
profiles use that install. If that seam isn't available, only profiles with
an enabled copy are observed. The gateway does not accept cron or
`send_message` delivery to this notification-only adapter.

## Privacy and compatibility

The protected pairing endpoint is mounted inside Hermes' own authenticated
dashboard. It returns only this install's relay and pairing information, with
`Cache-Control: no-store`. The install key is a bearer capability; keep it in
the app's Keychain and never paste or log it. Fresh keys are generated at first
configured pairing or eligible event, stored mode 0600 under
`~/.hermes/plugin-data/butler-push/`. The directory is mode 0700 and survives
reinstall. Legacy Hermex state isn't imported.

The relay receives the minimum existing wire contract for grouping and routing:
coarse client source, opaque event/thread/collapse IDs, the Hermes session ID,
event timestamps and status/counter metadata. It does not receive text from
users, commands, tool arguments/results, or approval/question content. An
AES‑256‑GCM envelope protects bot identity and request-routing fields. Assistant
reply text is off by default. If you specifically want assistant reply previews,
set `BUTLER_PUSH_REPLY_PREVIEWS=1` in the root Hermes environment; only a short
assistant reply excerpt is encrypted on the host and can be opened by the
paired device. Your relay operator can see the unencrypted routing metadata
and device registrations, but cannot decrypt this preview. The receiver may
independently disable previews. No data goes to the upstream Hermex relay.

Events originate only from recognized Butler/iOS, Hermes Desktop/TUI, web UI
and Butler group-room sessions. Cron, Kanban, unknown origins and chat
integrations with their own notifications are excluded. The bounded sender
does not persist queued events and does not follow redirect responses.

The MIT-licensed implementation is adapted from [Hermex Push][upstream].
The original copyright and license are preserved in [LICENSE](LICENSE).
The compatible event and encryption wire version is still 1. Butler's
host plugin, app and separately operated relay are independent of Hermex.

[upstream]: https://github.com/uzairansaruzi/hermex-push

## Verify source

From the plugin root with the optional test dependencies installed:

```sh
python -m pytest -q
hermes plugins doctor . --ci
```

The suite uses a synthetic relay and keys: it does not contact APNs or any
real host. See [NOTICE](NOTICE) for the precise upstream baseline.
