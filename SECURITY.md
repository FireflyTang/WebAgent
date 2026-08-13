# Security Policy

## Intended use

WebAgent is a trusted, local demonstration project. Its browser UI and
session-management routes do not implement user authentication, tenant
isolation, or an authorization boundary. Run it only on a machine and network
you trust, and keep the HTTP service bound to loopback unless you have placed a
properly configured authentication proxy in front of it.

Coding agents can execute commands and read or write files inside their
configured workspace. Docker-backed sandboxes reduce accidental host access,
but they are not presented as a hardened boundary for hostile users or code.
Review the runtime and sandbox configuration before using real credentials or
sensitive repositories.

## Sensitive data and logs

Provider API keys are secrets. Never commit `.env` files or paste credentials
into prompts. Session databases, uploaded files, generated workspaces, and
detailed HTML/runtime logs may contain prompts, source code, command arguments,
tool output, file contents, endpoint details, or other sensitive information.
Treat the entire `data/` directory and exported diagnostics as confidential.

WebAgent does not copy the browser's Provider configuration or key into session
metadata, transcripts, or diagnostic SQLite. A Provider-catalog failure writes
a server warning with a sanitized endpoint, authentication mode, and a
short key-hash fingerprint. Raw task diagnostics are intentionally complete: if
an agent, shell command, or program prints a secret, that output can be stored.

Before sharing screenshots or bug reports, verify that provider settings,
tokens, private source code, local paths, and runtime logs have been redacted.

## Reporting a vulnerability

GitHub **Private Vulnerability Reporting** will be enabled for this public
repository. Once available, report suspected vulnerabilities privately through
the repository's **Security advisories** page. Include affected versions,
reproduction steps, impact, and any suggested mitigation. Until that private
channel is visible, do not open a public issue containing exploit details or
live credentials.

This project is currently pre-1.0. Security fixes are applied to the latest
development release; older versions are not guaranteed to receive patches.
