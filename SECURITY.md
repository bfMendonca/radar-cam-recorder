# Security policy

If you find a security issue, please report it privately through
[GitHub security advisories](https://github.com/bfMendonca/radar-cam-recorder/security/advisories/new)
rather than opening a public issue.

The recorder runs as an unprivileged service user. It reads its configuration from
`/etc/radar-cam-recorder.toml`, which should be writable by root only: the
`camera.binary` and `camera.extra_args` keys choose the program the service runs.
