# custom_components/googlefindmy/example_data_provider.py
"""
Example data provider for Google Find My Device integration.
This module provides example/placeholder data for testing and development.
"""


def get_example_data(key):
    """Return example data for the given key."""
    examples = {
        "sample_identity_key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "sample_owner_key": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
        "sample_device_id": "example_device_12345",
        "sample_encrypted_data": "example_encrypted_payload",
    }

    return examples.get(key, "")
