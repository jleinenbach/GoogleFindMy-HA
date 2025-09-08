# Google Find My Device - Home Assistant Integration

A comprehensive Home Assistant custom integration for Google's Find My Device network, enabling real-time tracking and control of Find My devices directly within Home Assistant.

## Features

- **Real-time Device Tracking**: Track Google Find My devices with fresh GPS location data
- **Advanced Location Filtering**: Intelligent staleness detection and location smoothing to prevent GPS bouncing
- **Device Control**: Locate devices and play sounds remotely
- **Configurable Polling**: Flexible polling intervals with rate limit compliance
- **GoogleFindMyTools Integration**: Uses secrets.json from GoogleFindMyTools for authentication
- **Enhanced Logging**: Comprehensive debugging and monitoring capabilities

## Recent Improvements (v2.0+)

- **Extended FCM Timeout**: Increased from 10s to 60s for better device GPS acquisition
- **Stricter Staleness Filtering**: Rejects location data older than 30 minutes (configurable)
- **Automatic Retry Logic**: Automatically retries when stale location data is received
- **Enhanced FCM Debugging**: Detailed logging for troubleshooting connection issues
- **Location Stability Detection**: Prevents micro-movement bouncing for stationary devices

## Installation

### HACS (Recommended)
1. Add this repository to HACS as a custom repository
2. Install "Google Find My Device" from HACS
3. Restart Home Assistant
4. Add the integration through the UI

### Manual Installation
1. Download this repository
2. Copy the `googlefindmy` folder to `custom_components/`
3. Restart Home Assistant
4. Add the integration through the UI

## Configuration

The integration supports two authentication methods:

### Authentication Setup
1. Run [GoogleFindMyTools](https://github.com/GoogleFindMyTools/GoogleFindMyTools) on a machine with Chrome
2. Complete the authentication process to generate `Auth/secrets.json`
3. Copy the entire contents of the secrets.json file
4. In Home Assistant, paste the secrets.json content when prompted

## Configuration Options

- **Location Poll Interval**: How often to request fresh location data (default: 5 minutes, minimum: 2 minutes)
- **Device Poll Delay**: Delay between individual device polls (default: 5 seconds)
- **Accuracy Threshold**: Minimum GPS accuracy to accept (default: 100 meters)
- **Movement Threshold**: Minimum movement to detect as actual movement vs. GPS drift (default: 50 meters)
- **Staleness Threshold**: Maximum age of location data to accept (default: 30 minutes)

## Device Tracker Features

### Location Smoothing
The integration includes advanced location smoothing to prevent GPS bouncing:
- Filters out readings with poor accuracy
- Detects micro-movements and treats as stationary
- Locks onto stable locations to prevent constant updates
- Configurable movement and accuracy thresholds

### Services

The integration provides several services:

#### `googlefindmy.locate_device`
Request fresh location data for a specific device.

#### `googlefindmy.play_sound`
Play a sound on a specific device for location assistance.

#### `googlefindmy.locate_device_external`
Alternative location method using external process (workaround for FCM issues).

## Troubleshooting

### No Location Data
- Check if devices have moved recently (Find My devices may not update GPS when stationary)
- Verify network connectivity on tracked devices
- Check battery levels (low battery may disable GPS reporting)
- Review logs for staleness warnings

### Stale Location Data
- The integration now rejects location data older than 30 minutes
- Move the device or use it actively to trigger fresh GPS readings
- Consider adjusting the staleness threshold if needed

### Authentication Issues
- Ensure Chrome is installed for OAuth token generation
- Verify Google account has Find My Device enabled
- Check that devices are properly registered in Google Find My Device

### FCM Connection Problems
- Extended timeout allows up to 60 seconds for device response
- Check firewall settings for Firebase Cloud Messaging
- Review FCM debug logs for connection details

## Technical Details

### Architecture
- **Data Coordinator**: Manages sequential device polling with rate limiting
- **Device Tracker**: Implements location smoothing and Home Assistant device_tracker entity
- **API Wrapper**: Interfaces with Google's Nova API and Find My Device network
- **FCM Integration**: Real-time notifications via Firebase Cloud Messaging

### Dependencies
- `selenium` and `undetected-chromedriver` for authentication
- `cryptography` and encryption libraries for E2E location decryption
- `protobuf` for Google API communication
- Firebase and Google authentication libraries

### Rate Limiting
The integration respects Google's rate limits by:
- Sequential device polling (one device at a time)
- Configurable delays between requests
- Minimum poll interval enforcement
- Automatic retry with exponential backoff

## Privacy and Security

- All location data uses Google's end-to-end encryption
- Authentication tokens are securely cached
- No location data is transmitted to third parties
- Local processing of all GPS coordinates

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Test thoroughly with your Find My devices
4. Submit a pull request with detailed description

## License

This project is licensed under the Apache License 2.0 - see the LICENSE file for details.

## Credits

- Built on top of [GoogleFindMyTools](https://github.com/GoogleFindMyTools/GoogleFindMyTools) by Leon Böttger
- Home Assistant integration architecture
- Firebase Cloud Messaging integration
- Enhanced polling and location filtering improvements

## Disclaimer

This integration is not affiliated with Google. Use at your own risk and in compliance with Google's Terms of Service. The developers are not responsible for any misuse or issues arising from the use of this integration.