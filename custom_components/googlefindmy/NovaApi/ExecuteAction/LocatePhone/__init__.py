# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/__init__.py
"""Actions for Android phones that Google lists without a canonical ID.

Such a phone is addressed by its Android ID instead of a canonical ID. Only the
phone actions 1 (ring), 4 (locate) and 5 (stop ringing) are ever built; the others
(2, 6, 7, 13, 14) have no observed meaning and may lock or wipe the phone.
"""
