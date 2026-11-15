SEO Data Workspace for macOS
=============================

Start
-----
Double-click "SEO Dashboard.app". The dashboard starts locally and opens in
your default browser.

Stop
----
Close every dashboard tab or window when you finish. The local server stops
automatically after a short grace period. "Stop SEO Dashboard.app" remains
available as a fallback while automatic shutdown is being validated.

Updates and data
----------------
Download and extract the new release, then launch it. Your projects and history
are stored separately in:

  ~/Library/Application Support/SEO Data Workspace

Replacing or deleting the downloaded application does not delete that data.
Use Settings > Download backup before important updates or transfers.

The service listens only on 127.0.0.1 and does not require internet access.

Unsigned test builds may require Control-click > Open on first launch. Official
release builds should be signed and notarized through the configured GitHub
repository secrets.
