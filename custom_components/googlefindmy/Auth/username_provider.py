#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from custom_components.googlefindmy.Auth.token_cache import get_cached_value, async_get_cached_value

username_string = 'username'

def get_username():
    username = get_cached_value(username_string)

    if username is not None:
        return username

    # For Home Assistant integration, we need a Google email
    # This should ideally be configured through the UI
    return "user@gmail.com"  # Placeholder - should be configured

async def async_get_username():
    username = await async_get_cached_value(username_string)

    if username is not None:
        return username

    # For Home Assistant integration, we need a Google email
    # This should ideally be configured through the UI
    return "user@gmail.com"  # Placeholder - should be configured

if __name__ == '__main__':
    import asyncio
    asyncio.run(async_get_username())
