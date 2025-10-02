#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
import struct

class GrpcParser:
    @staticmethod
    def extract_grpc_payload(grpc: bytes) -> bytes:
        # Defensive guards for gRPC length-prefixed frame: 1 byte flag + 4 bytes length
        # See gRPC over HTTP/2 framing docs.
        if not grpc or len(grpc) < 5:
            raise ValueError("Invalid GRPC payload (too short for frame header)")

        flag = grpc[0]
        if flag not in (0, 1):
            raise ValueError(f"Invalid GRPC payload (bad compressed-flag {flag})")

        length = struct.unpack(">I", grpc[1:5])[0]
        if len(grpc) < 5 + length:
            raise ValueError(f"Invalid GRPC payload length (expected {length}, got {len(grpc) - 5})")

        # Extract exactly one message frame (unary RPC)
        return grpc[5:5 + length]

    @staticmethod
    def construct_grpc(payload: bytes) -> bytes:
        # Not compressed
        compressed = bytes([0])
        length_data = struct.pack(">I", len(payload))
        return compressed + length_data + payload
