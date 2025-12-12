from custom_components.googlefindmy.FMDNCrypto.eid_generator import generate_eid_p256

EXPECTED_X_COORDINATE = bytes.fromhex(
    "7410de6210529ba183a70ec0fd11beee18de9f806bd8903e52771cd00fc22f19"
)


def test_generate_eid_p256_matches_known_x_coordinate() -> None:
    identity_key = bytes(range(32))
    timestamp = 3600

    eid = generate_eid_p256(identity_key, timestamp)

    assert eid == EXPECTED_X_COORDINATE
    assert len(eid) == 32


def test_generate_eid_p256_rejects_short_keys() -> None:
    assert generate_eid_p256(b"short", 0) == b""
