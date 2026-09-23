# Optional browser runtime source

This directory reuses Jev Ultrafast from commit `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` under its MIT license (`vendor/jev_ultrafast/LICENSE`). Its upstream `browser-harness==0.1.13` dependency is locked with hashes in `vendor/jev_ultrafast/uv.lock`; the pinned source is commit `c24e5072ee66f8499bacd663f4f4bcb089bc4492`, MIT licensed.

CtrlIssues keeps the upstream observed-action table, CDP session/target ownership, freshness checks, current geometry/hit test, and action executor. Local source changes are narrow:

- `jev_ultrafast/browser.py` calls Browser Harness `require_existing_daemon()` and never invokes `ensure_daemon()`. Its initial document-ready wait is capped by the mission's remaining time budget.
- `jev_ultrafast/snapshot.js` suppresses password, login, MFA, token, email-account, and payment credential widgets and exposes exact HTML `id`/`name` metadata for mission field binding.
- `jev_ultrafast/model.py` permits one TypeSafe request per decision. It does not retry a failed request. The upstream field-text model helper and prompt were removed; the `Agent` accepts only an exact mission literal for fill actions.
- `bootstrap.py` disables Browser Harness `.env` loading in the three pinned transport modules. It checks the exact package version and import-site source before replacing files. Run it outside ENVSTACK; it creates an isolated venv under the private CtrlIssues data directory.

`main.py` uses the pinned chooser only after filtering observed candidates. It supplies exact mission literals directly to the existing guarded executor. A `DONE` choice is followed by fresh typed CDP readback; the chooser result itself never proves completion. The per-profile lock serializes CtrlIssues browser runs only. It does not control other Browser Harness clients or a person using the same Brave profile.

Install the optional runtime before provider use, without ENVSTACK and without starting or changing Brave:

```sh
python3 plugins/ctrlissues/hands/bootstrap.py \
  --data-dir "$CTRLISSUES_DATA" \
  --plugin-root "$CTRLISSUES_PLUGIN_ROOT"
```

Before a browser mission, provision the named Browser Harness transport separately with `BU_NAME=ctrlissues` and `BU_CDP_URL=http://127.0.0.1:9222`, using the existing Brave endpoint and a clean environment. The foreground mission refuses to create or restart that daemon.
