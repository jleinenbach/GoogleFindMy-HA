"""Home Assistant compatible FCM receiver for Google Find My Device."""
import asyncio
import base64
import binascii
import logging
from typing import Optional, Callable, Dict, Any

from custom_components.googlefindmy.Auth.token_cache import (
    set_cached_value, 
    get_cached_value, 
    async_set_cached_value, 
    async_get_cached_value,
    async_load_cache_from_file
)

_LOGGER = logging.getLogger(__name__)


class FcmReceiverHA:
    """FCM Receiver that works with Home Assistant's async architecture."""
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(FcmReceiverHA, cls).__new__(cls, *args, **kwargs)
        return cls._instance
    
    def __init__(self):
        """Initialize the FCM receiver for Home Assistant."""
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True
        
        self.credentials = None
        self.location_update_callbacks = []
        self.pc = None
        self._listening = False
        self._listen_task = None
        
        # Firebase project configuration for Google Find My Device
        self.project_id = "google.com:api-project-289722593072"
        self.app_id = "1:289722593072:android:3cfcf5bc359f0308"
        self.api_key = "AIzaSyD_gko3P392v6how2H7UpdeXQ0v2HLettc"
        self.message_sender_id = "289722593072"
        
        # Note: credentials will be loaded asynchronously in async_initialize
        
    async def async_initialize(self):
        """Async initialization that works with Home Assistant."""
        try:
            # Load cached credentials asynchronously to avoid blocking I/O
            await async_load_cache_from_file()
            self.credentials = await async_get_cached_value('fcm_credentials')
            
            # Import FCM libraries
            from custom_components.googlefindmy.Auth.firebase_messaging import FcmRegisterConfig, FcmPushClient
            
            fcm_config = FcmRegisterConfig(
                project_id=self.project_id,
                app_id=self.app_id,
                api_key=self.api_key,
                messaging_sender_id=self.message_sender_id,
                bundle_id="com.google.android.apps.adm",
            )
            
            # Create push client with callbacks
            self.pc = FcmPushClient(
                self._on_notification, 
                fcm_config, 
                self.credentials, 
                self._on_credentials_updated
            )
            
            _LOGGER.info("FCM receiver initialized successfully")
            return True
            
        except Exception as e:
            _LOGGER.error(f"Failed to initialize FCM receiver: {e}")
            return False
    
    async def async_register_for_location_updates(self, callback: Callable) -> Optional[str]:
        """Register for location updates asynchronously."""
        try:
            # Add callback to list
            self.location_update_callbacks.append(callback)
            
            # If not listening, start listening
            if not self._listening:
                await self._start_listening()
            
            # Return FCM token if available
            if self.credentials and 'fcm' in self.credentials and 'registration' in self.credentials['fcm']:
                token = self.credentials['fcm']['registration']['token']
                _LOGGER.info(f"FCM token available: {token[:20]}...")
                return token
            else:
                _LOGGER.warning("FCM credentials not available")
                return None
                
        except Exception as e:
            _LOGGER.error(f"Failed to register for location updates: {e}")
            return None
    
    async def _start_listening(self):
        """Start listening for FCM messages."""
        try:
            if not self.pc:
                await self.async_initialize()
            
            if self.pc:
                # Register with FCM
                await self._register_for_fcm()
                
                # Start listening in background task
                self._listen_task = asyncio.create_task(self._listen_for_messages())
                self._listening = True
                _LOGGER.info("Started listening for FCM notifications")
            else:
                _LOGGER.error("Failed to create FCM push client")
                
        except Exception as e:
            _LOGGER.error(f"Failed to start FCM listening: {e}")
    
    async def _register_for_fcm(self):
        """Register with FCM to get token."""
        if not self.pc:
            return
            
        fcm_token = None
        retries = 0
        
        while fcm_token is None and retries < 3:
            try:
                fcm_token = await self.pc.checkin_or_register()
                if fcm_token:
                    _LOGGER.info(f"FCM registration successful, token: {fcm_token[:20]}...")
                else:
                    _LOGGER.warning(f"FCM registration attempt {retries + 1} failed")
                    retries += 1
                    await asyncio.sleep(5)
            except Exception as e:
                _LOGGER.error(f"FCM registration error: {e}")
                retries += 1
                await asyncio.sleep(5)
    
    async def _listen_for_messages(self):
        """Listen for FCM messages in background."""
        try:
            if self.pc:
                await self.pc.start()
                _LOGGER.info("FCM message listener started")
        except Exception as e:
            _LOGGER.error(f"FCM listen error: {e}")
            self._listening = False
    
    def _on_notification(self, obj: Dict[str, Any], notification, data_message):
        """Handle incoming FCM notification."""
        try:
            # Check if the payload is present
            if 'data' in obj and 'com.google.android.apps.adm.FCM_PAYLOAD' in obj['data']:
                # Decode the base64 string with padding fix
                base64_string = obj['data']['com.google.android.apps.adm.FCM_PAYLOAD']
                
                # Add proper Base64 padding if missing
                missing_padding = len(base64_string) % 4
                if missing_padding:
                    base64_string += '=' * (4 - missing_padding)
                
                try:
                    decoded_bytes = base64.b64decode(base64_string)
                except Exception as decode_error:
                    _LOGGER.error(f"Failed to decode Base64 FCM payload: {decode_error}")
                    _LOGGER.debug(f"Problematic Base64 string: {base64_string[:50]}...")
                    return
                
                # Convert to hex string
                hex_string = binascii.hexlify(decoded_bytes).decode('utf-8')
                
                _LOGGER.info(f"Received FCM location response: {len(hex_string)} chars")
                
                # Call all registered callbacks asynchronously to avoid blocking
                for callback in self.location_update_callbacks:
                    try:
                        # Run callback in executor to avoid blocking the event loop
                        asyncio.create_task(self._run_callback_async(callback, hex_string))
                    except Exception as e:
                        _LOGGER.error(f"Error scheduling FCM callback: {e}")
            else:
                _LOGGER.debug("FCM notification without location payload")
                
        except Exception as e:
            _LOGGER.error(f"Error processing FCM notification: {e}")
    
    async def _run_callback_async(self, callback, hex_string):
        """Run callback in executor to avoid blocking the event loop."""
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            # Run the potentially blocking callback in a thread executor
            await loop.run_in_executor(None, callback, hex_string)
        except Exception as e:
            _LOGGER.error(f"Error in async FCM callback: {e}")
    
    def _on_credentials_updated(self, creds):
        """Handle credential updates."""
        self.credentials = creds
        # Schedule async update to avoid blocking I/O in callback
        asyncio.create_task(self._async_save_credentials())
        _LOGGER.info("FCM credentials updated")
    
    async def _async_save_credentials(self):
        """Save credentials asynchronously."""
        try:
            await async_set_cached_value('fcm_credentials', self.credentials)
        except Exception as e:
            _LOGGER.error(f"Failed to save FCM credentials: {e}")
    
    async def async_stop(self):
        """Stop listening for FCM messages."""
        try:
            if self._listen_task:
                self._listen_task.cancel()
                
            if self.pc:
                await self.pc.stop()
                
            self._listening = False
            _LOGGER.info("FCM receiver stopped")
            
        except Exception as e:
            _LOGGER.error(f"Error stopping FCM receiver: {e}")
    
    def get_fcm_token(self) -> Optional[str]:
        """Get current FCM token if available."""
        if self.credentials and 'fcm' in self.credentials and 'registration' in self.credentials['fcm']:
            return self.credentials['fcm']['registration']['token']
        return None