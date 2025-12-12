from custom_components.googlefindmy.FMDNCrypto.eid_generator import generate_eid_p256

EXPECTED_X_COORDINATE = bytes.fromhex(
    "f17b297f9fb1246139ad5de464c9044b2846f0964cc7b2a11be9942ff0a5c2c4"
)


def test_generate_eid_p256_matches_known_x_coordinate() -> None:
    identity_key = bytes(range(32))
    timestamp = 3600

    eid = generate_eid_p256(identity_key, timestamp)

    assert eid == EXPECTED_X_COORDINATE
    assert len(eid) == 32


def test_generate_eid_p256_rejects_short_keys() -> None:
    assert generate_eid_p256(b"short", 0) == b""
