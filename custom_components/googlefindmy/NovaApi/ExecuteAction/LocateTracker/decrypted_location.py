# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/decrypted_location.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#


class WrappedLocation:
    def __init__(  # noqa: PLR0913
        self,
        *,
        decrypted_location: bytes,
        time: float,
        accuracy: float,
        status: int,
        is_own_report: bool,
        is_network_report: bool,
        name: str,
    ) -> None:
        if isinstance(decrypted_location, bytearray):
            decrypted_payload = bytes(decrypted_location)
        elif isinstance(decrypted_location, bytes):
            decrypted_payload = decrypted_location
        else:
            msg = "decrypted_location must be bytes"
            raise TypeError(msg)

        self.time: float = time
        self.status: int = status
        self.decrypted_location: bytes = decrypted_payload
        # Invariant: a network report is never an owner report. The server-supplied
        # ``is_own_report`` flag is unreliable: this integration's own crowdsourced
        # uploader (fmdn_finder/location_uploader.py:586) stamps network reports with
        # ``isOwnReport=True`` while carrying a non-empty publicKeyRandom. Cryptographic
        # provenance (``is_network_report``) is authoritative, so enforcing the
        # mutual exclusion here keeps every downstream consumer that reads
        # ``is_own_report`` as owner provenance (row_source_label, FCM crowd-source
        # stats, map view, ranking) from misclassifying an admitted network fix.
        self.is_own_report: bool = is_own_report and not is_network_report
        # Cryptographic provenance: True when this report came from the FMDN
        # network side (a foreign/crowdsourced ECDH report, or a SEMANTIC network
        # hit) rather than one of THIS device's own AES-GCM server reports. This is
        # the authoritative decrypt-path signal and, unlike the server-supplied
        # ``is_own_report`` flag, is not spoofed to True by this integration's own
        # crowdsourced uploader (fmdn_finder/location_uploader.py).
        self.is_network_report: bool = is_network_report
        self.accuracy: float = accuracy
        self.name: str = name
