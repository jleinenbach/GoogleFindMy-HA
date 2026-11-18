# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/__init__.py
"""Actions for Android phones addressed by their Android ID.

This covers phones that Google lists without a canonical ID and, for a manual
locate only, phones listed with a canonical ID whose entry offers action 4
(``owned_phone_target``). Such a phone is addressed by its Android ID. Only the
phone actions 1 (ring), 4 (locate) and 5 (stop ringing) are ever built; the others
(2, 6, 7, 13, 14) have no observed meaning and may lock or wipe the phone.
"""
