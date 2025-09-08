# Installation Guide

## Prerequisites

- Home Assistant (version 2023.1 or later)
- Google account with Find My Device enabled
- At least one device registered in Google Find My Device
- For authentication: Computer with Google Chrome browser

## Method 1: HACS Installation (Recommended)

1. **Add Custom Repository**:
   - Open HACS in Home Assistant
   - Go to Integrations
   - Click the three dots menu → Custom repositories
   - Add this repository URL
   - Select "Integration" as the category

2. **Install Integration**:
   - Search for "Google Find My Device"
   - Click Install
   - Restart Home Assistant

3. **Add Integration**:
   - Go to Settings → Devices & Services
   - Click Add Integration
   - Search for "Google Find My Device"
   - Follow the setup wizard

## Method 2: Manual Installation

1. **Download Integration**:
   ```bash
   cd /config/custom_components
   git clone https://github.com/YOUR_USERNAME/googlefindmy-homeassistant.git googlefindmy
   ```

2. **Restart Home Assistant**:
   - Restart your Home Assistant instance
   - Check that the integration loads without errors

3. **Add Integration**:
   - Go to Settings → Devices & Services
   - Click Add Integration
   - Search for "Google Find My Device"

## Authentication Setup

### Method 1: GoogleFindMyTools secrets.json (Recommended)

This method provides the most stable authentication and full feature access.

1. **Install GoogleFindMyTools**:
   ```bash
   git clone https://github.com/GoogleFindMyTools/GoogleFindMyTools.git
   cd GoogleFindMyTools
   pip install -r requirements.txt
   ```

2. **Run Authentication**:
   ```bash
   python main.py
   ```
   - Follow the Chrome browser authentication flow
   - Complete the Google login process
   - Wait for `Auth/secrets.json` to be generated

3. **Copy Secrets Data**:
   - Open `Auth/secrets.json` in a text editor
   - Copy the entire file contents
   - Keep this data secure and private

4. **Configure in Home Assistant**:
   - In the integration setup, paste the complete secrets.json content
   - Complete the device selection

## Configuration Options

### Device Selection
- Select which Find My devices to track in Home Assistant
- You can modify this list later through integration options

### Polling Configuration
- **Location Poll Interval**: How often to request fresh GPS data (default: 5 minutes)
- **Device Poll Delay**: Delay between individual device requests (default: 5 seconds)
- **Minimum Poll Interval**: Enforced minimum to respect rate limits (2 minutes)

### Location Filtering
- **Accuracy Threshold**: Reject GPS readings with poor accuracy (default: 100 meters)
- **Movement Threshold**: Minimum movement to detect vs. GPS drift (default: 50 meters)
- **Staleness Threshold**: Maximum age of acceptable location data (default: 30 minutes)

## Troubleshooting Installation

### Integration Not Found
```
ERROR: Integration 'googlefindmy' not found
```
**Solution**: Ensure the integration folder is correctly placed in `custom_components/googlefindmy/`

### Missing Dependencies
```
ERROR: Could not install packages
```
**Solution**: 
- Restart Home Assistant to trigger automatic dependency installation
- Check Home Assistant logs for specific missing packages
- Manually install dependencies if needed

### Authentication Failures
```
ERROR: Failed to authenticate with Google
```
**Solution**:
- Verify Google account has Find My Device enabled
- Check that Chrome browser completes authentication successfully
- Ensure tokens are copied correctly without extra spaces or characters
- Try regenerating authentication tokens

### Chrome Browser Issues
```
ERROR: Chrome/Chromium not found
```
**Solution**:
- Install Google Chrome: https://www.google.com/chrome/
- Ensure Chrome is in system PATH
- Try running authentication on a different machine with Chrome

### Rate Limiting
```
WARNING: API rate limit exceeded
```
**Solution**:
- Increase poll intervals in configuration
- Reduce number of tracked devices
- Wait for rate limit to reset (usually 1 hour)

## Post-Installation

### Verify Installation
1. **Check Device Entities**:
   - Go to Developer Tools → States
   - Look for `device_tracker.` entities for your Find My devices
   - Verify location coordinates are populated

2. **Test Services**:
   - Go to Developer Tools → Services
   - Try `googlefindmy.locate_device` service
   - Check logs for successful location requests

3. **Review Configuration**:
   - Go to Settings → Devices & Services → Google Find My Device
   - Verify device list and configuration options
   - Adjust settings as needed

### Integration Options
Access integration options through:
- Settings → Devices & Services → Google Find My Device → Configure

Available options:
- Device selection (add/remove tracked devices)
- Polling intervals and delays
- Location filtering thresholds
- Debug logging levels

## Security Considerations

- **Authentication Data**: Keep secrets.json and OAuth tokens secure
- **Network Access**: Integration communicates with Google's servers
- **Local Storage**: Tokens are cached locally in Home Assistant
- **Privacy**: Location data is processed locally, not shared with third parties

## Getting Help

- **Integration Issues**: Check Home Assistant logs for detailed error messages
- **Authentication Problems**: Verify Google account settings and device registration
- **Device Not Updating**: Check device connectivity and battery levels
- **Performance Issues**: Adjust polling intervals and device limits

For additional support, check the GitHub repository issues and discussions.