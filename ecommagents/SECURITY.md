# Security

## Reporting a vulnerability

Do not disclose credentials, customer data, or exploitable vulnerabilities in a public issue. Open a private security report through the repository owner's preferred GitHub security channel, or contact the maintainer privately before disclosure.

## Secrets

The runtime uses environment variables for credentials. Keep these out of source control.

If a credential was committed previously, assume it is compromised: revoke/rotate it at the provider, then remove it from the working tree and rewrite Git history when appropriate.