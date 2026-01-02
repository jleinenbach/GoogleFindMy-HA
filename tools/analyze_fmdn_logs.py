#!/usr/bin/env python3
"""Analyze Android Logcat for FMDN endpoint discovery.

This script helps identify Google FMDN upload endpoints by analyzing
logcat output from Android devices (via ADB + Shizuku).

Usage:
    # Live analysis (requires ADB connection)
    python analyze_fmdn_logs.py --live

    # Analyze saved logcat file
    adb logcat > fmdn_logs.txt
    python analyze_fmdn_logs.py fmdn_logs.txt

    # Filter by specific patterns
    python analyze_fmdn_logs.py --pattern "spot-pa" fmdn_logs.txt
"""

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass

# Display constants
LOG_LINE_MAX_LENGTH = 100  # Maximum log line length before truncation


@dataclass
class EndpointCandidate:
    """Potential FMDN endpoint discovered in logs."""

    url: str
    method: str = "POST"
    log_line: str = ""
    confidence: str = "low"  # low, medium, high
    source: str = ""  # logcat tag


# Regex patterns for endpoint detection
PATTERNS = {
    "url_full": re.compile(
        r'https?://(?:spot-pa|findmydevice|nearbyfinder-pa)\.googleapis\.com(/[^\s"\']+)',
        re.IGNORECASE,
    ),
    "grpc_service": re.compile(
        r'(google\.internal\.spot\.v\d+\.SpotService/[A-Za-z0-9_]+)',
        re.IGNORECASE,
    ),
    "url_path": re.compile(
        r'(?:POST|GET|PUT)\s+(/v\d+/[^\s"\']+)',
        re.IGNORECASE,
    ),
    "endpoint_name": re.compile(
        r'(?:endpoint|url|path|uri|method)[":\s]+["\']?([A-Za-z0-9_/]+(?:Upload|Report|Location)[A-Za-z0-9_/]*)["\']?',
        re.IGNORECASE,
    ),
    "protobuf_class": re.compile(
        r'(LocationReportsUpload|UploadLocationReports|LocationReport)',
    ),
    "api_call": re.compile(
        r'(?:upload|send|post).*(?:location|report|beacon)',
        re.IGNORECASE,
    ),
}

# High-confidence keywords
HIGH_CONFIDENCE_KEYWORDS = [
    "LocationReportsUpload",
    "UploadLocationReports",
    "uploadLocationReports",
    "/v1/upload",
    "spot-pa.googleapis.com",
    "google.internal.spot",  # gRPC service pattern
    "SpotService/Upload",    # gRPC method pattern
]

# Relevant logcat tags
RELEVANT_TAGS = [
    "GmsClient",
    "Spot",
    "FMDN",
    "FindMy",
    "NetworkRequest",
    "Chimera",
    "Volley",
    "OkHttp",
]


def parse_logcat_line(line: str) -> tuple[str, str, str] | None:
    """Parse logcat line into (tag, priority, message).

    Example line formats:
    - V/Tag: message
    - 01-02 12:34:56.789  1234  5678 I Tag: message
    """
    # Format: timestamp pid tid priority tag: message
    match = re.match(
        r'(?:\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+)?'
        r'([VDIWEF])/([^:]+):\s*(.+)',
        line,
    )

    if match:
        priority, tag, message = match.groups()
        return tag.strip(), priority, message.strip()

    return None


def analyze_line(line: str, line_num: int) -> list[EndpointCandidate]:
    """Analyze a single logcat line for FMDN endpoints."""
    candidates: list[EndpointCandidate] = []

    parsed = parse_logcat_line(line)
    if not parsed:
        return candidates

    tag, priority, message = parsed

    # Filter by relevant tags (if any)
    if RELEVANT_TAGS and tag not in RELEVANT_TAGS:
        # But still check for high-value keywords
        if not any(kw in line for kw in HIGH_CONFIDENCE_KEYWORDS):
            return candidates

    # Pattern matching
    for pattern_name, regex in PATTERNS.items():
        for match in regex.finditer(message):
            url = match.group(1) if match.groups() else match.group(0)

            # Determine confidence
            confidence = "low"
            if any(kw in line for kw in HIGH_CONFIDENCE_KEYWORDS):
                confidence = "high"
            elif "upload" in line.lower() and "location" in line.lower():
                confidence = "medium"

            candidates.append(
                EndpointCandidate(
                    url=url,
                    method="POST",  # FMDN uploads are always POST
                    log_line=line.strip(),
                    confidence=confidence,
                    source=f"{tag} (line {line_num})",
                )
            )

    return candidates


