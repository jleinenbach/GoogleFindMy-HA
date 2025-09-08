# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2025-01-09

### Added
- Extended FCM timeout from 10s to 60s for better device GPS acquisition
- Automatic retry logic when stale location data is received
- Enhanced FCM debugging with detailed logging
- Location staleness filtering (configurable, default 30 minutes)
- Comprehensive location smoothing and stability detection
- Support for both individual OAuth tokens and GoogleFindMyTools secrets.json
- Advanced device tracker with anti-bounce filtering
- Configurable accuracy and movement thresholds
- External location service for FCM workarounds

### Changed
- **BREAKING**: Stricter location data validation (rejects data older than 30 minutes by default)
- Improved sequential device polling with better error handling
- Enhanced Firebase Cloud Messaging integration
- Better logging and debugging capabilities
- Updated authentication flow with multiple methods

### Fixed
- GPS coordinate bouncing for stationary devices
- Stale location data being displayed as current
- FCM connection timeout issues
- Rate limiting compliance improvements
- Memory leaks in location caching

### Technical Improvements
- Asynchronous location requests with proper timeout handling
- Enhanced error handling and recovery
- Better caching mechanisms for location data
- Improved Home Assistant integration patterns
- More robust authentication token management

## [1.x.x] - Previous Versions
- Basic Google Find My Device integration
- Simple location polling
- OAuth authentication
- Basic device tracking functionality

---

## Migration Guide

### From 1.x to 2.0

**Configuration Changes:**
- The integration now requires reconfiguration due to authentication improvements
- Location data older than 30 minutes is rejected by default
- New configuration options for staleness threshold, accuracy, and movement detection

**Behavior Changes:**
- Devices may show "unknown" location initially if cached data is too old
- More accurate location reporting with reduced false movement detection
- Better handling of devices that haven't moved recently

**Benefits:**
- Much more accurate location reporting
- Elimination of GPS bouncing issues
- Better battery life on tracked devices due to smarter polling
- Enhanced debugging capabilities