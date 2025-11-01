# script/bootstrap_truststore.py
"""Create a local trust store for pip-compatible tooling.

The helper concatenates one or more organization-provided certificate
authorities with the upstream ``certifi`` bundle so utilities like
``pip-audit`` keep working when outbound TLS inspection blocks the
default PyPI certificates. Optionally the script can also generate a
``pip.conf`` pointing at an internal package index.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import certifi


def _normalize_newlines(content: str) -> str:
    """Ensure a certificate blob terminates with a newline."""

    if content.endswith("\n"):
        return content
    return f"{content}\n"


def _read_certificates(cert_paths: Sequence[Path]) -> list[str]:
    """Read custom certificate authorities from disk."""

    certificates: list[str] = []
    for cert_path in cert_paths:
        certificates.append(_normalize_newlines(cert_path.read_text(encoding="utf-8")))
    return certificates


def _write_file(path: Path, content: str) -> None:
    """Persist ``content`` to ``path`` creating parents as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_bundle(extra_certificates: Sequence[Path]) -> str:
    """Return a CA bundle combining certifi data with custom authorities."""

    bundle_parts = [Path(certifi.where()).read_text(encoding="utf-8")]
    bundle_parts.extend(_read_certificates(extra_certificates))
    return "".join(bundle_parts)


def _build_pip_config(
    *,
    bundle_path: Path,
    index_url: str | None,
    extra_index_urls: Sequence[str],
    trusted_hosts: Sequence[str],
) -> str:
    """Create the contents of a pip configuration referencing the bundle."""

    lines: list[str] = ["[global]", f"cert = {bundle_path}"]
    if index_url:
        lines.append(f"index-url = {index_url}")
    for extra in extra_index_urls:
        lines.append(f"extra-index-url = {extra}")
    for host in trusted_hosts:
        lines.append(f"trusted-host = {host}")
    return "\n".join(lines) + "\n"


def _add_default_trusted_host(index_url: str | None, trusted_hosts: list[str]) -> None:
    """Add the hostname from ``index_url`` to trusted hosts when needed."""

    if not index_url or trusted_hosts:
        return
    hostname = index_url.split("//", maxsplit=1)[-1].split("/", maxsplit=1)[0]
    if hostname:
        trusted_hosts.append(hostname)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the trust store builder."""

    parser = argparse.ArgumentParser(
        description=(
            "Combine organization-issued certificate authorities with the "
            "certifi bundle so pip-compatible tooling continues to operate "
            "in restricted environments."
        )
    )
    parser.add_argument(
        "--ca-file",
        action="append",
        default=[],
        dest="ca_files",
        type=Path,
        help=(
            "Path to an additional certificate authority in PEM format. "
            "Specify the flag multiple times to append several files."
        ),
    )
    parser.add_argument(
        "--output",
        default=Path(".truststore") / "ca-bundle.pem",
        type=Path,
        help="Destination for the combined bundle (default: .truststore/ca-bundle.pem).",
    )
    parser.add_argument(
        "--pip-config",
        type=Path,
        help=(
            "Optional path to a pip configuration file referencing the "
            "generated bundle. When provided you may also supply --index-url, "
            "--extra-index-url, and --trusted-host settings."
        ),
    )
    parser.add_argument(
        "--index-url",
        help="Primary package index URL to record in the generated pip.conf.",
    )
    parser.add_argument(
        "--extra-index-url",
        action="append",
        default=[],
        dest="extra_index_urls",
        help="Additional package index URLs to append to the pip.conf.",
    )
    parser.add_argument(
        "--trusted-host",
        action="append",
        default=[],
        dest="trusted_hosts",
        help="Hostnames that should bypass certificate verification in pip.",
    )
    parser.add_argument(
        "--emit-exports",
        action="store_true",
        help=(
            "Print shell export statements for REQUESTS_CA_BUNDLE and PIP_CERT "
            "after creating the bundle."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Create the local trust store and optional pip configuration."""

    args = parse_args(argv)
    bundle_path: Path = args.output
    ca_files: list[Path] = list(args.ca_files)

    for cert_path in ca_files:
        if not cert_path.is_file():
            raise FileNotFoundError(f"Certificate file not found: {cert_path}")

    bundle_content = _build_bundle(ca_files)
    _write_file(bundle_path, bundle_content)

    print(f"Wrote combined CA bundle to {bundle_path}")

    if args.pip_config:
        trusted_hosts: list[str] = list(args.trusted_hosts)
        _add_default_trusted_host(args.index_url, trusted_hosts)
        pip_config = _build_pip_config(
            bundle_path=bundle_path,
            index_url=args.index_url,
            extra_index_urls=args.extra_index_urls,
            trusted_hosts=trusted_hosts,
        )
        _write_file(args.pip_config, pip_config)
        print(f"Wrote pip configuration to {args.pip_config}")
        if args.pip_config != Path(os.environ.get("PIP_CONFIG_FILE", "")):
            print(
                "Remember to export PIP_CONFIG_FILE to point at the generated "
                "pip.conf before invoking pip or pip-audit."
            )

    if args.emit_exports:
        print("\n# Recommended environment overrides")
        print(f"export REQUESTS_CA_BUNDLE={bundle_path}")
        print(f"export PIP_CERT={bundle_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