def analyze_logcat(file_path: str | None = None) -> list[EndpointCandidate]:
    """Analyze logcat output for FMDN endpoints.

    Args:
        file_path: Path to logcat file, or None for live capture

    Returns:
        List of endpoint candidates
    """
    candidates = []

    if file_path:
        # Analyze saved logcat file
        print(f"[*] Analyzing logcat file: {file_path}")
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            for line_num, line in enumerate(f, start=1):
                candidates.extend(analyze_line(line, line_num))
    else:
        # Live logcat analysis (requires ADB)
        print("[*] Starting live logcat analysis (Ctrl+C to stop)...")
        print("[*] Trigger FMDN upload on your Android device now!")
        print()

        try:
            # Run logcat with FMDN-related filters
            cmd = [
                "adb",
                "shell",
                "logcat",
                "-v",
                "time",
                "*:W",  # Warning and above by default
            ]

            # Add tag filters
            for tag in RELEVANT_TAGS:
                cmd.append(f"{tag}:D")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            line_num = 0
            if process.stdout is not None:
                for line in process.stdout:
                    line_num += 1
                    line_candidates = analyze_line(line, line_num)

                    # Print high-confidence findings immediately
                    for candidate in line_candidates:
                        if candidate.confidence == "high":
                            print("\n🎯 HIGH CONFIDENCE MATCH:")
                            print(f"   URL: {candidate.url}")
                            print(f"   Source: {candidate.source}")
                            print(f"   Log: {candidate.log_line[:120]}...")
                            print()

                    candidates.extend(line_candidates)

        except KeyboardInterrupt:
            print("\n[*] Stopped live analysis.")
        except FileNotFoundError:
            print("❌ Error: 'adb' not found. Please install Android Platform Tools.")
            sys.exit(1)

    return candidates


def print_summary(candidates: list[EndpointCandidate]) -> None:  # noqa: PLR0915
    """Print analysis summary with ranked candidates."""
    if not candidates:
        print("❌ No FMDN endpoints found in logs.")
        print()
        print("Troubleshooting:")
        print("1. Ensure FMDN is enabled on your device")
        print("2. Walk near an FMDN beacon (Pixel Buds, etc.)")
        print("3. Wait 2-5 minutes for automatic upload")
        print("4. Try with elevated log level: adb shell setprop log.tag.FMDN DEBUG")
        return

    print(f"\n{'=' * 80}")
    print("FMDN ENDPOINT ANALYSIS SUMMARY")
    print(f"{'=' * 80}\n")

    # Group by confidence
    by_confidence = defaultdict(list)
    for candidate in candidates:
        by_confidence[candidate.confidence].append(candidate)

    # Deduplicate URLs
    seen_urls: set[str] = set()

    for confidence in ["high", "medium", "low"]:
        if confidence not in by_confidence:
            continue

        confidence_candidates = by_confidence[confidence]
        print(f"\n{confidence.upper()} CONFIDENCE ({len(confidence_candidates)} matches):")
        print("-" * 80)

        for candidate in confidence_candidates:
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)

            print(f"\n  🔗 {candidate.url}")
            print(f"     Method: {candidate.method}")
            print(f"     Source: {candidate.source}")
            if len(candidate.log_line) > LOG_LINE_MAX_LENGTH:
                print(f"     Log: {candidate.log_line[:LOG_LINE_MAX_LENGTH-3]}...")
            else:
                print(f"     Log: {candidate.log_line}")

    # Recommendations
    print(f"\n{'=' * 80}")
    print("RECOMMENDATIONS:")
    print(f"{'=' * 80}\n")

    high_confidence = [c for c in candidates if c.confidence == "high"]
    if high_confidence:
        top_candidate = high_confidence[0]
        print(f"✅ Top candidate: {top_candidate.url}")
        print()
        print("Next steps:")
        print("1. Update google_uploader.py:")
        print(f'   FMDN_UPLOAD_ENDPOINT = "{top_candidate.url}"')
        print("   FMDN_UPLOAD_ENABLED = True")
        print()
        print("2. Test with Home Assistant")
        print("3. Monitor logs for successful uploads")
    else:
        print("⚠️  No high-confidence matches found.")
        print()
        print("Most likely endpoint (based on existing Spot API patterns):")
        print("   https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/UploadLocationReports")
        print()
        print("This pattern matches confirmed working endpoints in the codebase:")
        print("   - CreateBleDevice")
        print("   - GetEidInfoForE2eeDevices")
        print("   - UploadPrecomputedPublicKeyIds")
        print()
        print("Recommended actions:")
        print("1. Increase logcat verbosity:")
        print("   adb shell setprop log.tag.Spot DEBUG")
        print("   adb shell setprop log.tag.FMDN DEBUG")
        print("   adb shell setprop log.tag.GmsClient VERBOSE")
        print()
        print("2. Try PCAPdroid/Wireshark for network capture")
        print("   Look for gRPC traffic (HTTP/2) to spot-pa.googleapis.com")
        print()
        print("3. Decompile Google Play Services APK with JADX")
        print("   Search for: 'UploadLocationReports' or 'LocationReportsUpload'")

    print()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze Android logcat for FMDN endpoint discovery",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Live analysis (requires ADB)
  %(prog)s --live

  # Analyze saved logcat file
  adb logcat > fmdn_logs.txt
  %(prog)s fmdn_logs.txt

  # Increase verbosity and save
  adb shell setprop log.tag.FMDN DEBUG
  adb logcat > detailed_logs.txt
  %(prog)s detailed_logs.txt
        """,
    )

    parser.add_argument(
        "file",
        nargs="?",
        help="Logcat file to analyze (omit for live capture)",
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="Perform live logcat analysis via ADB",
    )

    parser.add_argument(
        "--pattern",
        help="Additional regex pattern to search for",
    )

    args = parser.parse_args()

    # Add custom pattern if provided
    if args.pattern:
        PATTERNS["custom"] = re.compile(args.pattern, re.IGNORECASE)

    # Determine analysis mode
    if args.live:
        candidates = analyze_logcat(file_path=None)
    elif args.file:
        candidates = analyze_logcat(file_path=args.file)
    else:
        parser.print_help()
        sys.exit(1)

    # Print summary
    print_summary(candidates)


if __name__ == "__main__":
    main()
