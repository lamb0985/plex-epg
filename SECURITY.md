# Security

## Web UI exposure

The Plex EPG Web UI does not provide built-in user authentication.

Run it on a trusted network, or place it behind a reverse proxy or other access
control if it must be reachable from outside that network. HTTPS is strongly
recommended whenever the Setup Wizard is used across an untrusted network,
because the Plex token is submitted to the container during setup and testing.

A stored Plex token is not returned by the Web UI after it has been saved.

## Secrets

Do not commit `/config/.env`, Plex tokens, private hostnames, or generated output
files to a public repository. The included `.gitignore` excludes common local
runtime and generated files.

## Reporting a vulnerability

If you publish this project on GitHub, configure a private vulnerability
reporting method or provide a maintainer contact before inviting public security
reports.
