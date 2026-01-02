# FMDN Analysis Tools

Tools for discovering and analyzing Google FMDN (Find My Device Network) endpoints.

## analyze_fmdn_logs.py

Automated logcat analysis script for identifying FMDN upload endpoints.

### Features

- ✅ Live logcat capture via ADB
- ✅ Pattern matching for endpoint URLs
- ✅ Confidence scoring (high/medium/low)
- ✅ Automatic deduplication
- ✅ Ranked recommendations

### Requirements

```bash
# Python 3.8+
# Android Platform Tools (ADB)
sudo apt install android-tools-adb  # Ubuntu/Debian
# OR
brew install android-platform-tools  # macOS
```

### Usage

**Live Analysis** (Recommended):

```bash
# Start live capture
python3 analyze_fmdn_logs.py --live

# On Android device:
# 1. Enable "Find My Device" in settings
# 2. Walk near FMDN beacon (Pixel Buds, etc.)
# 3. Wait 2-5 minutes for automatic upload
# 4. Script will show HIGH CONFIDENCE matches automatically
```

**Analyze Saved Logs:**

```bash
# Capture logs to file
adb logcat > fmdn_logs.txt

# Analyze
python3 analyze_fmdn_logs.py fmdn_logs.txt
```

**Custom Pattern Search:**

```bash
# Search for specific keywords
python3 analyze_fmdn_logs.py --pattern "beacon.*upload" fmdn_logs.txt
```

### Output Example

```
🎯 HIGH CONFIDENCE MATCH:
   URL: /v1/uploadLocationReports
   Method: POST
   Source: GmsClient (line 1234)
   Log: POST https://spot-pa.googleapis.com/v1/uploadLocationReports ...

RECOMMENDATIONS:
✅ Top candidate: /v1/uploadLocationReports

Next steps:
1. Update google_uploader.py:
   FMDN_UPLOAD_ENDPOINT = "uploadLocationReports"
   FMDN_UPLOAD_ENABLED = True
```

### Troubleshooting

**No matches found:**

```bash
# Increase log verbosity
adb shell setprop log.tag.Spot DEBUG
adb shell setprop log.tag.FMDN DEBUG
adb shell setprop log.tag.GmsClient VERBOSE

# Retry analysis
python3 analyze_fmdn_logs.py --live
```

**ADB connection issues:**

```bash
# Check device connection
adb devices

# If empty, enable USB debugging on Android
# Settings → Developer Options → USB Debugging

# Reconnect
adb kill-server
adb start-server
```

**Permission errors:**

With Shizuku:
- Ensure Shizuku is running on Android
- Grant ADB permissions in Shizuku app
- Use wireless ADB for better permissions

### Pattern Reference

The script searches for these patterns:

| Pattern | Example Match |
|---------|--------------|
| Full URL | `https://spot-pa.googleapis.com/v1/upload...` |
| URL Path | `POST /v1/uploadLocationReports` |
| Endpoint Name | `endpoint: "UploadLocationReports"` |
| Protobuf Class | `LocationReportsUpload` |
| API Call | `uploadLocationReport(...)` |

### High-Confidence Keywords

- `LocationReportsUpload`
- `UploadLocationReports`
- `uploadLocationReports`
- `/v1/upload`
- `spot-pa.googleapis.com`

### Relevant Logcat Tags

- `GmsClient` - Google Mobile Services
- `Spot` - FMDN/Spot API
- `FMDN` - Find My Device Network
- `FindMy` - Find My services
- `NetworkRequest` - HTTP requests
- `Chimera` - Play Services module loader
- `Volley` - HTTP library
- `OkHttp` - HTTP client

### Integration

After finding the endpoint:

```bash
# 1. Edit google_uploader.py
nano ../custom_components/googlefindmy/fmdn_finder/google_uploader.py

# 2. Update lines 32-33:
FMDN_UPLOAD_ENDPOINT = "YourFoundEndpoint"  # e.g., "uploadLocationReports"
FMDN_UPLOAD_ENABLED = True

# 3. Restart Home Assistant
systemctl restart home-assistant

# 4. Monitor logs
tail -f ~/.homeassistant/home-assistant.log | grep -i fmdn
```

## Contributing

Found a working endpoint? Please contribute:

1. Document the endpoint details:
   - Exact URL path
   - Android version
   - Play Services version
   - Discovery method used

2. Create a Pull Request with:
   - Updated `google_uploader.py`
   - Documentation in commit message

3. Share in Discord/GitHub Issues to help the community!

## See Also

- [FMDN_ENDPOINT_DISCOVERY.md](../docs/FMDN_ENDPOINT_DISCOVERY.md) - Complete discovery guide
- [QUICK_START_SHIZUKU.md](../docs/QUICK_START_SHIZUKU.md) - Fast-track Shizuku guide
- [FMDN.md](../custom_components/googlefindmy/FMDN.md) - FMDN specification
