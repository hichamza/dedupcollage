# Security policy

## Reporting a vulnerability

If you discover a security issue in DedupCollage — for example, a crafted image file that causes arbitrary code execution, a path-traversal vulnerability that lets the tool write outside the output folder, or any way for the app to send user data off-device — please **do not** open a public GitHub issue.

Instead, email the maintainer privately. Include:

- A description of the issue and its impact
- Steps to reproduce
- Affected versions
- Any proof-of-concept you have (do not include real user photos)

You will get an acknowledgement within 7 days and a fix or mitigation plan within 30 days for confirmed issues.

## Scope

DedupCollage's threat model assumes:

- The user trusts the source files they're scanning (i.e., the files are their own photos, not adversarial inputs).
- The output disk is trusted.
- The tool runs offline. Any code path that would send data over the network counts as a security issue.

Out-of-scope:

- Crashes from genuinely corrupt files that don't enable code execution (those are bugs, not vulnerabilities — open a regular issue).
- Issues in third-party tools we shell out to (ExifTool, FFmpeg) — report those upstream.

## Privacy commitment

DedupCollage does not collect, transmit, or upload any data. This is enforced by:

- No network code in the runtime. The build pipeline pulls third-party binaries at build time, not at runtime.
- No telemetry. No update checks. No crash reporting that sends data anywhere.

If you find network activity originating from DedupCollage, that is a security issue — report it as above.
