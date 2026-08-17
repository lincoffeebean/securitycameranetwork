# Security Policy

## Security status

Security Camera Network is an early-stage, trusted-LAN-oriented project. The latest code on `main` receives security fixes on a best-effort basis; no older release branches are currently supported.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository when it is enabled. Do not open a public issue containing exploit details, private camera footage, credentials, or network information.

Before publishing the repository, a maintainer should enable private vulnerability reporting in GitHub's **Settings → Security**. If it is not enabled, open a minimal issue asking the maintainers for a private reporting channel without including sensitive details.

## Known limitations

- There is no authentication, authorization, or user management.
- Anyone who can reach the service may be able to view cameras, change settings, trigger recordings, and download clips.
- Simple LAN mode uses unencrypted HTTP.
- HTTPS protects transport but does not add user authentication.
- Recordings are ordinary files protected only by the server operating system's permissions.
- Camera and viewer pages load pose-detection libraries from a third-party CDN.
- Automatic storage-limit cleanup is not implemented.

Run the service only on a network you trust. Do not expose it directly through router port forwarding. Use an appropriately configured VPN or secure tunnel for remote access until authenticated deployment is available.
