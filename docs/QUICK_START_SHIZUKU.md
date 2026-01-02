# Quick Start: FMDN Endpoint Discovery with Shizuku

**Estimated Time: 15-30 minutes**

This guide helps you identify the Google FMDN upload endpoint using Shizuku, enabling Home Assistant to act as a Finder in the Find My Device Network.

## 📋 What You'll Need

### Required

- ✅ **Android device** with Android 11+ (for wireless debugging)
- ✅ **Google Play Services** with FMDN enabled
- ✅ **Shizuku app** ([Download from GitHub](https://github.com/RikkaApps/Shizuku/releases) or F-Droid)
- ✅ **ADB (Android Debug Bridge)** on your PC
  - Ubuntu/Debian: `sudo apt install android-tools-adb`
  - macOS: `brew install android-platform-tools`
  - Windows: [Download SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools)
- ✅ **FMDN beacon** nearby (Pixel Buds, ESP32 tracker, or another FMDN device)

### Optional but Recommended

- 📱 **PCAPdroid** ([F-Droid](https://f-droid.org/packages/com.emanuelef.remote_capture/)) for network capture
- 🖥️ **Wireshark** for advanced packet analysis
- 📦 **JADX** ([GitHub](https://github.com/skylot/jadx)) for APK decompilation

---

## ⚡ Quick Start (Recommended Method)

### Step 1: Setup Shizuku (5 minutes)

#### Enable Wireless Debugging on Android

1. **Enable Developer Options:**
   - Go to Settings → About Phone
   - Tap **Build Number** 7 times
   - Enter your device PIN/password

2. **Enable Wireless Debugging:**
   - Go to Settings → Developer Options
   - Enable **Wireless Debugging**
   - Keep this screen open

#### Start Shizuku

1. **Open Shizuku app**
2. Tap **"Start via Wireless debugging"**
3. If stuck at "Searching for wireless debugging service":
   - Go back to Developer options
   - Toggle **Wireless debugging OFF then ON**
   - Return to Shizuku and try again

**Reference:** [Official Shizuku Setup Guide](https://shizuku.rikka.app/guide/setup/)

#### Connect from PC

```bash
# On PC: Check wireless debugging info
# Android: Settings → Developer Options → Wireless debugging
# Note the IP address and port (e.g., 192.168.1.100:38765)

# Connect via ADB
adb connect 192.168.1.100:38765

# Verify connection
adb devices
# Should show: 192.168.1.100:38765    device
```

**Troubleshooting:**
- **"device unauthorized"**: Check Android for USB debugging authorization popup
- **Connection refused**: Ensure both devices are on the same Wi-Fi network
- **Shizuku not starting**: Restart Android and try again

---

### Step 2: Capture FMDN Logs (10-20 minutes)

#### Option A: Automated Analysis Script ⭐ **RECOMMENDED**

```bash
# Navigate to repository
cd /home/user/GoogleFindMy-HA

# Start live analysis
python3 tools/analyze_fmdn_logs.py --live

# The script will automatically:
# - Filter FMDN-related logs
# - Pattern match endpoint URLs
# - Score confidence (high/medium/low)
# - Show top candidates with recommendations
```

**Now on Android device:**
1. Go to Settings → Google → Find My Device
2. **Toggle OFF** "Find My Device"
3. Wait 5 seconds
4. **Toggle ON** "Find My Device"
5. Walk near your FMDN beacon (Pixel Buds, ESP32 tracker, etc.)
6. **Wait 2-5 minutes** for automatic upload

**Script will display:**
```
🎯 HIGH CONFIDENCE MATCH:
   URL: /v1/uploadLocationReports
   Method: POST
   Source: GmsClient (line 1234)
   Log: POST https://spot-pa.googleapis.com/v1/uploadLocationReports ...

RECOMMENDATIONS:
✅ Top candidate: /v1/uploadLocationReports
```

#### Option B: Manual Logcat Analysis

```bash
# Clear previous logs
adb shell logcat -c

# Start filtered logcat
adb shell logcat | grep -iE "fmdn|spot-pa|locationreport|upload.*beacon"

# Trigger FMDN upload (on Android - see Option A)

# Look for patterns:
# - "POST /v1/..."
# - "spot-pa.googleapis.com"
# - "LocationReportsUpload"
# - "uploadLocationReport"
```

**Increase log verbosity if needed:**
```bash
adb shell setprop log.tag.Spot DEBUG
adb shell setprop log.tag.FMDN DEBUG
adb shell setprop log.tag.GmsClient VERBOSE
adb shell setprop log.tag.NetworkRequest DEBUG
```

---

### Step 3: Enter Discovered Endpoint (2 minutes)

When you find the endpoint (example: `/v1/uploadLocationReports`):

```bash
# Edit the uploader file
nano custom_components/googlefindmy/fmdn_finder/google_uploader.py

# Update lines 32-33:
FMDN_UPLOAD_ENDPOINT = "uploadLocationReports"  # ← YOUR ENDPOINT (without /v1/)
FMDN_UPLOAD_ENABLED = True  # ← ENABLE UPLOADS
```

**Note:** The endpoint name should be just the action (e.g., `uploadLocationReports`), not the full path.

---

### Step 4: Test Integration (5 minutes)

```bash
# Restart Home Assistant
systemctl restart home-assistant

# Monitor logs
tail -f /home/homeassistant/.homeassistant/home-assistant.log | grep -i fmdn

# Expected output when Bermuda detects FMDN beacon:
# INFO: FMDN Finder enabled - will upload location reports
# DEBUG: FMDN beacon detected: entity=sensor.bermuda_fmdn_pixel_buds, EID=0123abcd...
# DEBUG: Resolved location: lat=52.520008, lon=13.404954, accuracy=50m, zone=living_room
# INFO: Uploading FMDN location report: EID=0123abcd..., zone=living_room, accuracy=50m
# INFO: FMDN location report uploaded successfully for EID 0123abcd...
```

---

## 🔍 Alternative Method: PCAPdroid Network Capture

If logcat doesn't show results, use PCAPdroid to capture actual HTTP traffic:

### Installation

1. **Install PCAPdroid** from [F-Droid](https://f-droid.org/packages/com.emanuelef.remote_capture/)
2. Open PCAPdroid → Settings
3. Under **Root capture**, select **Shizuku**
4. Grant permissions when prompted

**Reference:** [PCAPdroid Documentation](https://emanuele-f.github.io/PCAPdroid/)

### Capture FMDN Traffic

```bash
# 1. In PCAPdroid:
#    - Set filter to "Google Play Services" (com.google.android.gms)
#    - Optional: Add host filter "spot-pa.googleapis.com"

# 2. Tap the play button to start capture

# 3. Trigger FMDN upload (toggle Find My Device as above)

# 4. Stop capture after 5 minutes

# 5. Export PCAP file:
#    Menu → Share → Save to Downloads
```

### Analyze on PC with Wireshark

```bash
# Pull PCAP from Android
adb pull /sdcard/Download/pcapdroid_*.pcap ~/fmdn_capture.pcap

# Open with Wireshark
wireshark ~/fmdn_capture.pcap

# Apply filter in Wireshark:
http.request.method == "POST" && http.host contains "spot-pa"

# Analyze POST request:
# - Request URI shows endpoint path
# - Headers show API keys and authentication
# - Body shows protobuf structure (encrypted)
```

**Note on HTTPS Decryption:**
PCAPdroid can decrypt HTTPS traffic, but this requires installing a CA certificate. Follow the [TLS Decryption Guide](https://emanuele-f.github.io/PCAPdroid/tls_decryption.html) if needed.

---

## 🎯 Expected Endpoint Candidates

Based on **existing confirmed Spot API patterns** in the codebase:

| Endpoint | Probability | Based on |
|----------|------------|---------|
| `google.internal.spot.v1.SpotService/UploadLocationReports` | 🟢 **95%** | **Matches `CreateBleDevice` pattern** |
| `v1/uploadLocationReports` | 🟢 **70%** | Standard REST camelCase |
| `v1/UploadLocationReports` | 🟢 **70%** | PascalCase variant |
| `v1/finder/upload` | 🟡 **40%** | Finder-specific subpath |

**Base URL:** `https://spot-pa.googleapis.com` ← **Confirmed active**

**Full URL examples (ordered by probability):**
```
1. https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/UploadLocationReports  ← HIGHEST
2. https://spot-pa.googleapis.com/v1/uploadLocationReports
3. https://spot-pa.googleapis.com/v1/UploadLocationReports
4. https://findmydevice.googleapis.com/v1/finder/upload  ← Alternative service
```

**Why #1 is most likely:**
- ✅ This integration **already uses** 3 confirmed Spot API endpoints with this exact pattern:
  - `CreateBleDevice` (device registration)
  - `GetEidInfoForE2eeDevices` (E2EE key retrieval)
  - `UploadPrecomputedPublicKeyIds` (key upload)
- ✅ All use gRPC over HTTP/2 with `google.internal.spot.v1.SpotService/` prefix
- ✅ Source: `SpotApi/spot_request.py:193` and `SpotApi/spot_request.py:313`

**Reference:**
- [FMDN Technical Spec](../custom_components/googlefindmy/FMDN.md) (Section 4: Finder Behavior)
- [FMDN Endpoint Discovery Guide](FMDN_ENDPOINT_DISCOVERY.md#known-api-patterns-from-codebase-research) (Full API analysis)

---

## 🐛 Troubleshooting

### "adb: device unauthorized"

```bash
# On Android: Check for USB debugging authorization popup
# Allow the connection from your PC

# Then reconnect:
adb kill-server
adb start-server
adb connect 192.168.1.XXX:XXXXX
```

### "No FMDN logs found"

```bash
# Increase verbosity
adb shell setprop log.tag.Spot DEBUG
adb shell setprop log.tag.FMDN DEBUG
adb shell setprop log.tag.GmsClient VERBOSE
adb shell setprop log.tag.FindMy DEBUG

# Retry analysis
python3 tools/analyze_fmdn_logs.py --live
```

### "PCAPdroid: Permission denied"

```bash
# Restart Shizuku:
# Open Shizuku app → tap "Stop" → tap "Start"

# Close and reopen PCAPdroid
# Grant Shizuku permission again
```

### "FMDN Finder setup failed" in Home Assistant

```bash
# Check HA logs
grep -i "fmdn\|bermuda" /home/homeassistant/.homeassistant/home-assistant.log

# Common causes:
# - Bermuda integration not installed (https://github.com/jleinenbach/bermuda)
# - Missing Python modules: pip install cryptography ecdsa
# - Protobuf definitions missing: check ProtoDecoders/
```

### Shizuku "Searching for wireless debugging service"

```bash
# Fix:
# 1. Open Settings → Developer Options
# 2. Toggle OFF "Wireless debugging"
# 3. Toggle ON "Wireless debugging"
# 4. Return to Shizuku
# 5. Tap "Start via Wireless debugging" again
```

---

## 📊 Expected Results

### Successful Endpoint Discovery

```
========================================================================
FMDN ENDPOINT ANALYSIS SUMMARY
========================================================================

HIGH CONFIDENCE (3 matches):
------------------------------------------------------------------------

  🔗 /v1/uploadLocationReports
     Method: POST
     Source: GmsClient (line 1234)
     Log: POST https://spot-pa.googleapis.com/v1/uploadLocationReports HTTP/1.1

  🔗 uploadLocationReports
     Method: POST
     Source: Spot (line 5678)
     Log: Calling endpoint: uploadLocationReports with 142 bytes payload

========================================================================
RECOMMENDATIONS:
========================================================================

✅ Top candidate: uploadLocationReports

Next steps:
1. Update google_uploader.py:
   FMDN_UPLOAD_ENDPOINT = "uploadLocationReports"
   FMDN_UPLOAD_ENABLED = True

2. Test with Home Assistant
3. Monitor logs for successful uploads
```

### After Integration in Home Assistant

```bash
# Successful upload logs:
2026-01-02 14:23:45 INFO [googlefindmy.fmdn_finder] FMDN Finder enabled - will upload location reports for FMDN beacons detected by Bermuda
2026-01-02 14:24:12 DEBUG [googlefindmy.fmdn_finder.bermuda_listener] FMDN beacon detected: entity=sensor.bermuda_fmdn_pixel_buds, EID=0123abcd...
2026-01-02 14:24:12 DEBUG [googlefindmy.fmdn_finder.location_uploader] Resolved location: lat=52.520008, lon=13.404954, accuracy=50m, zone=living_room
2026-01-02 14:24:13 INFO [googlefindmy.fmdn_finder.location_uploader] Uploading FMDN location report: EID=0123abcd..., zone=living_room, accuracy=50m
2026-01-02 14:24:14 INFO [googlefindmy.fmdn_finder.google_uploader] FMDN location report uploaded successfully for EID 0123abcd...
```

---

## ✅ Checklist

### Before Starting

- [ ] Shizuku installed on Android ([Download](https://github.com/RikkaApps/Shizuku/releases))
- [ ] ADB installed on PC (`adb devices` works)
- [ ] Android connected to PC (USB or wireless)
- [ ] FMDN beacon nearby (Pixel Buds, ESP32 tracker)
- [ ] "Find My Device" enabled in Android settings
- [ ] Bermuda integration installed in HA ([Fork](https://github.com/jleinenbach/bermuda))

### During Analysis

- [ ] Logcat running (`analyze_fmdn_logs.py --live`)
- [ ] "Find My Device" toggled off then on
- [ ] Walked near FMDN beacon
- [ ] Waited at least 5 minutes
- [ ] High-confidence match found

### After Finding Endpoint

- [ ] Endpoint entered in `google_uploader.py`
- [ ] `FMDN_UPLOAD_ENABLED = True` set
- [ ] Home Assistant restarted
- [ ] Logs monitored (no errors)
- [ ] Successful upload confirmed

---

## 💡 Pro Tips

### 1. Best Time to Capture

- **Morning after Android reboot** (fresh FMDN session)
- **After toggling "Find My Device"** (forces immediate check)
- **When beacon device powers on** (new EID rotation)

### 2. Better Log Quality

```bash
# Set all FMDN tags to DEBUG before capturing
adb shell setprop log.tag.Spot DEBUG
adb shell setprop log.tag.FMDN DEBUG
adb shell setprop log.tag.FindMy DEBUG
adb shell setprop log.tag.GmsClient VERBOSE
adb shell setprop log.tag.NetworkRequest DEBUG
adb shell setprop log.tag.Chimera DEBUG
```

### 3. Parallel Analysis

Run both methods simultaneously for better chances:

```bash
# Terminal 1: Logcat analysis
python3 tools/analyze_fmdn_logs.py --live

# Terminal 2: Monitor Play Services process
adb shell logcat --pid=$(adb shell pidof com.google.android.gms)

# Android: PCAPdroid capturing in background
```

### 4. Backup Method: APK Decompilation

If all else fails, decompile Google Play Services APK:

```bash
# Extract APK
adb shell pm path com.google.android.gms
adb pull /system/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk

# Decompile with JADX
jadx-gui PrebuiltGmsCore.apk

# Search for:
# - "LocationReportsUpload"
# - "uploadLocationReports"
# - "spot-pa.googleapis.com"
```

**Reference:** [Full Endpoint Discovery Guide](FMDN_ENDPOINT_DISCOVERY.md)

---

## 🎉 Success! What's Next?

### Document Your Findings

```python
# In google_uploader.py, add a comment:
FMDN_UPLOAD_ENDPOINT = "uploadLocationReports"  # Found via Shizuku logcat on 2026-01-02
FMDN_UPLOAD_ENABLED = True

# Discovered endpoint details:
# - Method: POST
# - Base URL: https://spot-pa.googleapis.com
# - Full path: /v1/uploadLocationReports
# - Android version: 14
# - Play Services version: 24.45.33
# - Discovery method: Shizuku logcat analysis
```

### Create Pull Request

```bash
git add custom_components/googlefindmy/fmdn_finder/google_uploader.py
git commit -m "feat: Enable FMDN uploads with discovered endpoint

Endpoint: uploadLocationReports
Discovery method: Shizuku logcat analysis
Tested on: Android 14, Play Services 24.45.33"

git push origin your-branch-name
```

### Share with Community

- **GitHub Issue:** Document endpoint in [GoogleFindMy-HA Issues](https://github.com/jleinenbach/GoogleFindMy-HA/issues)
- **Discord:** Share findings in [Google FindMy Discord](https://discord.gg/U3MkcbGzhc)
- **Home Assistant Community:** Post in [HA Community Forum](https://community.home-assistant.io/t/google-findmy-find-hub-integration/931136)

Your discovery helps the entire community! 🙏

---

## 📚 Additional Resources

### Official Documentation

- **FMDN Specification:** [Google Developers - FMDN](https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn)
- **Shizuku Setup:** [shizuku.rikka.app](https://shizuku.rikka.app/guide/setup/)
- **PCAPdroid Guide:** [PCAPdroid Docs](https://emanuele-f.github.io/PCAPdroid/)
- **Bermuda Integration:** [jleinenbach/bermuda](https://github.com/jleinenbach/bermuda)

### Related Tools

- **GoogleFindMyTools:** [leonboe1/GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools) - Original FMDN toolkit
- **JADX:** [skylot/jadx](https://github.com/skylot/jadx) - APK decompiler
- **Wireshark:** [wireshark.org](https://www.wireshark.org/) - Network protocol analyzer

### Integration Documentation

- **FMDN Technical Reference:** [FMDN.md](../custom_components/googlefindmy/FMDN.md)
- **Endpoint Discovery Guide:** [FMDN_ENDPOINT_DISCOVERY.md](FMDN_ENDPOINT_DISCOVERY.md)
- **Analysis Tools:** [tools/README.md](../tools/README.md)

---

## 🆘 Need Help?

**Discord:** Join [Google FindMy Discord Server](https://discord.gg/U3MkcbGzhc) for real-time help

**GitHub Issues:** [Report problems or ask questions](https://github.com/jleinenbach/GoogleFindMy-HA/issues)

**Home Assistant Community:** [Integration thread](https://community.home-assistant.io/t/google-findmy-find-hub-integration/931136)

---

**Good luck discovering the endpoint! 🚀**

*Your Home Assistant instance is about to become a full FMDN Finder!*
